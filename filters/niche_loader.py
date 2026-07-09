"""Загрузка и фильтрация ниш из xlsx-файла WB Search Analytics."""
from openpyxl import load_workbook
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Niche:
    query: str          # поисковый запрос
    requests: int       # количество запросов (частотность)
    products: int       # количество товаров (карточек в выдаче)
    competition: float  # конкуренция % (products / requests * 100)
    category: str       # предмет (категория WB)
    # Дополнительные метрики
    cart_conversion: Optional[float] = None    # конверсия в корзину %
    order_conversion: Optional[float] = None   # конверсия в заказ %
    items_with_orders: Optional[int] = None     # предметов с заказами


def load_niches(filepath: str, max_competition: float = 5.0, min_requests: int = 500) -> List[Niche]:
    """Загружает xlsx, фильтрует по конкуренции и частотности, возвращает список ниш."""
    wb = load_workbook(filepath, data_only=True)

    # Ищем лист с детальной информацией
    ws = None
    for name in wb.sheetnames:
        if "детальн" in name.lower():
            ws = wb[name]
            break
    if ws is None:
        ws = wb[wb.sheetnames[-1]]  # последний лист как fallback

    # Заголовки в строке 2
    headers = [cell.value for cell in ws[2]]
    col_map = {}
    for i, h in enumerate(headers):
        if h:
            col_map[str(h).strip().lower()] = i

    col_query = col_map.get("поисковый запрос", 0)
    col_requests = col_map.get("количество запросов", 1)
    col_products = col_map.get("количество товаров", 18)
    col_category = col_map.get("больше всего заказов в предмете", 5)
    col_cart_conv = col_map.get("конверсия в корзину", 10)
    col_order_conv = col_map.get("конверсия в заказ", 14)
    col_items_orders = col_map.get("предметов с заказами по запросу", 16)

    niches = []
    for row in ws.iter_rows(min_row=3, values_only=True):
        query = row[col_query] if col_query < len(row) else None
        requests = row[col_requests] if col_requests < len(row) else None
        products = row[col_products] if col_products < len(row) else None

        if not query or not requests or not products:
            continue
        if not isinstance(requests, (int, float)) or not isinstance(products, (int, float)):
            continue

        competition = (products / requests * 100) if requests > 0 else 999.0

        if competition > max_competition or requests < min_requests:
            continue

        niches.append(Niche(
            query=str(query).strip(),
            requests=int(requests),
            products=int(products),
            competition=round(competition, 2),
            category=str(row[col_category]).strip() if col_category < len(row) and row[col_category] else "Без категории",
            cart_conversion=row[col_cart_conv] if col_cart_conv < len(row) and isinstance(row[col_cart_conv], (int, float)) else None,
            order_conversion=row[col_order_conv] if col_order_conv < len(row) and isinstance(row[col_order_conv], (int, float)) else None,
            items_with_orders=int(row[col_items_orders]) if col_items_orders < len(row) and isinstance(row[col_items_orders], (int, float)) else None,
        ))

    # Сортируем по конкуренции (возрастание — лучшие первыми)
    niches.sort(key=lambda n: n.competition)
    return niches


def get_categories(niches: List[Niche]) -> List[str]:
    """Возвращает уникальные категории из списка ниш."""
    return sorted(set(n.category for n in niches))


def filter_by_category(niches: List[Niche], category: str) -> List[Niche]:
    """Фильтрует ниши по категории."""
    return [n for n in niches if n.category.lower() == category.lower()]


def filter_by_keywords(niches: List[Niche], exclude_keywords: List[str]) -> List[Niche]:
    """Исключает ниши, содержащие ключевые слова-исключения в запросе."""
    result = []
    for n in niches:
        q_lower = n.query.lower()
        if not any(kw.lower() in q_lower for kw in exclude_keywords):
            result.append(n)
    return result


def format_niche(n: Niche) -> str:
    """Форматирует нишу для вывода в Telegram."""
    line = f"📦 **{n.query}**\n"
    line += f"   Запросов: {n.requests:,} · Товаров: {n.products:,} · Конкуренция: {n.competition}%\n"
    if n.cart_conversion:
        line += f"   Конв. в корзину: {n.cart_conversion}%"
    if n.order_conversion:
        line += f" · Конв. в заказ: {n.order_conversion}%"
    return line


def wb_search_url(query: str) -> str:
    """Ссылка на поисковую выдачу WB."""
    from urllib.parse import quote
    return f"https://www.wildberries.ru/catalog/0/search.aspx?search={quote(query)}"