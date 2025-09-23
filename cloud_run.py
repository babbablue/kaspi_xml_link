import os
import json
import base64
import asyncio
import hashlib
from google.oauth2 import service_account
"""Generate kaspi.xml and copy to docs/ for GitHub Pages.

This script intentionally does not use Google Drive. Use GitHub Actions to
publish `docs/kaspi.xml` on an hourly schedule (workflow present in
.github/workflows/generate-xml.yml).
"""

import os
import asyncio
import shutil
from dotenv import load_dotenv
import kaspi_xml_sync


def main():
    load_dotenv()
    print("\nНачинаем процесс генерации kaspi.xml...")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        products = loop.run_until_complete(kaspi_xml_sync.fetch_products())
        print(f"Получено {len(products)} продуктов")
        kaspi_xml_sync.generate_xml(products)
    finally:
        loop.close()

    # Копируем XML в docs/ для GitHub Pages
    try:
        os.makedirs('docs', exist_ok=True)
        shutil.copyfile('kaspi.xml', os.path.join('docs', 'kaspi.xml'))
        print("Файл kaspi.xml скопирован в docs/kaspi.xml")
    except Exception as e:
        print(f"Не удалось скопировать файл в docs/: {e}")


if __name__ == '__main__':
    main()