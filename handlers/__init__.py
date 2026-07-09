"""Обработчики сообщений Telegram-бота."""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from urllib.parse import quote

from config import DAILY_USER_LIMIT
from database import log_action, check_and_increment_usage, init_db
from filters.niche_loader import (
    load_niches, get_categories, filter_by_category,
    filter_by_keywords, format_niche, wb_search_url, Niche,
)
from llm import match_categories, filter_niches_by_text

router = Router()

# Глобальный кэш ниш (загружается при старте)
_niches_cache: list[Niche] = []
_niches_by_category: dict[str, list[Niche]] = {}
_user_state: dict[int, dict] = {}  # user_id → {niches: [...], offset: int}


def get_niches() -> list[Niche]:
    global _niches_cache
    if not _niches_cache:
        from config import DATA_FILE
        _niches_cache = load_niches(DATA_FILE)
    return _niches_cache


def get_niches_by_category() -> dict[str, list[Niche]]:
    global _niches_by_category
    if not _niches_by_category:
        niches = get_niches()
        cats = get_categories(niches)
        for cat in cats:
            _niches_by_category[cat] = filter_by_category(niches, cat)
    return _niches_by_category


@router.message(CommandStart())
async def cmd_start(message: Message):
    init_db()
    await message.answer(
        "👋 Привет! Я бот для поиска свободных ниш на Wildberries.\n\n"
        "Опиши, что ты ищешь или производишь — например:\n"
        "«шью женскую одежду»\n«продаю семена»\n«делаю украшения»\n\n"
        "Я подберу категории WB и покажу ниши с низкой конкуренцией."
    )
    log_action(message.from_user.id, "start")


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📝 Как пользоваться:\n\n"
        "1. Опиши, что ты ищешь — свободным текстом\n"
        "2. Бот подберёт категории WB и покажет ниши\n"
        "3. Уточни, если нужно — «не большие размеры», «только лёгкие»\n"
        "4. Нажми на ссылку, чтобы перейти на WB\n\n"
        f"Лимит: {DAILY_USER_LIMIT} запросов в день."
    )
    log_action(message.from_user.id, "help")


@router.message()
async def handle_text(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    # Проверка лимита
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

        # Находим исходные Niche-объекты
        filtered_queries = {f["query"] for f in filtered}
        filtered_niches = [n for n in niches_to_filter if n.query in filtered_queries]

        state["niches"] = filtered_niches
        state["offset"] = 0
        await _send_niches(message, filtered_niches, 0, user_id)
        return

    # Новый поиск: мэтчинг категорий
    all_categories = get_categories(niches)
    matched = await match_categories(text, all_categories)
    log_action(user_id, "categories_matched", ", ".join(matched))

    # Собираем ниши из выбранных категорий
    niches_by_cat = get_niches_by_category()
    result_niches = []
    for cat in matched:
        result_niches.extend(niches_by_cat.get(cat, []))

    # Если ничего не найдено — пробуем substring-поиск по запросам
    if not result_niches:
        text_lower = text.lower()
        result_niches = [n for n in niches if any(w in n.query.lower() for w in text_lower.split())]

    if not result_niches:
        await message.answer("🔍 Ничего не найдено. Попробуй другой запрос.")
        return

    # Сохраняем состояние юзера
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
    refinement_markers = ["не ", "без ", "только ", "исключи", "убери", "кроме", "но не"]
    return any(text.lower().startswith(m) or f" {m}" in text.lower() for m in refinement_markers)