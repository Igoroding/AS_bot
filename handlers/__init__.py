"""Обработчики сообщений Telegram-бота."""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from urllib.parse import quote
import sqlite3
import os

from config import DAILY_USER_LIMIT
from database import log_action, check_and_increment_usage, init_db
from filters.niche_loader import (
    load_niches, get_categories, filter_by_category,
    filter_by_keywords, format_niche, wb_search_url, Niche,
)
from llm import match_categories, filter_niches_by_text, filter_niches_by_semantic, parse_query_params
from voice import transcribe_audio

router = Router()

# Глобальный кэш ниш (загружается при старте)
_niches_cache: list[Niche] = []
_niches_by_category: dict[str, list[Niche]] = {}
_user_state: dict[int, dict] = {}  # user_id → {niches: [...], offset: int}

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "wb_trends.db")


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
    """Прямой SQL-запрос к БД с фильтрами по параметрам."""
    if not os.path.exists(DB_PATH):
        return []
    
    where_parts = ["competition <= 5", "request_count >= 500"]
    args = []
    
    if params.get("max_products") is not None:
        where_parts.append("cards_count <= ?")
        args.append(int(params["max_products"]))
    
    if params.get("min_requests") is not None:
        where_parts.append("request_count >= ?")
        args.append(int(params["min_requests"]))
    
    if params.get("max_competition") is not None:
        where_parts.append("competition <= ?")
        args.append(float(params["max_competition"]))
    
    if params.get("categories"):
        placeholders = ",".join("?" * len(params["categories"]))
        where_parts.append(f"category IN ({placeholders})")
        args.extend(params["categories"])
    
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
        "3. Уточни, если нужно — «не большие», «без цветов»\n"
        "4. Нажми на ссылку, чтобы перейти на WB\n\n"
        f"Лимит: {DAILY_USER_LIMIT} запросов в день."
    )
    log_action(message.from_user.id, "help")


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

    # Загружаем ниши
    niches = get_niches()
    if not niches:
        await message.answer("❌ Данные не загружены. Проверьте файл.")
        return

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
        await _send_niches(message, filtered_niches, 0, user_id)
        return

    # Парсим параметры из запроса (новый функционал)
    await message.answer("⏳ Анализирую запрос...")
    params = await parse_query_params(text)
    
    search_text = text
    has_params = False
    
    if params:
        search_text = params.get("search_text", text) or text
        has_params = any(params.get(k) is not None for k in ["max_products", "min_requests", "max_competition"])
        if params.get("categories"):
            has_params = True
        
        if has_params:
            log_action(user_id, "params", str(params))
    
    # Если есть числовые параметры — делаем прямой SQL-запрос
    if has_params and os.path.exists(DB_PATH):
        await message.answer("⏳ Ищу в базе с фильтрами...")
        result_niches = _query_sqlite(params)
        
        if not result_niches:
            await message.answer("🔍 По заданным фильтрам ничего не найдено. Попробуй смягчить условия.")
            return
        
        # Если есть поисковый текст и он конкретный — применяем семантический фильтр
        # Абстрактные тексты («хорошие ниши», «все категории») — пропускаем фильтр
        abstract_markers = ["хорош", "все ниш", "все катег", "свободн", "любые", "любая", "найти ниш", "куда зайти"]
        is_abstract = any(m in search_text.lower() for m in abstract_markers) or len(search_text.strip()) < 3
        
        if search_text and search_text.strip() and not is_abstract:
            await message.answer("⏳ Фильтрую по смыслу...")
            niches_dicts = [{"query": n.query, "requests": n.requests, "products": n.products, "competition": n.competition} for n in result_niches[:100]]
            filtered = await filter_niches_by_semantic(search_text, niches_dicts)
            if filtered is not None and len(filtered) > 0:
                filtered_queries = {f["query"] for f in filtered}
                result_niches = [n for n in result_niches if n.query in filtered_queries]
            elif filtered == [] and len(result_niches) > 10:
                result_niches = sorted(result_niches, key=lambda n: n.requests, reverse=True)[:20]
        
        if not result_niches:
            await message.answer("🔍 По смыслу запроса ничего не найдено. Попробуй уточнить.")
            return
        
        _user_state[user_id] = {"niches": result_niches, "offset": 0}
        await _send_niches(message, result_niches, 0, user_id)
        return

    # Обычный поиск: мэтчинг категорий
    await message.answer("⏳ Подбираю категории на WB...")
    all_categories = get_categories(niches)
    matched = await match_categories(search_text, all_categories)
    log_action(user_id, "categories_matched", ", ".join(matched))

    # Собираем ниши из выбранных категорий
    niches_by_cat = get_niches_by_category()
    result_niches = []
    for cat in matched:
        result_niches.extend(niches_by_cat.get(cat, []))

    # Если ничего не найдено — пробуем substring-поиск по запросам
    if not result_niches:
        text_lower = search_text.lower()
        result_niches = [n for n in niches if any(w in n.query.lower() for w in text_lower.split())]

    if not result_niches:
        await message.answer("🔍 Ничего не найдено. Попробуй другой запрос.")
        return

    # Семантический фильтр
    await message.answer("⏳ Фильтрую по смыслу запроса...")
    niches_dicts = [{"query": n.query, "requests": n.requests, "products": n.products, "competition": n.competition} for n in result_niches[:100]]
    filtered = await filter_niches_by_semantic(search_text, niches_dicts)
    if filtered is not None and len(filtered) > 0:
        filtered_queries = {f["query"] for f in filtered}
        result_niches = [n for n in result_niches if n.query in filtered_queries]
    elif filtered == [] and len(result_niches) > 10:
        result_niches = sorted(result_niches, key=lambda n: n.requests, reverse=True)[:20]

    if not result_niches:
        await message.answer("🔍 По смыслу запроса ничего не найдено. Попробуй уточнить.")
        return

    _user_state[user_id] = {"niches": result_niches, "offset": 0}
    await _send_niches(message, result_niches, 0, user_id)


async def _send_niches(message: Message, niches: list[Niche], offset: int, user_id: int):
    """Отправляет 10 ниш начиная с offset, с кнопкой «Далее»."""
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


def _looks_like_refinement(text: str) -> bool:
    """Эвристика: текст похож на уточнение, а не на новый поиск."""
    refinement_markers = ["не ", "без ", "исключи", "убери", "кроме", "но не"]
    return any(text.lower().startswith(m) or f" {m}" in text.lower() for m in refinement_markers)