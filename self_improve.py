"""
Self-improvement script for WB Trends Bot.
Runs daily at 2:00 AM via cronjob.
Analyses logs, finds patterns, generates improvement recommendations.
"""
import sqlite3
import os
import json
import time
from datetime import datetime, timedelta
from collections import Counter, defaultdict

LOG_DB = os.path.join(os.path.dirname(__file__), "bot_logs.db")
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "wb_trends.db")
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.py")
LLM_PATH = os.path.join(os.path.dirname(__file__), "llm.py")
HANDLERS_PATH = os.path.join(os.path.dirname(__file__), "handlers", "__init__.py")


def analyze_logs() -> dict:
    """Анализирует логи за последние 24 часа."""
    if not os.path.exists(LOG_DB):
        return {"error": "Log DB not found"}

    conn = sqlite3.connect(LOG_DB)
    c = conn.cursor()

    # За последние 24 часа
    cutoff = int(time.time()) - 86400

    # Общая статистика
    c.execute("SELECT COUNT(*) FROM user_actions WHERE timestamp >= ?", (cutoff,))
    total_actions = c.fetchone()[0]

    c.execute("SELECT COUNT(DISTINCT user_id) FROM user_actions WHERE timestamp >= ?", (cutoff,))
    total_users = c.fetchone()[0]

    # Типы действий
    c.execute(
        "SELECT action, COUNT(*) as cnt FROM user_actions WHERE timestamp >= ? GROUP BY action ORDER BY cnt DESC",
        (cutoff,),
    )
    action_counts = dict(c.fetchall())

    # Пустые результаты (query → ничего не найдено)
    c.execute(
        "SELECT detail FROM user_actions WHERE action = 'query' AND timestamp >= ?",
        (cutoff,),
    )
    queries = [row[0] for row in c.fetchall()]

    c.execute(
        "SELECT COUNT(*) FROM user_actions WHERE action IN ('niches_shown', 'products_shown') AND timestamp >= ?",
        (cutoff,),
    )
    successful = c.fetchone()[0]

    c.execute(
        "SELECT COUNT(*) FROM user_actions WHERE action = 'query' AND timestamp >= ?",
        (cutoff,),
    )
    total_queries = c.fetchone()[0]

    empty_results = total_queries - successful if total_queries > successful else 0

    # Уточнения (refinement) — сколько раз пользователь уточнял
    c.execute(
        "SELECT COUNT(*) FROM user_actions WHERE action = 'refinement' AND timestamp >= ?",
        (cutoff,),
    )
    refinements = c.fetchone()[0]

    # Ошибки
    c.execute(
        "SELECT detail FROM user_actions WHERE action = 'query' AND timestamp >= ? AND detail LIKE '%ошибк%'",
        (cutoff,),
    )
    error_queries = [row[0] for row in c.fetchall()]

    # Категории, которые чаще всего не дают результатов
    c.execute(
        "SELECT detail FROM user_actions WHERE action = 'categories_matched' AND timestamp >= ?",
        (cutoff,),
    )
    matched_categories = [row[0] for row in c.fetchall()]

    # Пагинация — сколько раз листали
    c.execute(
        "SELECT COUNT(*) FROM user_actions WHERE action IN ('pagination', 'product_pagination') AND timestamp >= ?",
        (cutoff,),
    )
    paginations = c.fetchone()[0]

    conn.close()

    return {
        "total_actions": total_actions,
        "total_users": total_users,
        "total_queries": total_queries,
        "successful_results": successful,
        "empty_results": empty_results,
        "empty_rate": round(empty_results / total_queries * 100, 1) if total_queries else 0,
        "refinements": refinements,
        "refinement_rate": round(refinements / total_queries * 100, 1) if total_queries else 0,
        "paginations": paginations,
        "action_counts": action_counts,
        "error_queries": error_queries,
        "matched_categories": matched_categories,
    }


def analyze_db_stats() -> dict:
    """Анализирует состояние БД ниш."""
    if not os.path.exists(DB_PATH):
        return {"error": "DB not found"}

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM niches")
    total = c.fetchone()[0]

    c.execute("SELECT COUNT(DISTINCT category) FROM niches")
    categories = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM niches WHERE competition <= 15 AND request_count >= 500 AND cards_count > 0")
    free_niches_15 = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM niches WHERE competition <= 5 AND request_count >= 500 AND cards_count > 0")
    free_niches_5 = c.fetchone()[0]

    c.execute("SELECT category, COUNT(*) as cnt FROM niches WHERE competition <= 15 AND request_count >= 500 AND cards_count > 0 GROUP BY category ORDER BY cnt DESC LIMIT 10")
    top_categories = [f"{row[0]}: {row[1]} ниш" for row in c.fetchall()]

    conn.close()

    return {
        "total_phrases": total,
        "categories": categories,
        "free_niches_15pct": free_niches_15,
        "free_niches_5pct": free_niches_5,
        "top_categories": top_categories,
    }


def generate_recommendations(stats: dict, db_stats: dict) -> list[dict]:
    """Генерирует рекомендации на основе анализа."""
    recommendations = []

    # 1. Много пустых результатов
    if stats.get("empty_rate", 0) > 30:
        recommendations.append({
            "priority": "high",
            "issue": f"Высокий процент пустых результатов: {stats['empty_rate']}%",
            "suggestion": "Проверить семантический фильтр — возможно, он слишком строгий. Рассмотреть ослабление порога или fallback на топ-20 при пустом результате.",
            "action": "review_semantic_filter",
        })

    # 2. Много уточнений
    if stats.get("refinement_rate", 0) > 20:
        recommendations.append({
            "priority": "medium",
            "issue": f"Частые уточнения: {stats['refinement_rate']}% запросов",
            "suggestion": "Пользователи часто уточняют результаты — возможно, бот не понимает контекст с первого раза. Проверить качество мэтчинга категорий.",
            "action": "review_category_matching",
        })

    # 3. Мало свободных ниш
    if db_stats.get("free_niches_15pct", 0) < 100:
        recommendations.append({
            "priority": "high",
            "issue": f"Мало свободных ниш в БД: всего {db_stats['free_niches_15pct']}",
            "suggestion": "База данных почти пуста. Нужна новая выгрузка от Никиты с более широким охватом категорий.",
            "action": "need_new_data",
        })

    # 4. Ошибки в запросах
    if stats.get("error_queries"):
        recommendations.append({
            "priority": "medium",
            "issue": f"Обнаружены запросы с ошибками: {len(stats['error_queries'])} шт",
            "suggestion": "Проверить логи на предмет повторяющихся ошибок LLM или Whisper.",
            "action": "check_error_logs",
        })

    # 5. Нет пользователей
    if stats.get("total_users", 0) <= 1:
        recommendations.append({
            "priority": "info",
            "issue": "Нет внешних пользователей (только тестировщик)",
            "suggestion": "Пора запускать рекламу. Без пользователей self-improvement не имеет смысла.",
            "action": "launch_ads",
        })

    # 6. Много пагинаций — пользователи листают
    if stats.get("paginations", 0) > stats.get("total_queries", 0) * 2:
        recommendations.append({
            "priority": "low",
            "issue": f"Активное использование пагинации: {stats['paginations']} раз",
            "suggestion": "Пользователи активно листают — возможно, стоит увеличить вывод с 3 до 5 товаров за раз.",
            "action": "increase_batch_size",
        })

    return recommendations


def format_report(stats: dict, db_stats: dict, recommendations: list[dict]) -> str:
    """Форматирует отчёт для отправки в Telegram."""
    lines = ["🤖 **Ежедневный отчёт самодиагностики**", f"📅 {datetime.now().strftime('%d.%m.%Y')}", ""]

    # Статистика использования
    lines.append("## 📊 Статистика за сутки")
    lines.append(f"· Пользователей: **{stats.get('total_users', 0)}**")
    lines.append(f"· Всего запросов: **{stats.get('total_queries', 0)}**")
    lines.append(f"· Успешных выдач: **{stats.get('successful_results', 0)}**")
    lines.append(f"· Пустых результатов: **{stats.get('empty_results', 0)}** ({stats.get('empty_rate', 0)}%)")
    lines.append(f"· Уточнений: **{stats.get('refinements', 0)}** ({stats.get('refinement_rate', 0)}%)")
    lines.append(f"· Пагинаций: **{stats.get('paginations', 0)}**")
    lines.append("")

    # Состояние БД
    lines.append("## 🗄 Состояние базы данных")
    lines.append(f"· Всего фраз: **{db_stats.get('total_phrases', 0):,}**")
    lines.append(f"· Категорий: **{db_stats.get('categories', 0)}**")
    lines.append(f"· Свободных ниш (≤15%): **{db_stats.get('free_niches_15pct', 0):,}**")
    lines.append(f"· Свободных ниш (≤5%): **{db_stats.get('free_niches_5pct', 0):,}**")
    lines.append("")

    # Рекомендации
    if recommendations:
        lines.append("## 💡 Рекомендации")
        for rec in recommendations:
            priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢", "info": "ℹ️"}
            lines.append(f"{priority_emoji.get(rec['priority'], '•')} **{rec['issue']}**")
            lines.append(f"  {rec['suggestion']}")
            lines.append("")
    else:
        lines.append("## ✅ Всё хорошо")
        lines.append("Проблем не обнаружено. Бот работает стабильно.")
        lines.append("")

    lines.append("---")
    lines.append("_Следующий анализ — завтра в 02:00_")

    return "\n".join(lines)


def main():
    print("🔍 Self-improvement analysis started...")

    stats = analyze_logs()
    db_stats = analyze_db_stats()
    recommendations = generate_recommendations(stats, db_stats)
    report = format_report(stats, db_stats, recommendations)

    # Сохраняем отчёт
    report_path = os.path.join(os.path.dirname(__file__), "data", "daily_report.txt")
    with open(report_path, "w") as f:
        f.write(report)

    print(report)
    print(f"\n✅ Report saved to {report_path}")
    return report


if __name__ == "__main__":
    main()
