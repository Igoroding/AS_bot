"""SQLite логирование действий пользователей."""
import sqlite3
import time
from config import LOG_DB


def init_db():
    conn = sqlite3.connect(LOG_DB)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            detail TEXT,
            timestamp INTEGER NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_usage (
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, date)
        )
    """)
    conn.commit()
    conn.close()


def log_action(user_id: int, action: str, detail: str = ""):
    conn = sqlite3.connect(LOG_DB)
    c = conn.cursor()
    c.execute(
        "INSERT INTO user_actions (user_id, action, detail, timestamp) VALUES (?, ?, ?, ?)",
        (user_id, action, detail, int(time.time())),
    )
    conn.commit()
    conn.close()


def check_and_increment_usage(user_id: int, limit: int) -> bool:
    """Возвращает True, если лимит не превышен. Иначе False."""
    from datetime import date
    today = date.today().isoformat()
    conn = sqlite3.connect(LOG_DB)
    c = conn.cursor()
    c.execute(
        "SELECT count FROM daily_usage WHERE user_id = ? AND date = ?",
        (user_id, today),
    )
    row = c.fetchone()
    if row and row[0] >= limit:
        conn.close()
        return False
    if row:
        c.execute(
            "UPDATE daily_usage SET count = count + 1 WHERE user_id = ? AND date = ?",
            (user_id, today),
        )
    else:
        c.execute(
            "INSERT INTO daily_usage (user_id, date, count) VALUES (?, ?, 1)",
            (user_id, today),
        )
    conn.commit()
    conn.close()
    return True