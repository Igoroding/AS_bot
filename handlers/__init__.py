"""Обработчики сообщений Telegram-бота."""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from urllib.parse import quote
import sqlite3
import os

from config import DAILY_USER_LIMIT
from database import log_action, check_and_increment_usage, init_db
from filters.niche_loader import Niche, format_niche, wb_search_url
from llm import parse_query_params, filter_niches_by_text, analyze_product
from voice import transcribe_audio

router = Router()

# Глобальный кэш ниш (загружается при старте)
_niches_cache: list[Niche] = []
_niches_by_category: dict[str, list[Niche]] = {}
_user_state: dict[int, dict] = {}  # user_id → {niches: [...], offset: int}

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "wb_trends.db")


def get_niches() -> list[Niche]:
    global _niches_cache
    if not _niches_cache:
        _niches_cache = load_niches()
    return _niches_cache


def get_niches_by_category() -> dict[str, list[Niche]]:
    global _niches_by_category
    if not _niches_by_category:
        niches = get_niches()
        cats = get_categories(niches)
        for cat in cats:
            _niches_by_category[cat] = filter_by_category(niches, cat)
    return _niches_by_category


def _query_sqlite(params: dict) -> list[Niche]:
    """Единый SQL-запрос: числовые фильтры + LIKE по search_text."""
    if not os.path.exists(DB_PATH):
        return []

    where_parts = ["request_count >= 500", "cards_count > 0"]
    args = []

    # Конкуренция (по умолчанию 15%)
    max_comp = 15.0
    if params.get("max_competition") is not None:
        max_comp = float(params["max_competition"])
    where_parts.append("competition <= ?")
    args.append(max_comp)

    if params.get("max_products") is not None:
        where_parts.append("cards_count <= ?")
        args.append(int(params["max_products"]))

    if params.get("min_requests") is not None:
        where_parts.append("request_count >= ?")
        args.append(int(params["min_requests"]))

    # search_text — LIKE по category ИЛИ phrase (по корню слова, первые 5 букв)
    search_text = params.get("search_text", "")
    if search_text and search_text.strip():
        # Стоп-слова, которые не ищем в БД
        stop_words = {"привет", "здравствуйте", "здарова", "торгую", "продаю", "хочу", "есть", "что", "как",
                      "все", "если", "там", "или", "по", "на", "для", "без", "не", "и", "в", "с", "о", "а",
                      "но", "то", "из", "у", "же", "бы", "ли", "до", "от", "про", "под", "над", "об",
                      "может", "нужно", "надо", "можно", "самые", "самое", "самый", "какой", "какие",
                      "покажи", "найди", "подскажите", "посоветуйте", "интересует", "интересуют",
                      "расшириться", "новенькое", "новые", "новую", "новых", "необычное",
                      "конкуренция", "конкурентно", "конкурентные", "большая", "адская", "небольшая",
                      "ниши", "нишу", "ниш", "товары", "товар", "товаров", "запросов", "запроса",
                      "спросом", "спрос", "тыс", "много", "мало", "сейчас", "тестирую", "работает",
                      "бизнес", "маркетплейс", "вайлдберриз", "wildberries", "wb",
                      "здарова", "тестирую", "бота", "можешь", "хочется", "хендмейд", "эко",
                      "недорогие", "одежду", "интерьеру", "вазы", "статуэтки",
                      "развивающие", "сортеры", "кубики", "пазлы", "коляски", "автокресла",
                      "датчики", "розетки", "лампочки", "умному", "дому",
                      "спиннингам", "катушкам", "снастями", "интересного",
                      "конкретный", "собак", "сортировка", "свободное",
                      "горячие", "ограничений", "просто"}
        words = [w.strip().lower().strip("!?,.;:-—\"'()[]{}«»") for w in search_text.split() if len(w.strip().strip("!?,.;:-—\"'()[]{}«»")) > 2 and w.strip().lower().strip("!?,.;:-—\"'()[]{}«»") not in stop_words]
        # Если значимых слов меньше 2 — не фильтруем по LIKE, возвращаем все ниши
        if len(words) >= 2:
            # Берём корень слова (первые 5 букв) для поиска по падежам
            like_clauses = " OR ".join(
                f"(LOWER(category) LIKE ? OR LOWER(phrase) LIKE ?)"
                for _ in words
            )
            where_parts.append(f"({like_clauses})")
            for w in words:
                root = w[:4]  # первые 4 буквы — корень для русского языка (рыбалка→рыба, наушники→науш)
                args.extend([f"%{root}%", f"%{root}%"])
        else:
            # Если после фильтрации не осталось слов — ищем все ниши (без LIKE)
            pass

    sort_by = params.get("sort_by", "requests")
    sort_map = {"requests": "request_count", "competition": "competition", "products": "cards_count"}
    sort_col = sort_map.get(sort_by, "request_count")

    where = " AND ".join(where_parts)
    sql = f"SELECT phrase, request_count, cards_count, competition FROM niches WHERE {where} ORDER BY {sort_col} DESC LIMIT 200"

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(sql, args)
    result = []
    for row in c:
        result.append(Niche(
            query=row["phrase"],
            requests=row["request_count"],
            products=row["cards_count"],
            competition=row["competition"],
        ))
    conn.close()
    return result


@router.message(CommandStart())
async def cmd_start(message: Message):
    init_db()
    await message.answer(
        "👋 Привет! Я бот для поиска свободных ниш на Wildberries.\n\n"
        "Опиши, что ты ищешь — например:\n"
        "«компостер не более 100 карточек»\n"
        "«укроп, запросов от 5000»\n"
        "«платья, конкуренция до 2»\n\n"
        "Можно и голосовым! 🎙\n\n"
        "Параметры:\n"
        "· «не более N карточек» — лимит товаров\n"
        "· «от N запросов» — минимальный спрос\n"
        "· «конкуренция до N» — макс. конкуренция\n"
        "· «сортировка по запросам/конкуренции/товарам»"
    )
    log_action(message.from_user.id, "start")


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📝 Как пользоваться:\n\n"
        "1. Опиши, что ищешь — текстом или голосовым 🎙\n"
        "2. Можно добавить фильтры:\n"
        "   · «не более 100 карточек»\n"
        "   · «от 5000 запросов»\n"
        "   · «конкуренция до 2»\n"
        "   · «сортировка по конкуренции»\n"
        "3. Уточни результат — «не пластиковые», «без брендов»\n"
        "4. /new — сбросить и начать новый поиск\n\n"
        f"Лимит: {DAILY_USER_LIMIT} запросов в день.\n\n"
        "💡 Каждый новый запрос (не уточнение) ищет по всей базе заново."
    )
    log_action(message.from_user.id, "help")


@router.message(Command("new"))
async def cmd_new(message: Message):
    """Сбрасывает состояние и начинает новый поиск."""
    user_id = message.from_user.id
    _user_state.pop(user_id, None)
    await message.answer("✅ Готов к новому поиску! Напиши, что ищешь.")
    log_action(user_id, "new_search")


@router.message(F.voice)
async def handle_voice(message: Message):
    """Обрабатывает голосовые сообщения: распознаёт речь и передаёт в поиск."""
    user_id = message.from_user.id

    if not check_and_increment_usage(user_id, DAILY_USER_LIMIT):
        await message.answer("⚠️ Дневной лимит запросов исчерпан. Возвращайся завтра!")
        return

    await message.answer("🎤 Распознаю голосовое сообщение...")
    try:
        file = await message.bot.get_file(message.voice.file_id)
        audio_data = await message.bot.download_file(file.file_path)
        audio_bytes = audio_data.read()
        import logging
        logging.info(f"Voice file size: {len(audio_bytes)} bytes, file_path: {file.file_path}")
    except Exception as e:
        import logging
        error_msg = str(e)
        logging.error(f"Failed to download voice: {error_msg}")
        if "too large" in error_msg.lower() or "20 MB" in error_msg:
            await message.answer("❌ Файл слишком большой (лимит 20 МБ). Запиши короче — до 30 секунд.")
        else:
            await message.answer(f"❌ Не удалось скачать голосовое: {error_msg[:100]}")
        return

    transcribed = await transcribe_audio(audio_bytes, "voice.ogg")
    if not transcribed:
        await message.answer("❌ Не удалось распознать речь. Попробуй записать чётче или напиши текстом.")
        return

    log_action(user_id, "voice_transcribed", transcribed)
    await message.answer(f"🎙 Распознал: «{transcribed}»\n🔍 Ищу ниши...")

    await _process_query(message, user_id, transcribed, _voice_mode=True)


@router.message()
async def handle_text(message: Message, _voice_mode: bool = False):
    user_id = message.from_user.id
    text = message.text.strip()
    await _process_query(message, user_id, text, _voice_mode=_voice_mode)


async def _process_query(message: Message, user_id: int, text: str, _voice_mode: bool = False):

    # Проверка лимита (пропускаем если уже проверили в handle_voice)
    if not _voice_mode:
        if not check_and_increment_usage(user_id, DAILY_USER_LIMIT):
            await message.answer("⚠️ Дневной лимит запросов исчерпан. Возвращайся завтра!")
            return

    log_action(user_id, "query", text)

    # Если у юзера есть сохранённые ниши и текст похож на уточнение
    state = _user_state.get(user_id)
    if state and _looks_like_refinement(text):
        log_action(user_id, "refinement", text)
        niches_to_filter = state["niches"]
        niches_dicts = [{"query": n.query, "requests": n.requests, "products": n.products, "competition": n.competition} for n in niches_to_filter]
        filtered = await filter_niches_by_text(text, niches_dicts)

        filtered_queries = {f["query"] for f in filtered}
        filtered_niches = [n for n in niches_to_filter if n.query in filtered_queries]

        state["niches"] = filtered_niches
        state["offset"] = 0
        await _send_products(message, filtered_niches, 0, user_id)
        return

    # Парсим параметры из запроса
    await message.answer("⏳ Анализирую запрос...")
    params = await parse_query_params(text)

    search_text = text
    if params:
        search_text = params.get("search_text", text) or text
        log_action(user_id, "params", str(params))

    # Единый SQL-запрос: числовые фильтры + LIKE по search_text
    await message.answer("⏳ Ищу в базе...")
    query_params = {
        "search_text": search_text,
        "max_competition": params.get("max_competition") if params else None,
        "min_requests": params.get("min_requests") if params else None,
        "max_products": params.get("max_products") if params else None,
        "sort_by": params.get("sort_by") if params else None,
    }
    result_niches = _query_sqlite(query_params)

    if not result_niches:
        await message.answer("🔍 Ничего не найдено. Попробуй другой запрос или смягчи условия.")
        return

    _user_state[user_id] = {"niches": result_niches, "offset": 0}
    await _send_products(message, result_niches, 0, user_id)


async def _send_products(message: Message, niches: list[Niche], offset: int, user_id: int):
    """Группирует ниши по категориям, анализирует и выводит по 3 штуки."""
    # Группируем по категориям через SQL
    if not niches:
        await message.answer("Больше нет ниш по этому запросу.")
        return

    # Получаем категории с агрегированными данными
    categories = _get_category_groups(niches)
    if not categories:
        await message.answer("Не удалось сгруппировать по категориям.")
        return

    total_categories = len(categories)
    batch = categories[offset:offset + 3]
    if not batch:
        await message.answer("Больше нет категорий по этому запросу.")
        return

    text_parts = [f"🎯 Найдено категорий: {total_categories}\n"]

    for i, cat in enumerate(batch, offset + 1):
        name = cat["category"]
        total_requests = cat["total_requests"]
        phrase_count = cat["phrase_count"]
        avg_competition = cat["avg_competition"]
        top_phrases = cat["top_phrases"]

        text_parts.append(f"## {i}. {name}")
        text_parts.append(f"📊 **{total_requests:,}** запросов/мес · **{phrase_count}** фраз · 🎯 конкуренция **{avg_competition:.1f}%**")

        # Топ-5 фраз с низкой конкуренцией
        if top_phrases:
            text_parts.append("🔑 *Лучшие фразы:*")
            for p in top_phrases:
                text_parts.append(f"  · {p['query']} — {p['requests']:,} запросов, конкуренция {p['competition']:.1f}%")

        # Аналитика категории
        text_parts.append("")
        analysis = await analyze_product(name, top_phrases[:5])
        if analysis:
            text_parts.append(analysis)
        else:
            text_parts.append("⚠️ Аналитика временно недоступна")

        # Ссылка на WB по первой фразе (не по категории — категории не ищутся)
        first_phrase = top_phrases[0]["query"] if top_phrases else name
        text_parts.append(f"🔗 [Поиск на WB](https://www.wildberries.ru/catalog/0/search.aspx?query={quote(first_phrase)})")
        text_parts.append("")

    text = "\n".join(text_parts)

    # Кнопки пагинации
    keyboard = []
    row = []
    if offset > 0:
        row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"prod_{offset-3}"))
    if offset + 3 < total_categories:
        row.append(InlineKeyboardButton(text="Далее ➡️", callback_data=f"prod_{offset+3}"))
    if row:
        keyboard.append(row)
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None

    await message.answer(text, reply_markup=markup, parse_mode="Markdown")
    log_action(user_id, "products_shown", f"offset={offset}, count={len(batch)}")


def _get_category_groups(niches: list[Niche]) -> list[dict]:
    """Группирует ниши по категориям через SQL, возвращает агрегированные данные."""
    if not niches or not os.path.exists(DB_PATH):
        return []

    # Извлекаем фразы из ниш для поиска по БД
    phrases = [n.query for n in niches[:200]]

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Строим WHERE по фразам
    placeholders = ",".join("?" * len(phrases))
    sql = f"""
        SELECT category,
               SUM(request_count) as total_requests,
               COUNT(*) as phrase_count,
               AVG(competition) as avg_competition
        FROM niches
        WHERE phrase IN ({placeholders})
          AND competition <= 15
          AND request_count >= 500
          AND cards_count > 0
          AND category IS NOT NULL
          AND category != ''
        GROUP BY category
        ORDER BY total_requests DESC
        LIMIT 50
    """
    c.execute(sql, phrases)
    rows = c.fetchall()

    result = []
    for row in rows:
        category = row["category"]
        # Получаем топ-5 фраз для этой категории
        c.execute(
            "SELECT phrase, request_count, cards_count, competition "
            "FROM niches WHERE category = ? AND competition <= 15 AND request_count >= 500 AND cards_count > 0 "
            "ORDER BY request_count DESC LIMIT 5",
            (category,),
        )
        top_phrases = [
            {"query": r["phrase"], "requests": r["request_count"], "products": r["cards_count"], "competition": r["competition"]}
            for r in c.fetchall()
        ]

        result.append({
            "category": category,
            "total_requests": row["total_requests"],
            "phrase_count": row["phrase_count"],
            "avg_competition": row["avg_competition"],
            "top_phrases": top_phrases,
        })

    conn.close()
    return result


async def _send_niches(message: Message, niches: list[Niche], offset: int, user_id: int):
    """Отправляет 10 ниш начиная с offset, с кнопкой «Далее» (старый формат)."""
    batch = niches[offset:offset + 10]
    if not batch:
        await message.answer("Больше нет ниш по этому запросу.")
        return

    text_parts = [f"🔍 Найдено ниш: {len(niches)}\n"]
    for i, n in enumerate(batch, offset + 1):
        text_parts.append(f"{i}. {format_niche(n)}")
        text_parts.append(f"   🔗 [WB]({wb_search_url(n.query)})\n")

    text = "\n".join(text_parts)

    # Кнопки
    keyboard = []
    row = []
    if offset > 0:
        row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page_{offset-10}"))
    if offset + 10 < len(niches):
        row.append(InlineKeyboardButton(text="Далее ➡️", callback_data=f"page_{offset+10}"))
    if row:
        keyboard.append(row)
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None

    await message.answer(text, reply_markup=markup, parse_mode="Markdown")
    log_action(user_id, "niches_shown", f"offset={offset}, count={len(batch)}")


@router.callback_query(F.data.startswith("page_"))
async def handle_pagination(callback: CallbackQuery):
    user_id = callback.from_user.id
    offset = int(callback.data.split("_")[1])
    state = _user_state.get(user_id)

    if not state:
        await callback.answer("Сессия истекла. Сделай новый запрос.")
        return

    niches = state["niches"]
    state["offset"] = offset

    await _send_niches(callback.message, niches, offset, user_id)
    await callback.answer()
    log_action(user_id, "pagination", f"offset={offset}")


@router.callback_query(F.data.startswith("prod_"))
async def handle_product_pagination(callback: CallbackQuery):
    user_id = callback.from_user.id
    offset = int(callback.data.split("_")[1])
    state = _user_state.get(user_id)

    if not state:
        await callback.answer("Сессия истекла. Сделай новый запрос.")
        return

    niches = state["niches"]
    state["offset"] = offset

    await _send_products(callback.message, niches, offset, user_id)
    await callback.answer()
    log_action(user_id, "product_pagination", f"offset={offset}")


def _looks_like_refinement(text: str) -> bool:
    """Эвристика: текст похож на уточнение, а не на новый поиск."""
    # Длинные тексты (>80 символов) — почти всегда новый поиск, не уточнение
    if len(text) > 80:
        return False
    refinement_markers = ["не ", "без ", "исключи", "убери", "кроме", "но не"]
    return any(text.lower().startswith(m) or f" {m}" in text.lower() for m in refinement_markers)