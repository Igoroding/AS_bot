"""Распознавание голосовых сообщений через Polza.ai Whisper API."""
import httpx
from config import LLM_API_KEY

# Whisper endpoint (обрати внимание: /api/v1/, не /v1/)
WHISPER_URL = "https://api.polza.ai/api/v1/audio/transcriptions"
WHISPER_MODEL = "openai/whisper-1"


async def transcribe_audio(audio_data: bytes, filename: str = "voice.ogg") -> str | None:
    """
    Принимает аудио (байты) и отправляет в Polza.ai Whisper.
    Возвращает распознанный текст или None при ошибке.
    """
    if not LLM_API_KEY:
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
            return result.get("text", "").strip()
    except Exception as e:
        import logging
        logging.error(f"Whisper transcription error: {e}")
        return None