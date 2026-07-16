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
- min_requests: «от N запросов», «более N запросов», «запросов больше N», «хороший спрос хотя бы N», «спрос от N», «от N» (если N большое, явно спрос) → N
- max_competition: «конкуренция до N», «конкуренция не выше N», «конкуренция меньше N», «не очень большая конкуренция» → 5
- sort_by: «по запросам»→"requests", «по конкуренции»→"competition", «по товарам»→"products". ЕСЛИ пользователь явно сказал «сортировка по X» — обязательно верни X, не игнорируй.
- search_text: что искать. Если пользователь написал категорию (например «садоводство», «одежда», «спорт») — оставь её в search_text как есть, не обнуляй.
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
        # GPT-4o может вернуть ```json ... ```
        if "```json" in content:
            content = content.split("```json", 1)[1].split("```", 1)[0]
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
    Сначала substring-поиск по ключевым словам, потом LLM для уточнения.
    Возвращает список подходящих категорий (макс. 5).
    """
    if not _is_valid_query(user_text):
        return []

    # Шаг 1: substring-поиск по словам из запроса
    text_lower = user_text.lower()
    words = [w for w in text_lower.split() if len(w) > 2]

    # Строим префиксы для каждого слова (от 3 букв до полного слова)
    prefixes = set()
    for w in words:
        for n in range(3, len(w) + 1):
            prefixes.add(w[:n])

    # Ищем категории, содержащие любое из слов или префиксов
    candidate_cats = []
    for c in available_categories:
        c_lower = c.lower()
        if any(w in c_lower for w in words) or any(p in c_lower for p in prefixes):
            candidate_cats.append(c)

    # Если нашли мало — расширяем поиск по отдельным буквам слов
    if len(candidate_cats) < 3:
        for c in available_categories:
            c_lower = c.lower()
            if any(w[0] in c_lower for w in words if w):
                candidate_cats.append(c)

    candidate_cats = list(dict.fromkeys(candidate_cats))  # уникальные, сохраняя порядок

    # Шаг 2: если кандидатов мало — возвращаем сразу
    if len(candidate_cats) <= 5:
        return candidate_cats[:5]

    # Шаг 3: если кандидатов много — LLM уточняет
    if not LLM_API_KEY:
        return candidate_cats[:5]

    cats_text = "\n".join(candidate_cats[:30])  # передаём только топ-30 кандидатов
    prompt = (
        f"Пользователь ищет ниши на Wildberries. Его запрос: «{user_text}».\n"
        f"Вот подходящие категории (выбери до 5 самых релевантных):\n{cats_text}\n\n"
        f"Верни только названия категорий, каждую с новой строки, без нумерации и пояснений. "
        f"Если ничего не подходит — верни пустоту."
    )

    try:
        content = await _llm_call(
            "Ты — помощник для подбора категорий товаров на Wildberries. Отвечай кратко, только названия категорий.",
            prompt,
            max_tokens=200,
        )
    except Exception:
        import logging
        logging.error("match_categories: LLM error, returning substring candidates")
        return candidate_cats[:5]

    result = [line.strip() for line in content.split("\n") if line.strip()]
    cats_lower = {c.lower(): c for c in available_categories}
    matched = []
    for r in result:
        if r.lower() in cats_lower:
            matched.append(cats_lower[r.lower()])
    return matched[:5] or candidate_cats[:5]


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


async def group_niches_into_products(niches_data: list[dict]) -> list[dict]:
    """
    Группирует ключевые фразы в товары через LLM.
    Возвращает список товаров, каждый с полями:
      product_name: str — название товара
      phrases: list[dict] — ключевые фразы этого товара
    """
    if not LLM_API_KEY or not niches_data:
        return []

    phrases_text = "\n".join(
        f"{i+1}. {n['query']} (запросов: {n['requests']}, товаров: {n['products']}, конкуренция: {n['competition']:.2f}%)"
        for i, n in enumerate(niches_data[:50])
    )

    prompt = (
        f"Сгруппируй эти поисковые фразы Wildberries в товары. Один товар может объединять несколько похожих фраз.\n\n"
        f"{phrases_text}\n\n"
        f"Верни ТОЛЬКО JSON-массив (без markdown, без пояснений):\n"
        f'[{{"product_name": "Название товара", "phrase_indices": [1, 3, 5]}}, ...]\n\n'
        f"Правила:\n"
        f"- phrase_indices — номера фраз из списка выше, которые относятся к этому товару\n"
        f"- Каждая фраза должна быть ровно в одном товаре\n"
        f"- АГРЕССИВНО объединяй похожие фразы в один товар. «Компостер садовый», «компостер для дачи», «компостер пластиковый», «ящик для компоста» — это ОДИН товар\n"
        f"- «Спиннинг ультралайт» и «спиннинг для джига» — ОДИН товар. «Катушка для спиннинга» и «катушка безынерционная» — ОДИН товар\n"
        f"- Название товара — КОНКРЕТНОЕ, КОММЕРЧЕСКОЕ, БЕЗ ВЫДУМАННЫХ БРЕНДОВ. Не «ProCast», не «Трофейный Удар». Просто «Безынерционная катушка для спиннинга» или «Спиннинг ультралайт»\n"
        f"- ЕСЛИ все фразы содержат название одного бренда (zarina, love republic, befree, lalis и т.д.) — НЕ делай из них отдельный товар. Это брендовые запросы, не свободная ниша\n"
        f"- Максимум 5 товаров, сортируй по суммарному спросу (самые востребованные — первыми)"
    )

    try:
        content = await _llm_call(
            "Ты — аналитик Wildberries. Группируешь поисковые фразы в товары. Отвечай только валидным JSON.",
            prompt,
            max_tokens=600,
            temperature=0.2,
        )
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content
            content = content.rsplit("```", 1)[0]
        if "```json" in content:
            content = content.split("```json", 1)[1].split("```", 1)[0]
        content = content.strip()
        if not content.startswith("["):
            import re
            match = re.search(r'\[.*\]', content, re.DOTALL)
            if match:
                content = match.group(0)

        import json
        groups = json.loads(content)
        if not isinstance(groups, list):
            return []

        result = []
        for g in groups:
            indices = g.get("phrase_indices", [])
            phrases = [niches_data[i - 1] for i in indices if 1 <= i <= len(niches_data)]
            if phrases:
                result.append({
                    "product_name": g.get("product_name", "Товар"),
                    "phrases": phrases,
                })
        return result
    except Exception:
        import logging
        logging.error("group_niches_into_products: LLM error")
        return []


async def analyze_product(product_name: str, phrases: list[dict]) -> str:
    """
    Анализирует товар: размер/вес, удобство для маркетплейсов, Честный знак,
    сертификаты, бренды в запросах.
    Возвращает форматированный текст аналитики.
    """
    if not LLM_API_KEY:
        return ""

    phrases_text = "\n".join(
        f"- {p['query']} ({p['requests']:,} запросов/мес)"
        for p in phrases[:10]
    )

    prompt = (
        f"Товар: «{product_name}»\n"
        f"Ключевые фразы (рынки):\n{phrases_text}\n\n"
        f"Дай короткую аналитику для продавца на Wildberries. Ответь строго по пунктам, каждый с новой строки:\n\n"
        f"📦 Размер и вес: (примерная оценка — маленький/средний/крупный, лёгкий/тяжёлый, насколько удобно хранить и возить)\n"
        f"🚚 Удобство для маркетплейсов: (логистика — бьётся ли, занимает ли много места на складе, высокий ли риск возвратов)\n"
        f"🏷 Честный знак: нужен или нет (да/нет, кратко почему)\n"
        f"📜 Сертификаты: нужны или нет (да/нет, какие именно если да)\n"
        f"™ Бренды в запросах: есть ли брендовые запросы среди фраз (да/нет, какие бренды если есть, риск блокировки)\n\n"
        f"Пиши коротко, по делу. Без вступления и заключения. Только эти 5 пунктов."
    )

    try:
        content = await _llm_call(
            "Ты — эксперт по товарам для Wildberries. Отвечай коротко, строго по пунктам.",
            prompt,
            max_tokens=1000,
            temperature=0.3,
        )
        return content.strip()
    except Exception:
        import logging
        logging.error("analyze_product: LLM error")
        return ""


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