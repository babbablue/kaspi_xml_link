import asyncio
import os
import json
import base64
import hashlib


"""Generate kaspi.xml and copy to docs/ for GitHub Pages.

This script intentionally does not use Google Drive. Use GitHub Actions to
publish `docs/kaspi.xml` on an hourly schedule (workflow present in
.github/workflows/generate-xml.yml).
"""

import shutil
from dotenv import load_dotenv
import kaspi_xml_sync


async def main():
    load_dotenv()
    print("\nНачинаем процесс генерации kaspi.xml...")

    await kaspi_xml_sync.update_xml()
    print("Процесс генерации kaspi.xml завершен.")


if __name__ == '__main__':
    asyncio.run(main())