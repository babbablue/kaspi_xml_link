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
import json  # Добавляем импорт json для отладки
from functools import wraps

# Настройка логирования
logging.basicConfig(filename='kaspi_xml_sync.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', encoding='utf-8')

from dotenv import load_dotenv
import os

# Конфигурация через переменные окружения (поддерживается .env)
load_dotenv()

# LOGIN и PASSWORD должны храниться в окружении (не в коде). Примеры: LOGIN, PASSWORD
LOGIN = os.getenv('MS_LOGIN') or os.getenv('LOGIN')
PASSWORD = os.getenv('MS_PASSWORD') or os.getenv('PASSWORD')

# UUID атрибута для фильтрации продуктов (чекбокс "Выгружать на Каспи?")
ATTRIBUTE_ID = os.getenv('ATTRIBUTE_ID', '14858c5a-ccb7-11ef-0a80-08a200511bcd')
# Внешний код склада для остатков
STOCK_EXTERNAL_CODE = os.getenv('STOCK_ID', 'V2M50lgsggOhAsUxFXeMK3')

app = Flask(__name__)

# Control queue for commands from external GUI (thread-safe)
control_queue = queue.Queue()

# runtime status
last_generated_time = None
event_loop = None
current_token = None # Добавляем глобальную переменную для хранения токена

# Декоратор для повторных попыток
def retry_async(retries=2, delay=15):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for i in range(retries + 1):
                try:
                    result = await func(*args, **kwargs)
                    if result is not None:
                        return result
                except Exception as e:
                    logging.warning(f"Попытка {i + 1}/{retries + 1}: Функция {func.__name__} вызвала исключение: {e}")
                if i < retries:
                    logging.info(f"Повторная попытка через {delay} секунд...")
                    await asyncio.sleep(delay)
            logging.error(f"Функция {func.__name__} не выполнилась после {retries + 1} попыток.")
            return None
        return wrapper
    return decorator

@retry_async()
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
            else:
                logging.error(f"Failed to obtain access token: {response.status} - {await response.text()}")
                return None

async def ensure_token_is_valid(force_refresh=False):
    global current_token
    if force_refresh or not current_token:
        if force_refresh:
            logging.info("Forcing token refresh.")
        else:
            logging.info("No current token, attempting to get a new one.")
        current_token = await get_access_token()
        if not current_token:
            logging.error("Failed to obtain a valid access token.")
            return False
    return True

@retry_async()
async def get_store_href(token):
    url = f"https://api.moysklad.ru/api/remap/1.2/entity/store?filter=externalCode={STOCK_EXTERNAL_CODE}"
    headers = {"Authorization": f"Bearer {token}"}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                rows = data.get("rows", [])
                if rows:
                    logging.info(f"Store with externalCode {STOCK_EXTERNAL_CODE} found: {rows[0]['meta']['href']}")
                    return rows[0]["meta"]["href"]
                else:
                    logging.error(f"Store with externalCode {STOCK_EXTERNAL_CODE} not found")
                    return None
            else:
                logging.error(f"Failed to fetch store: {response.status} - {await response.text()}")
                return None

# Удаляем декоратор retry_async отсюда
async def get_stock_for_products(products):
    """Получаем остатки для списка товаров"""
    if not products:
        logging.debug("get_stock_for_products: Product list is empty.")
        return {}

    if not await ensure_token_is_valid():
        return {}

    store_href = await get_store_href(current_token)
    if not store_href:
        logging.error("get_stock_for_products: Store href not found.")
        return {}
    store_id = store_href.split('/')[-1] # Извлекаем ID склада из href

    stock_data = {}
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        url = "https://api.moysklad.ru/api/remap/1.2/report/stock/all"
        headers = {"Authorization": f"Bearer {current_token}"}
        params = {
            "store.id": store_id, # Используем store.id для фильтрации
            "limit": 1000, # Увеличиваем лимит для уменьшения количества запросов
            "offset": 0
        }

        all_stock_rows = []
        retries_401 = 0
        max_retries_401 = 3 # Увеличиваем количество повторных попыток для 401 ошибки

        while True:
            logging.debug(f"Fetching stock page with offset {params['offset']}")
            try:
                async with session.get(url, headers=headers, params=params) as response:
                    if response.status == 401:
                        logging.warning(f"Stock API returned 401 for offset {params['offset']}. Attempting to refresh token. Retries left: {max_retries_401 - retries_401}.")
                        if retries_401 < max_retries_401:
                            logging.info("Attempting to force token refresh...")
                            success = await ensure_token_is_valid(force_refresh=True) # Принудительно запрашиваем новый токен
                            if success:
                                headers["Authorization"] = f"Bearer {current_token}"
                                retries_401 += 1
                                logging.info(f"Token refreshed, retrying request for offset {params['offset']}.")
                                continue # Повторяем текущий запрос с новым токеном
                            else:
                                logging.error("Failed to refresh token after 401. Aborting stock data fetch.")
                                return {}
                        else:
                            logging.error("Max token refresh retries reached after 401. Aborting stock data fetch.")
                            return {}
                    elif response.status != 200:
                        logging.error(f"Failed to get stock data: {response.status} - {await response.text()}")
                        return {}
                    
                    data = await response.json()

                rows = data.get("rows", [])
                all_stock_rows.extend(rows)
                logging.info(f"Stock API returned {len(rows)} records for offset {params['offset']}. Total stock records fetched: {len(all_stock_rows)}")
                
                # Сбрасываем счетчик повторных попыток 401 после успешного запроса
                retries_401 = 0

                if len(rows) < params["limit"]:
                    break # Достигнут конец данных
                params["offset"] += params["limit"]
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.error(f"Exception getting stock data: {e}")
                return {}

    for row in all_stock_rows:
        meta = row.get("meta", {})
        if meta.get("type") == "product":
            product_href = meta["href"]
            product_id = product_href.split("/")[-1].split("?")[0] # Очищаем product_id от параметров запроса
            stock = row.get("quantity", 0)
            stock_data[product_id] = stock
            if logging.getLogger().level == logging.DEBUG: # Логируем детально только в режиме DEBUG
                if len(stock_data) <= 10 or product_id in [p.get("id") for p in products[:10]]: # Для первых 10 или если это один из первых 10 продуктов
                    logging.debug(f"Product {product_id} - Stock from MoySklad: {stock}")
        else:
            logging.debug(f"Skipping non-product assortment (type: {meta.get('type', 'None')}).")

    logging.info(f"Retrieved stock data for {len(stock_data)} unique products. Total raw stock records fetched: {len(all_stock_rows)}")
    return stock_data

def has_kaspi_attribute(product):
    """Проверяет, отмечен ли чекбокс 'Выгружать на Каспи?' у товара"""
    attrs = product.get("attributes") or product.get("productAttributeValues") or []

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
                return True
            if isinstance(val, dict) and val.get("value") in [True, "true", "yes", 1, "1"]:
                return True
            # Если значение - это словарь с полем value, проверяем его
            if isinstance(val, dict) and val.get("value") is True:
                return True

    return False

@retry_async()
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

    # ВКЛЮЧАЕМ ФИЛЬТР ПО АТРИБУТУ (как показало тестирование)
    if ATTRIBUTE_ID and not ATTRIBUTE_ID.startswith("your_"):
        filter_url = f"https://api.moysklad.ru/api/remap/1.2/entity/product/metadata/attributes/{ATTRIBUTE_ID}=true"
        params["filter"] = filter_url
        logging.info(f"Using API filter for attribute: {ATTRIBUTE_ID}")
        logging.info(f"Using filter URL: {filter_url}")

    products = []
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        current_url = url # Инициализируем текущий URL
        current_params = params # Инициализируем параметры для первого запроса
        while current_url:
            logging.info(f"Fetching page: {current_url}")
            try:
                async with session.get(current_url, headers=headers, params=current_params) as response:
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
            logging.info(f"Fetched {len(rows)} products from current page.")

            if "filter" in params:
                products.extend(rows)
                logging.info(f"Filter active, added {len(rows)} products")
            else:
                filtered = []
                for p in rows:
                    if has_kaspi_attribute(p):
                        filtered.append(p)
                products.extend(filtered)
                logging.info(f"Local filtering, added {len(filtered)} products")

            current_url = data.get("meta", {}).get("nextHref")
            current_params = None # После первого запроса, используем nextHref напрямую, без params
    logging.info(f"Total fetched {len(products)} products")
    return products

async def generate_xml(products):
    logging.info(f"Starting XML generation for {len(products)} products.")
    if not products:
        logging.warning("Невозможно сгенерировать XML: список продуктов пуст.")
        return False # Возвращаем False, если нет продуктов для генерации

    root = ET.Element("kaspi_catalog", xmlns="kaspiShopping", date=datetime.datetime.now().strftime("%Y-%m-%d"))
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")
    root.set("xsi:schemaLocation", "http://kaspi.kz/kaspishopping.xsd")

    company = os.getenv('COMPANY', 'ИП ВОЗРОЖДЕНИЕ')
    merchant_id = os.getenv('MERCHANT_ID', '30286450')
    ET.SubElement(root, "company").text = company
    ET.SubElement(root, "merchantid").text = merchant_id
    offers = ET.SubElement(root, "offers")

    stock_data = {}
    if products:
        stock_data = await get_stock_for_products(products)
        logging.info(f"Получено данных об остатках для {len(stock_data)} продуктов")

    products_in_xml = 0
    products_with_zero_stock = 0

    for p in products:
        product_id = p.get("id")
        stock_count = stock_data.get(product_id, 0)
        
        # Убеждаемся что stock_count целое число
        stock_count = int(stock_count)

        if stock_count == 0:
            products_with_zero_stock += 1
            logging.debug(f"Продукт {p.get('name', 'Unknown')} (ID: {product_id}) имеет нулевой остаток, пропускаем.")
            continue # Пропускаем товары с нулевым остатком

        offer = ET.SubElement(offers, "offer", sku=p.get("code", str(p["id"])))
        ET.SubElement(offer, "model").text = p.get("name", "Unknown")
        brand_name = p.get("brand", {}).get("name", "Без бренда") if p.get("brand") else "Без бренда"
        ET.SubElement(offer, "brand").text = brand_name

        price = 0
        price_attribute_id = os.getenv('PRICE_ATTRIBUTE_ID', 'fc15ca1c-d188-11ef-0a80-08a200511bcd')
        if price_attribute_id:
            attrs = p.get("attributes") or p.get("productAttributeValues") or []
            for a in attrs:
                meta = a.get("meta", {}) or {}
                href = meta.get("href", "")
                if not href and a.get("attribute"):
                    href = a["attribute"].get("meta", {}).get("href", "")
                if price_attribute_id in href or a.get("id") == price_attribute_id:
                    val = a.get("value")
                    if isinstance(val, (int, float)):
                        price = int(val)
                    elif isinstance(val, str):
                        try:
                            price = int(float(val))
                        except ValueError:
                            price = 0
                    elif isinstance(val, dict) and "value" in val:
                        try:
                            price = int(float(val["value"]))
                        except (ValueError, TypeError):
                            price = 0
                    break

        if price == 0:
            sale_price = p.get("salePrices", [{}])[0].get("value", 0)
            if sale_price:
                price = sale_price // 100
            else:
                price = 0

        price = int(price)

        availabilities = ET.SubElement(offer, "availabilities")
        ET.SubElement(availabilities, "availability", available="yes", storeId="PP1", stockCount=str(int(stock_count)))
        ET.SubElement(offer, "price").text = str(price)
        products_in_xml += 1

    if products_in_xml == 0:
        logging.warning("Не найдено товаров с ненулевым остатком для включения в XML.")
        return False

    tree = ET.ElementTree(root)

    xml_file = os.getenv('XML_FILE', 'kaspi.xml')
    docs_dir = "docs"
    if not os.path.exists(docs_dir):
        os.makedirs(docs_dir)

    # Только если есть товары для записи
    full_xml_path = os.path.join(docs_dir, xml_file)
    tree.write(full_xml_path, encoding="utf-8", xml_declaration=True)
    backup_file = f"kaspi_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.xml"
    tree.write(os.path.join(docs_dir, backup_file), encoding="utf-8", xml_declaration=True)
    global last_generated_time
    last_generated_time = datetime.datetime.now()
    logging.info(f"XML успешно сгенерирован в {full_xml_path}. Добавлено {products_in_xml} товаров. Пропущено {products_with_zero_stock} товаров с нулевым остатком.")
    return True

async def update_xml():
    logging.info('update_xml: started')
    products = await fetch_products()
    logging.info(f'update_xml: fetched {len(products)} products from MoySklad.')
    if not products:
        logging.error("Не удалось получить товары из МойСклад. Пропускаем генерацию XML.")
        return

    xml_generated_successfully = await generate_xml(products)
    if xml_generated_successfully:
        logging.info('update_xml: finished successfully.')
    else:
        logging.warning('update_xml: XML не был сгенерирован или содержит 0 товаров. Оставляем старый XML.')

@app.route("/xml")
def serve_xml():
    xml_file = os.path.join("docs", os.getenv('XML_FILE', 'kaspi.xml'))
    return send_file(xml_file, mimetype="application/xml")

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