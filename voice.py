"""Распознавание голосовых сообщений через Polza.ai Whisper API."""
import httpx
from config import LLM_API_KEY

WHISPER_URL = "https://api.polza.ai/api/v1/audio/transcriptions"
WHISPER_MODEL = "openai/whisper-1"


async def transcribe_audio(audio_data: bytes, filename: str = "voice.ogg") -> str | None:
    """
    Принимает аудио (байты) и отправляет в Polza.ai Whisper.
    Возвращает распознанный текст или None при ошибке.
    Фильтрует галлюцинации Whisper на неречевом аудио.
    """
    if not LLM_API_KEY:
        return None

    # Ранняя проверка: пустой или слишком маленький файл
    if not audio_data or len(audio_data) < 100:
        return None

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
    }
    files = {
        "file": (filename, audio_data, "audio/ogg"),
    }
    data = {
        "model": WHISPER_MODEL,
        "language": "ru",
        "response_format": "json",
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(WHISPER_URL, headers=headers, files=files, data=data)
            resp.raise_for_status()
            result = resp.json()
            text = result.get("text", "").strip()
            
            # Фильтрация галлюцинаций Whisper:
            # - слишком короткий текст (< 2 символов)
            # - только ASCII (эмодзи, латиница — для русскоязычного бота подозрительно)
            # - только цифры/спецсимволы
            # - типичные галлюцинации Whisper на неречевом аудио (blacklist фраз)
            if not text or len(text) < 2:
                return None
            
            # Blacklist типичных галлюцинаций Whisper
            hallucination_phrases = [
                "редактор субтитров", "синецкая", "егорова", "корректор",
                "спасибо за внимание", "подписывайтесь на канал",
                "вы смотрели", "оставайтесь с нами", "до новых встреч",
                "субтитры добавил", "переводчик", "озвучка",
            ]
            text_lower = text.lower()
            if any(phrase in text_lower for phrase in hallucination_phrases):
                return None
            
            # Проверяем: есть ли кириллица или хотя бы осмысленные слова
            has_cyrillic = any('\u0400' <= c <= '\u04ff' for c in text)
            has_latin_words = any(w.isalpha() and len(w) >= 3 for w in text.split())
            
            if not has_cyrillic and not has_latin_words:
                # Только эмодзи/цифры/спецсимволы — галлюцинация
                return None
            
            return text
    except Exception as e:
        import logging
        logging.error(f"Whisper transcription error: {e}")
        return None