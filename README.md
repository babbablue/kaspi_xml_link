# kaspi_xml_link

Проект генерирует XML в формате Kaspi и публикует `docs/kaspi.xml` для использования через GitHub Pages.

## 🚀 Быстрый старт

### Локальный запуск:

1. **Установите зависимости:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Создайте `.env` файл:**
   ```env
   MS_LOGIN=your_login
   MS_PASSWORD=your_password
   ATTRIBUTE_ID=14858c5a-ccb7-11ef-0a80-08a200511bcd
   PRICE_ATTRIBUTE_ID=fc15ca1c-d188-4088-87b1-44a749aece17
   COMPANY=ИП ВОЗРОЖДЕНИЕ
   MERCHANT_ID=30286450
   ```

3. **Запустите генерацию:**
   ```bash
   python cloud_run.py
   ```

4. **Или запустите веб-сервер:**
   ```bash
   python kaspi_xml_sync.py
   ```
   Затем откройте http://localhost:5000/xml

## Ключевые моменты:

- Для публикации XML используется GitHub Pages (папка `docs/`).
- Чувствительные данные (логин/пароль для MoySklad) берутся из переменных окружения или `.env`:
  - `MS_LOGIN` (или `LOGIN`)
  - `MS_PASSWORD` (или `PASSWORD`)
  - `ATTRIBUTE_ID` (UUID чекбокса "Выгружать на Каспи?")
  - `PRICE_ATTRIBUTE_ID` (UUID поля "Каспи" для цены)
  - `COMPANY`, `MERCHANT_ID` (опционально)

## Инструкция:

### 1. Настройка переменных окружения

Создайте файл `.env` в корне проекта (файл уже добавлен в `.gitignore`):

```env
MS_LOGIN=your_login
MS_PASSWORD=your_password
ATTRIBUTE_ID=14858c5a-ccb7-11ef-0a80-08a200511bcd
PRICE_ATTRIBUTE_ID=fc15ca1c-d188-4088-87b1-44a749aece17
COMPANY=ИП ВОЗРОЖДЕНИЕ
MERCHANT_ID=30286450
```

Или используйте переменные окружения системы.

### 2. Поиск UUID атрибутов

Для получения UUID полей используйте скрипт:

```bash
python find_attributes.py
```

### 3. Локальный запуск генерации:

```bash
python cloud_run.py
```

### 4. Автоматическая публикация

Hourly публикация через GitHub Actions настроена в `.github/workflows/generate-xml.yml`. Workflow:
- Запускается каждый час
- Генерирует XML с актуальными данными
- Коммитит `docs/kaspi.xml`
- Публикует через GitHub Pages

### 5. GitHub Secrets

Для GitHub Actions установите следующие secrets:
- `MS_LOGIN`
- `MS_PASSWORD`
- `ATTRIBUTE_ID`
- `PRICE_ATTRIBUTE_ID`
- `COMPANY`
- `MERCHANT_ID`

### 6. Локальное тестирование сервера

**Полноценный режим с веб-интерфейсом:**

```bash
python kaspi_xml_sync.py
```

- Сервер запустится на http://localhost:5000
- Доступные endpoints:
  - `http://localhost:5000/xml` - текущий XML файл
  - `http://localhost:5000/control/status` - статус сервера
  - `http://localhost:5000/control/generate` - принудительная генерация
  - `http://localhost:5000/control/schedule?minutes=30` - изменить интервал

**Режим генерации без сервера:**

```bash
python cloud_run.py
```

- Просто генерирует XML и сохраняет в `kaspi.xml`
- Подходит для разового запуска
- Используется в GitHub Actions

## Структура XML:

```xml
<kaspi_catalog xmlns="kaspiShopping" date="YYYY-MM-DD">
  <company>ИП "ВОЗРОЖДЕНИЕ"</company>
  <merchantid>30286450</merchantid>
  <offers>
    <offer sku="123">
      <model>Название товара</model>
      <brand>Бренд</brand>
      <availabilities>
        <availability available="yes" storeId="PP1" stockCount="5"/>
      </availabilities>
      <price>1500</price>
    </offer>
  </offers>
</kaspi_catalog>
```
