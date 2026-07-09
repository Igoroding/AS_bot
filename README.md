# WB Trends Bot

Telegram-бот для поиска свободных ниш на Wildberries.

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Настройка

```bash
export BOT_TOKEN="твой_токен_от_botfather"
export LLM_API_KEY="твой_ключ_от_polza.ai"
```

## Запуск

```bash
python3 bot.py
```

## Структура

- `bot.py` — точка входа, aiogram 3
- `config.py` — конфигурация (токены, лимиты, пути)
- `handlers/` — обработчики Telegram-сообщений
- `filters/niche_loader.py` — загрузка и фильтрация ниш из xlsx
- `llm.py` — GLM Flash: мэтчинг категорий + фильтрация ниш
- `database.py` — SQLite логирование действий + лимиты
- `data/` — xlsx-файлы с данными WB

## MVP-функции

- Поиск ниш по свободному тексту через LLM
- Фильтр: конкуренция ≤5%, запросов ≥500
- Выдача по 10 ниш, кнопка «Далее»
- Ссылка на поисковую выдачу WB
- Уточняющий фильтр текстом («не большие размеры»)
- Логирование действий в SQLite
- Лимит 100 запросов/день на юзера