import aiohttp
import asyncio
import xml.etree.ElementTree as ET
from flask import Flask, send_file
import schedule
import queue
import threading
from flask import jsonify, request
import time
import logging
import datetime
import base64

# Настройка логирования
logging.basicConfig(filename='kaspi_xml_sync.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

from dotenv import load_dotenv
import os

# Конфигурация через переменные окружения (поддерживается .env)
load_dotenv()

# LOGIN и PASSWORD должны храниться в окружении (не в коде). Примеры: LOGIN, PASSWORD
LOGIN = os.getenv('MS_LOGIN') or os.getenv('LOGIN')
PASSWORD = os.getenv('MS_PASSWORD') or os.getenv('PASSWORD')

# UUID атрибута для фильтрации продуктов (если нужен)
ATTRIBUTE_ID = os.getenv('ATTRIBUTE_ID') or ''

# Компания и merchant id
COMPANY = os.getenv('COMPANY', 'ИП ВОЗРОЖДЕНИЕ')
MERCHANT_ID = os.getenv('MERCHANT_ID', '30286450')
XML_FILE = os.getenv('XML_FILE', 'kaspi.xml')

app = Flask(__name__)

# Control queue for commands from external GUI (thread-safe)
control_queue = queue.Queue()

# runtime status
last_generated_time = None
event_loop = None

async def get_access_token():
    if not LOGIN or not PASSWORD:
        logging.error('MS_LOGIN / MS_PASSWORD not set in environment')
        return None
    credentials = f"{LOGIN}:{PASSWORD}"
    encoded_credentials = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
    url = "https://api.moysklad.ru/api/remap/1.2/security/token"
    headers = {"Authorization": f"Basic {encoded_credentials}"}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers) as response:
            if response.status in [200, 201]:
                data = await response.json()
                logging.info("Successfully obtained access token")
                return data["access_token"]
            logging.error(f"Failed to get token: {response.status}")
            return None

async def fetch_products():
    token = await get_access_token()
    if not token:
        logging.error("No token, cannot fetch products")
        return []
    url = "https://api.moysklad.ru/api/remap/1.2/entity/product"
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "limit": 100,
        "expand": "brand"
    }
    products = []
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while url:
            logging.info(f"Fetching page: {url}")
            try:
                async with session.get(url, headers=headers, params=params) as response:
                    if response.status != 200:
                        logging.error(f"API error: {response.status} - {await response.text()}")
                        return []
                    data = await response.json()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.error(f"Exception while fetching products: {e}")
                return []

            rows = data.get("rows", [])

            # Если ATTRIBUTE_ID настроен (и не оставлен placeholder), попробуем фильтровать локально.
            if ATTRIBUTE_ID and not ATTRIBUTE_ID.startswith("your_"):
                filtered = []
                for p in rows:
                    attrs = p.get("attributes") or p.get("productAttributeValues") or []
                    include = False
                    for a in attrs:
                        # Пытаемся определить UUID атрибута в meta.href или id
                        meta = a.get("meta", {}) or {}
                        href = meta.get("href", "")
                        # Некоторые структуры хранят ссылку на атрибут в поле 'attribute'
                        if not href and a.get("attribute"):
                            href = a["attribute"].get("meta", {}).get("href", "")
                        if ATTRIBUTE_ID in href or a.get("id") == ATTRIBUTE_ID:
                            # проверяем значение - может быть булево или словарь
                            val = a.get("value")
                            if isinstance(val, bool) and val:
                                include = True
                                break
                            if isinstance(val, dict) and val.get("value") in [True, "true", "yes", 1, "1"]:
                                include = True
                                break
                    if include:
                        filtered.append(p)
                products.extend(filtered)
            else:
                # Если ATTRIBUTE_ID не настроен — добавляем все строки (пользователь может потом локально фильтровать)
                products.extend(rows)

            url = data.get("meta", {}).get("nextHref")
    logging.info(f"Fetched {len(products)} products")
    return products

def generate_xml(products):
    root = ET.Element("kaspi_catalog", xmlns="kaspiShopping", date=datetime.datetime.now().strftime("%Y-%m-%d"))
    ET.SubElement(root, "company").text = COMPANY
    ET.SubElement(root, "merchantid").text = MERCHANT_ID
    offers = ET.SubElement(root, "offers")
    for p in products:
        offer = ET.SubElement(offers, "offer", sku=p.get("code", str(p["id"])))
        ET.SubElement(offer, "model").text = p.get("name", "Unknown")
        brand_name = p.get("brand", {}).get("name", "Без бренда") if p.get("brand") else "Без бренда"
        ET.SubElement(offer, "brand").text = brand_name
        avail = ET.SubElement(ET.SubElement(offer, "availabilities"), "availability", available="yes", storeId="PP1", stockCount=str(p.get("stock", 0)))
        ET.SubElement(offer, "price").text = str(p.get("salePrices", [{}])[0].get("value", 0) // 100)
    tree = ET.ElementTree(root)
    tree.write(XML_FILE, encoding="utf-8", xml_declaration=True)
    backup_file = f"kaspi_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.xml"
    tree.write(backup_file, encoding="utf-8", xml_declaration=True)
    global last_generated_time
    last_generated_time = datetime.datetime.now()
    logging.info("XML generated")

async def update_xml():
    logging.info('update_xml: started')
    products = await fetch_products()
    logging.info(f'update_xml: fetched {len(products)} products')
    generate_xml(products)
    logging.info('update_xml: finished')

@app.route("/xml")
def serve_xml():
    return send_file(XML_FILE, mimetype="application/xml")


@app.route('/control/generate')
def control_generate():
    """Trigger generation now (schedules coroutine in event loop)."""
    try:
        # Put a command into the control queue to be handled by main loop
        control_queue.put_nowait({'cmd': 'generate_now'})
        return jsonify({'status': 'ok', 'message': 'generation scheduled'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/control/schedule')
def control_schedule():
    """Set schedule interval in minutes: /control/schedule?minutes=15"""
    minutes = request.args.get('minutes')
    try:
        minutes = int(minutes)
    except Exception:
        return jsonify({'status': 'error', 'message': 'invalid minutes parameter'}), 400
    control_queue.put_nowait({'cmd': 'set_schedule', 'minutes': minutes})
    return jsonify({'status': 'ok', 'message': f'schedule set to {minutes} minutes'})


@app.route('/control/status')
def control_status():
    """Return simple JSON status."""
    running = True
    lg = last_generated_time.isoformat() if last_generated_time else None
    return jsonify({'server': running, 'last_generated': lg})


@app.route('/control/stop', methods=['POST'])
def control_stop():
    """Request the process to stop (will exit)."""
    control_queue.put_nowait({'cmd': 'stop'})
    return jsonify({'status': 'ok', 'message': 'stop requested'})

def run_flask():
    # Disable reloader to avoid double-start when run as subprocess
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

async def main():
    global event_loop
    event_loop = asyncio.get_running_loop()

    # Start Flask server immediately so GUI/control endpoints are available
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # schedule default job but do first update in background
    schedule.clear()
    schedule.every().hour.do(lambda: asyncio.create_task(update_xml()))

    # kick off first generation in background so server is responsive
    asyncio.create_task(update_xml())

    running = True
    try:
        while running:
            # process control commands from GUI
            try:
                cmd = control_queue.get_nowait()
            except queue.Empty:
                cmd = None
            if cmd:
                c = cmd.get('cmd')
                if c == 'generate_now':
                    logging.info('Control: generate_now received')
                    asyncio.create_task(update_xml())
                elif c == 'set_schedule':
                    minutes = int(cmd.get('minutes', 60))
                    logging.info(f'Control: set_schedule {minutes} minutes')
                    schedule.clear()
                    schedule.every(minutes).minutes.do(lambda: asyncio.create_task(update_xml()))
                elif c == 'stop':
                    logging.info('Control: stop received, exiting')
                    running = False
                    break

            schedule.run_pending()
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        logging.info('Main loop cancelled')
    except KeyboardInterrupt:
        logging.info('KeyboardInterrupt received, shutting down')
    except Exception as e:
        logging.exception(f'Unhandled exception in main loop: {e}')
    finally:
        logging.info('Main exiting, stopping Flask thread if running')

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info('Program interrupted by user')