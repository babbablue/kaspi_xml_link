# kaspi_xml_link

Проект генерирует XML в формате Kaspi и публикует `docs/kaspi.xml` для использования через GitHub Pages.

Ключевые моменты:

- Удалена интеграция с Google Drive. Для публикации XML используется GitHub Pages (папка `docs/`).
- Чувствительные данные (логин/пароль для MoySklad) берутся из переменных окружения или `.env`:
  - `MS_LOGIN` (или `LOGIN`)
  - `MS_PASSWORD` (или `PASSWORD`)
  - `ATTRIBUTE_ID` (опционально)
  - `COMPANY`, `MERCHANT_ID` (опционально)

Инструкция:

1. Создайте файл `.env` в корне (добавьте его в `.gitignore`):

```
MS_LOGIN=your_login
MS_PASSWORD=your_password
ATTRIBUTE_ID=optional-attribute-uuid
COMPANY=Your Company
MERCHANT_ID=123456
```

2. Локальный запуск генерации:

```
python cloud_run.py
```

3. Hourly публикация через GitHub Actions — workflow добавлен в `.github/workflows/generate-xml.yml`. Он запускает генерацию и коммитит `docs/kaspi.xml`.

Secrets for GitHub Actions:
- Для автоматического коммита workflow использует `GITHUB_TOKEN` (предоставляется автоматически).

