import os
from pathlib import Path

BOT_TOKEN = os.getenv("BOT_TOKEN", "8837628673:AAE2lLCK5Of3PgKoAakUYvvxrq07hmtU8kU")
DATA_DIR = Path(__file__).with_name("data")
MEDIA_URL = "https://t.me/nightmaredlcmedia"
PREMIUM_EMOJI = True
EMOJI_HELLO = "5985478698722136468"
EMOJI_QUESTION = "6034969813032374911"
EMOJI_MEDIA = "6028171274939797252"
TOPIC_IDLE_HOURS = 48
CLOSED_TOPIC_DELETE_MINUTES = 10
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_PROVIDER = os.getenv("AI_PROVIDER", "custom")
AI_BASE_URL = os.getenv("AI_BASE_URL", "")
AI_MODEL = os.getenv("AI_MODEL", "")
AI_MODE = "auto"
AI_PROMPT = "Отвечай коротко и вежливо на русском, обычным текстом без Markdown. Не выдумывай правила, цены, ссылки или решения. Если данных недостаточно, задай уточняющий вопрос. Не утверждай, что выполнил действие, проверил аккаунт или снял блокировку."
AI_KNOWLEDGE = "По вопросам медиа направляй пользователя в https://t.me/nightmaredlcmedia."
