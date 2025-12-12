# -*- coding: utf-8 -*-
import os
import logging
import aiohttp
import asyncio
from flask import Flask, send_file
import schedule
import queue
import threading
from flask import jsonify, request
import time
import datetime
import base64
import json
import xml.etree.ElementTree as ET
from functools import wraps

# Настройка логирования
logging.basicConfig(filename='kaspi_xml_sync.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', encoding='utf-8')

from dotenv import load_dotenv

# Конфигурация через переменные окружения (поддерживается .env)
load_dotenv()

# LOGIN и PASSWORD должны храниться в окружении (не в коде)
LOGIN = os.getenv('MS_LOGIN') or os.getenv('LOGIN')
PASSWORD = os.getenv('MS_PASSWORD') or os.getenv('PASSWORD')

# UUID атрибута для фильтрации продуктов (чекбокс "Выгружать на Каспи?")
ATTRIBUTE_ID = os.getenv('ATTRIBUTE_ID', '14858c5a-ccb7-11ef-0a80-08a200511bcd')
# Внешний код склада для остатков
STOCK_EXTERNAL_CODE = os.getenv('STOCK_ID', 'V2M50lgsggOhAsUxFXeMK3')
# ID типа цены "Каспи"
KASPI_PRICE_TYPE_ID = os.getenv('KASPI_PRICE_TYPE_ID', '9fd68e0e-ca75-11ef-0a80-0c7900359c7d')

app = Flask(__name__)

# Control queue for commands from external GUI (thread-safe)
control_queue = queue.Queue()

# runtime status
last_generated_time = None
event_loop = None
current_token = None

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

async def get_stock_for_products(products):
    """Получаем остатки для списка товаров с учетом резервов"""
    if not products:
        logging.debug("get_stock_for_products: Product list is empty.")
        return {}

    if not await ensure_token_is_valid():
        return {}

    start_ts = time.time()
    logging.info("get_stock_for_products: начинаю запрос отчета по складу...")

    store_href = await get_store_href(current_token)
    if not store_href:
        logging.error("get_stock_for_products: Store href not found.")
        return {}
    store_id = store_href.split('/')[-1]

    stock_data = {}
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        url = "https://api.moysklad.ru/api/remap/1.2/report/stock/all"
        headers = {"Authorization": f"Bearer {current_token}"}
        params = {
            "store.id": store_id,
            "stockMode": "all",
            "limit": 1000,
            "offset": 0
        }

        all_stock_rows = []
        retries_401 = 0
        max_retries_401 = 3

        while True:
            logging.debug(f"Fetching stock page with offset {params['offset']}")
            try:
                async with session.get(url, headers=headers, params=params) as response:
                    if response.status == 401:
                        if retries_401 < max_retries_401:
                            success = await ensure_token_is_valid(force_refresh=True)
                            if success:
                                headers["Authorization"] = f"Bearer {current_token}"
                                retries_401 += 1
                                continue
                            else:
                                return {}
                        else:
                            return {}
                    elif response.status != 200:
                        logging.error(f"Failed to get stock data: {response.status} - {await response.text()}")
                        return {}
                    
                    data = await response.json()
                    rows = data.get("rows", [])
                    all_stock_rows.extend(rows)
                    logging.debug(f"Stock API returned {len(rows)} records for offset {params['offset']}")
                    
                    if len(rows) < params["limit"]:
                        break
                    params["offset"] += params["limit"]

            except Exception as e:
                logging.error(f"Exception getting stock data: {e}")
                return {}

    # Обработка полученных данных с учетом резервов (только товары product)
    for row in all_stock_rows:
        meta = row.get("meta", {})
        entity_type = meta.get("type")
        if entity_type == "product":
            entity_id = meta["href"].split("/")[-1].split("?")[0]
            stock = row.get("stock", 0)  # Общий остаток
            reserve = row.get("reserve", 0)  # Резерв
            available = max(0, stock - reserve)  # Доступный остаток
            stock_data[entity_id] = available
            
            if logging.getLogger().level == logging.DEBUG:
                logging.debug(f"Product {entity_id} - Stock: {stock}, Reserve: {reserve}, Available: {available}")

    elapsed = time.time() - start_ts
    logging.info(f"Retrieved stock data for {len(stock_data)} unique products in {elapsed:.1f} seconds")
    return stock_data

def has_kaspi_attribute(product):
    """Проверяет, отмечен ли чекбокс 'Выгружать на Каспи?' у товара"""
    attrs = product.get("attributes") or []
    for attr in attrs:
        meta = attr.get("meta", {})
        href = meta.get("href", "")
        if ATTRIBUTE_ID in href or attr.get("id") == ATTRIBUTE_ID:
            val = attr.get("value")
            if isinstance(val, bool):
                return val
            elif isinstance(val, dict) and "value" in val:
                return val["value"] is True
    return False

async def fetch_entity_items(token, entity_type, use_attribute_filter=True):
    """Базовый загрузчик сущностей (товары/комплекты) с фильтрацией по атрибуту."""
    if not token:
        logging.error(f"No token, cannot fetch {entity_type}")
        return []

    base_url = f"https://api.moysklad.ru/api/remap/1.2/entity/{entity_type}"
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "limit": 100,
        "expand": "attributes,salePrices,components,components.assortment"
    }

    filter_active = False
    if use_attribute_filter and ATTRIBUTE_ID:
        params["filter"] = f"{base_url}/metadata/attributes/{ATTRIBUTE_ID}=true"
        filter_active = True
        logging.info(f"[{entity_type}] Using API filter for attribute: {ATTRIBUTE_ID}")

    items = []
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        current_url = base_url
        current_params = params
        while current_url:
            logging.info(f"[{entity_type}] Fetching page: {current_url}")
            try:
                async with session.get(current_url, headers=headers, params=current_params) as response:
                    if response.status != 200:
                        logging.error(f"[{entity_type}] API error: {response.status} - {await response.text()}")
                        return []
                    data = await response.json()
            except Exception as e:
                logging.error(f"[{entity_type}] Exception while fetching entities: {e}")
                return []

            rows = data.get("rows", [])
            logging.info(f"[{entity_type}] Received {len(rows)} entities from current page.")

            if filter_active:
                items.extend(rows)
            else:
                filtered_rows = [p for p in rows if has_kaspi_attribute(p)]
                items.extend(filtered_rows)
                logging.info(f"[{entity_type}] Local filtering kept {len(filtered_rows)} entities.")

            current_url = data.get("meta", {}).get("nextHref")
            current_params = None

    logging.info(f"[{entity_type}] Total fetched {len(items)} entities after filtering.")
    return items

@retry_async()
async def fetch_products():
    token = await get_access_token()
    if not token:
        logging.error("No token, cannot fetch products and bundles")
        return []

    products = await fetch_entity_items(token, "product", use_attribute_filter=True)
    bundles = await fetch_entity_items(token, "bundle", use_attribute_filter=False)

    total_items = products + bundles
    logging.info(f"Fetched {len(products)} products and {len(bundles)} bundles. Total items: {len(total_items)}")
    return total_items

async def generate_xml(products):
    logging.info(f"Starting XML generation for {len(products)} products.")
    if not products:
        logging.warning("Невозможно сгенерировать XML: список продуктов пуст.")
        return False

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
        logging.info("generate_xml: запрашиваем остатки по складу для всех позиций...")
        t_stock = time.time()
        stock_data = await get_stock_for_products(products)
        logging.info(f"generate_xml: остатки получены для {len(stock_data)} товаров за {time.time() - t_stock:.1f} секунд")

    products_in_xml = 0
    products_with_zero_stock = 0

    for p in products:
        product_id = p.get("id")
        meta = p.get("meta", {})
        entity_type = meta.get("type", "product")

        # Остаток для товара берём напрямую из отчета, для комплекта считаем по компонентам
        if entity_type == "product":
            stock_count = int(stock_data.get(product_id, 0))
        elif entity_type == "bundle":
            # Расчет остатка комплекта по компонентам: минимум по доступности всех товарных компонент
            components_block = p.get("components") or []
            if isinstance(components_block, dict):
                raw_components = components_block.get("rows") or []
            elif isinstance(components_block, list):
                raw_components = components_block
            else:
                raw_components = []

            bundle_available = None
            for comp in raw_components:
                assortment = comp.get("assortment") if isinstance(comp, dict) else None
                if isinstance(assortment, str):
                    assortment = {"meta": {"href": assortment}}
                assortment = assortment or {}
                comp_meta = assortment.get("meta", {})
                if comp_meta.get("type") != "product":
                    continue
                comp_id = comp_meta.get("href", "").split("/")[-1].split("?")[0]
                quantity = comp.get("quantity", 1) if isinstance(comp, dict) else 1
                available_comp = int(stock_data.get(comp_id, 0))
                # Сколько комплектов можно собрать из этого компонента
                if quantity <= 0:
                    continue
                comp_limit = available_comp // quantity
                if bundle_available is None:
                    bundle_available = comp_limit
                else:
                    bundle_available = min(bundle_available, comp_limit)

            stock_count = int(bundle_available or 0)
            logging.debug(f"Комплект {p.get('name', 'Unknown')} (ID: {product_id}) доступен в кол-ве {stock_count} по компонентам.")
        else:
            stock_count = 0

        if stock_count == 0:
            products_with_zero_stock += 1
            logging.debug(f"Продукт {p.get('name', 'Unknown')} (ID: {product_id}) имеет нулевой остаток, пропускаем.")
            continue

        offer = ET.SubElement(offers, "offer", sku=p.get("code", str(p["id"])))
        ET.SubElement(offer, "model").text = p.get("name", "Unknown")
        brand_name = p.get("brand", {}).get("name", "Без бренда") if p.get("brand") else "Без бренда"
        ET.SubElement(offer, "brand").text = brand_name

        # Получаем цену Каспи из типов цен
        price = 0
        sale_prices = p.get('salePrices', [])
        for price_info in sale_prices:
            price_type = price_info.get('priceType', {})
            if price_type.get('id') == KASPI_PRICE_TYPE_ID:
                price = int(price_info.get('value', 0) / 100)  # Переводим копейки в рубли
                logging.debug(f"Найдена цена Каспи для товара {p.get('name')}: {price}")
                break
        
        # Если цена Каспи не найдена, берем первую доступную цену
        if price == 0:
            for price_info in sale_prices:
                value = price_info.get('value', 0)
                if value > 0:
                    price = int(value / 100)
                    logging.debug(f"Использована стандартная цена для товара {p.get('name')}: {price}")
                    break
        
        if price == 0:
            logging.warning(f"Не найдено цен для товара {p.get('name')} (артикул: {p.get('code')})")

        availabilities = ET.SubElement(offer, "availabilities")
        ET.SubElement(availabilities, "availability", available="yes", storeId="PP1", stockCount=str(stock_count))
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
    print(f"[{datetime.datetime.now().isoformat()}] update_xml: старт")
    t_start = time.time()

    products = await fetch_products()
    logging.info(f'update_xml: fetched {len(products)} products from MoySklad.')
    print(f"[{datetime.datetime.now().isoformat()}] update_xml: получено {len(products)} позиций из МойСклад за {time.time() - t_start:.1f} секунд")

    if not products:
        logging.error("Не удалось получить товары из МойСклад. Пропускаем генерацию XML.")
        return

    t_gen = time.time()
    xml_generated_successfully = await generate_xml(products)
    print(f"[{datetime.datetime.now().isoformat()}] update_xml: generate_xml завершен за {time.time() - t_gen:.1f} секунд")

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
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

async def main():
    global event_loop
    event_loop = asyncio.get_running_loop()

    # Start Flask server
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # schedule default job but do first update in background
    schedule.clear()
    schedule.every().hour.do(lambda: asyncio.create_task(update_xml()))

    # kick off first generation in background
    asyncio.create_task(update_xml())

    running = True
    try:
        while running:
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