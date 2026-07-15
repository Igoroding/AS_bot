"""LLM-слой: мэтчинг текста юзера с категориями WB + фильтрация ниш."""
import httpx
import json
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL


async def _llm_call(system_prompt: str, user_prompt: str, max_tokens: int = 800, temperature: float = 0.3) -> str:
    """Общий вызов LLM через Polza.ai API. Бросает исключения при ошибках."""
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


def _is_valid_query(text: str) -> bool:
    """Проверка: запрос осмысленный (не пустой, не бессмысленный набор букв)."""
    text = text.strip()
    if len(text) < 2:
        return False
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 2:
        return False
    unique = set(text.lower().replace(" ", ""))
    if len(unique) <= 2 and len(text) > 3:
        return False
    return True


async def parse_query_params(user_text: str) -> dict | None:
    """
    Извлекает параметры фильтрации из естественного языка.
    Возвращает dict с ключами:
      search_text: str — что искать (без параметров)
      categories: list[str] — названия категорий если указаны
      max_products: int | None
      min_requests: int | None
      max_competition: float | None
      sort_by: str — "requests" | "competition" | "products" (default: "requests")
    Или None если LLM недоступен.
    """
    if not LLM_API_KEY or not _is_valid_query(user_text):
        return None

    prompt = f"""Проанализируй запрос пользователя для поиска ниш на Wildberries:
«{user_text}»

Извлеки параметры фильтрации и верни ТОЛЬКО JSON (без markdown, без пояснений):
{{
  "search_text": "что искать — без числовых параметров и условий",
  "categories": ["названия категорий если пользователь указал конкретные"],
  "max_products": null или число,
  "min_requests": null или число,
  "max_competition": null или число,
  "sort_by": "requests"
}}

Правила:
- max_products: «не более N карточек», «до N товаров», «максимум N товаров» → N
- min_requests: «от N запросов», «более N запросов», «запросов больше N», «хороший спрос хотя бы N» → N
- max_competition: «конкуренция до N», «конкуренция не выше N», «конкуренция меньше N», «не очень большая конкуренция» → 5
- sort_by: «по запросам»→"requests", «по конкуренции»→"competition", «по товарам»→"products"
- search_text: конкретный товар или категория БЕЗ параметров. Если пользователь НЕ указал конкретный товар (например «найди хорошие ниши», «покажи все ниши») — верни пустую строку ""
- Если параметр не указан — null
- categories: пустой список если не указаны"""

    try:
        content = await _llm_call(
            "Ты — парсер параметров поиска для Wildberries. Отвечай только валидным JSON.",
            prompt,
            max_tokens=300,
            temperature=0.0,
        )
        # Убираем markdown если LLM добавил
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content
            content = content.rsplit("```", 1)[0]
        content = content.strip()
        
        params = json.loads(content)
        # Валидация
        if not isinstance(params, dict):
            return None
        return params
    except Exception as e:
        import logging
        logging.error(f"parse_query_params error: {e}")
        return None


async def match_categories(user_text: str, available_categories: list[str]) -> list[str]:
    """
    Принимает свободный текст юзера и список категорий WB.
    Возвращает список подходящих категорий (макс. 5).
    """
    if not _is_valid_query(user_text):
        return []

    if not LLM_API_KEY:
        text_lower = user_text.lower()
        return [c for c in available_categories if any(w in c.lower() for w in text_lower.split())][:5]

    cats_text = "\n".join(available_categories)
    prompt = (
        f"Пользователь ищет ниши на Wildberries. Его запрос: «{user_text}».\n"
        f"Доступные категории WB:\n{cats_text}\n\n"
        f"Выбери до 5 наиболее подходящих категорий. Верни только названия категорий, "
        f"каждую с новой строки, без нумерации и пояснений. Если ничего не подходит — верни пустоту."
    )

    try:
        content = await _llm_call(
            "Ты — помощник для подбора категорий товаров на Wildberries. Отвечай кратко, только названия категорий.",
            prompt,
            max_tokens=200,
        )
    except Exception:
        import logging
        logging.error("match_categories: LLM error, falling back to substring")
        text_lower = user_text.lower()
        return [c for c in available_categories if any(w in c.lower() for w in text_lower.split())][:5]

    result = [line.strip() for line in content.split("\n") if line.strip()]
    cats_lower = {c.lower(): c for c in available_categories}
    matched = []
    for r in result:
        if r.lower() in cats_lower:
            matched.append(cats_lower[r.lower()])
    return matched[:5]


async def filter_niches_by_semantic(user_text: str, niches_data: list[dict]) -> list[dict] | None:
    """
    Семантический фильтр: LLM отбирает ниши по смыслу запроса.
    Возвращает None (пропустить), [] (ничего), [...] (найдено).
    """
    if not LLM_API_KEY or not niches_data:
        return None

    queries_text = "\n".join(f"{i+1}. {n['query']}" for i, n in enumerate(niches_data[:100]))
    prompt = (
        f"Пользователь ищет: «{user_text}».\n"
        f"Список поисковых запросов на WB:\n{queries_text}\n\n"
        f"Отбери ТОЛЬКО те запросы, которые по СМЫСЛУ соответствуют тому, что ищет пользователь. "
        f"Будь строгим: «зелень» = укроп/петрушка/базилик/салат, но НЕ ашваганда/цветы/таба. "
        f"Верни номера подходящих запросов через запятую, без пояснений. "
        f"Если ничего не подходит — верни 0."
    )

    try:
        content = await _llm_call(
            "Ты — семантический фильтр поисковых запросов на Wildberries. Отвечай только номерами через запятую.",
            prompt,
            max_tokens=200,
            temperature=0.1,
        )
    except Exception:
        import logging
        logging.error("filter_niches_by_semantic: LLM error, skipping filter")
        return None

    try:
        if not content.strip():
            return None
        nums = [int(x.strip()) for x in content.split(",") if x.strip().isdigit()]
        if 0 in nums:
            return []
        return [niches_data[i - 1] for i in nums if 1 <= i <= len(niches_data)]
    except Exception:
        return None


async def filter_niches_by_text(user_text: str, niches_data: list[dict]) -> list[dict]:
    """
    Уточняющий фильтр для последующего запроса (например «не большие размеры»).
    """
    if not LLM_API_KEY or not niches_data:
        return niches_data

    queries_text = "\n".join(f"{i+1}. {n['query']}" for i, n in enumerate(niches_data[:100]))
    prompt = (
        f"Пользователь уточняет: «{user_text}».\n"
        f"Список ключевых фраз ниш:\n{queries_text}\n\n"
        f"Верни номера фраз, которые ПОДХОДЯТ под уточнение (через запятую). "
        f"Если уточнение содержит отрицание («не», «без»), исключи неподходящие. "
        f"Только номера через запятую, без пояснений."
    )

    try:
        content = await _llm_call(
            "Ты — фильтр для поисковых запросов на Wildberries. Отвечай только номерами через запятую.",
            prompt,
            max_tokens=200,
            temperature=0.2,
        )
        nums = [int(x.strip()) for x in content.split(",") if x.strip().isdigit()]
        return [niches_data[i - 1] for i in nums if 1 <= i <= len(niches_data)]
    except Exception:
        import logging
        logging.error("filter_niches_by_text: LLM error, returning unfiltered")
        return niches_data