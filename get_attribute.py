import base64
import requests
import json

# Скопируйте свои учётные данные из kaspi_xml_sync.py
LOGIN = "admin@hobbymrkt"
PASSWORD = "cosmic7878"

url = "https://api.moysklad.ru/api/remap/1.2/entity/product/metadata/attributes"
auth = (LOGIN, PASSWORD)
headers = {"Accept": "application/json;charset=utf-8"}

resp = requests.get(url, auth=auth, headers=headers, timeout=30)
if resp.status_code != 200:
    print(f"HTTP {resp.status_code}: {resp.text}")
    raise SystemExit(1)

data = resp.json()
rows = data.get("rows", [])
found = []
for r in rows:
    name = r.get("name")
    rid = r.get("id") or r.get("meta", {}).get("href")
    if name and "Каспи" in name or name == "Выгружать на Каспи?":
        found.append((rid, name, r.get("meta", {})))

if not found:
    print("Не найдено атрибутов с 'Каспи' в имени. Всего атрибутов:")
    for r in rows[:20]:
        print(json.dumps({"id": r.get("id"), "name": r.get("name")}, ensure_ascii=False))
    print("Если нужно, удалите фильтр поиска в скрипте и пересмотрите список")
else:
    print("Найдено атрибутов:")
    for fid, name, meta in found:
        print(json.dumps({"id": fid, "name": name, "meta": meta}, ensure_ascii=False))
