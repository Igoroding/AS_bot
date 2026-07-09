"""LLM-слой: мэтчинг текста юзера с категориями WB + фильтрация ниш."""
import httpx
import json
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL


async def _llm_call(system_prompt: str, user_prompt: str, max_tokens: int = 800, temperature: float = 0.3) -> str:
    """Общий вызов LLM через Polza.ai API."""
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{LLM_BASE_URL}/chat/completions", headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()


async def match_categories(user_text: str, available_categories: list[str]) -> list[str]:
    """
    Принимает свободный текст юзера и список категорий WB.
    Возвращает список подходящих категорий (макс. 5).
    """
    if not LLM_API_KEY:
        # Fallback: простой substring-поиск
        text_lower = user_text.lower()
        return [c for c in available_categories if any(w in c.lower() for w in text_lower.split())][:5]

    cats_text = "\n".join(available_categories)
    prompt = (
        f"Пользователь ищет ниши на Wildberries. Его запрос: «{user_text}».\n"
        f"Доступные категории WB:\n{cats_text}\n\n"
        f"Выбери до 5 наиболее подходящих категорий. Верни только названия категорий, "
        f"каждую с новой строки, без нумерации и пояснений."
    )

    content = await _llm_call(
        "Ты — помощник для подбора категорий товаров на Wildberries. Отвечай кратко, только названия категорий.",
        prompt,
        max_tokens=1000,
    )

    # Парсим ответ — каждая строка = категория
    result = [line.strip() for line in content.split("\n") if line.strip()]
    # Фильтруем только существующие категории
    cats_lower = {c.lower(): c for c in available_categories}
    matched = []
    for r in result:
        if r.lower() in cats_lower:
            matched.append(cats_lower[r.lower()])
    return matched[:5] if matched else result[:5]


async def filter_niches_by_semantic(user_text: str, niches_data: list[dict]) -> list[dict]:
    """
    Семантический фильтр: LLM получает поисковые запросы и текст юзера,
    отбирает только те, которые ТОЧНО соответствуют смыслу запроса.
    Например: «зелень» → укроп, петрушка, базилик, салат — но НЕ ашваганда, НЕ цветы.

    Каждая ниша: {"query": str, "requests": int, "products": int, "competition": float}
    """
    if not LLM_API_KEY or not niches_data:
        return niches_data

    queries_text = "\n".join(f"{i+1}. {n['query']}" for i, n in enumerate(niches_data[:100]))
    prompt = (
        f"Пользователь ищет: «{user_text}».\n"
        f"Список поисковых запросов на WB:\n{queries_text}\n\n"
        f"Отбери ТОЛЬКО те запросы, которые по СМЫСЛУ соответствуют тому, что ищет пользователь. "
        f"Будь строгим: «зелень» = укроп/петрушка/базилик/салат, но НЕ ашваганда/цветы/таба. "
        f"Верни номера подходящих запросов через запятую, без пояснений. "
        f"Если ничего не подходит — верни 0."
    )

    content = await _llm_call(
        "Ты — семантический фильтр поисковых запросов на Wildberries. Отвечай только номерами через запятую.",
        prompt,
        max_tokens=600,
        temperature=0.1,
    )

    try:
        nums = [int(x.strip()) for x in content.split(",") if x.strip().isdigit()]
        if 0 in nums:
            return []
        return [niches_data[i - 1] for i in nums if 1 <= i <= len(niches_data)]
    except Exception:
        return niches_data  # при ошибке — возвращаем без фильтрации


async def filter_niches_by_text(user_text: str, niches_data: list[dict]) -> list[dict]:
    """
    Принимает уточняющий текст юзера (например «не большие размеры»)
    и список ниш. Возвращает отфильтрованный список.

    Каждая ниша в niches_data: {"query": str, "requests": int, "products": int, "competition": float}
    """
    if not LLM_API_KEY:
        return niches_data

    # Если ниш слишком много — отправляем только ключевые фразы
    queries_text = "\n".join(f"{i+1}. {n['query']}" for i, n in enumerate(niches_data[:100]))
    prompt = (
        f"Пользователь уточняет: «{user_text}».\n"
        f"Список ключевых фраз ниш:\n{queries_text}\n\n"
        f"Верни номера фраз, которые ПОДХОДЯТ под уточнение (через запятую). "
        f"Если уточнение содержит отрицание («не», «без»), исключи неподходящие. "
        f"Только номера через запятую, без пояснений."
    )

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "Ты — фильтр для поисковых запросов на Wildberries. Отвечай только номерами через запятую."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 200,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{LLM_BASE_URL}/chat/completions", headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()

    # Парсим номера
    try:
        nums = [int(x.strip()) for x in content.split(",") if x.strip().isdigit()]
        return [niches_data[i - 1] for i in nums if 1 <= i <= len(niches_data)]
    except Exception:
        return niches_data  # при ошибке — возвращаем без фильтрации