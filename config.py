"""Конфигурация бота. Ключи вставит Игорь."""
import os

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN", "8736323533:AAFLEASGua8GgJfqVkjR8ksG6hnuFtTg9cw")

# LLM через Polza.ai (агрегатор, OpenAI-совместимый API)
LLM_API_KEY = os.getenv("LLM_API_KEY", "pza_BziHZ-oEJPKj0Cuc9-4tqs_Qykwp6Veg")
LLM_BASE_URL = "https://api.polza.ai/v1"
LLM_MODEL = "z-ai/glm-4.7-flash"  # дешёвая flash-модель, ~0.0005 ₽/запрос

# Лимиты
DAILY_USER_LIMIT = 100

# Данные
DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "wb_seeds_export.xlsx")

# Логирование
LOG_DB = os.path.join(os.path.dirname(__file__), "bot_logs.db")