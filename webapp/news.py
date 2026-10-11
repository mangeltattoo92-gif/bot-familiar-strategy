"""
Seccion de novedades dentro de la app -- 2026-09-17, a pedido del usuario
("crea dentro de la app una seccion de noticias que resalte hasta que el
usuario entre y la lea"). Primero solo para el (username en
NEWS_ENABLED_USERS mas abajo, en app.py) mientras se decide si se agrega
para el resto de las cuentas.

Guarda los items en users.db (misma base que ya usa auth.py) en dos
tablas nuevas: news_items (el contenido) y news_reads (que usuario ya vio
que item, para saber cuando dejar de resaltarlo).
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_USERS_DB = Path(__file__).resolve().parent.parent / "data" / "users.db"


def _connect(db_path: Path = DEFAULT_USERS_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news_reads (
            username TEXT NOT NULL,
            news_id INTEGER NOT NULL,
            read_at TEXT NOT NULL,
            PRIMARY KEY (username, news_id)
        )
        """
    )
    # 2026-09-28, a pedido del usuario ("implementa 4 y 5" -- reportes
    # automaticos POR CUENTA): antes toda novedad era broadcast a todos.
    # username=NULL sigue siendo broadcast (comportamiento de siempre,
    # todas las filas existentes quedan igual); un valor no-NULL hace que
    # SOLO esa cuenta la vea -- para los reportes diarios personalizados.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(news_items)")}
    if "username" not in cols:
        conn.execute("ALTER TABLE news_items ADD COLUMN username TEXT")
    return conn


def add_news_item(title: str, body: str, username: str | None = None, db_path: Path = DEFAULT_USERS_DB) -> int:
    """username=None (default): broadcast, todas las cuentas la ven --
    mismo comportamiento de siempre. username='alguien': SOLO esa cuenta
    la ve (2026-09-28, para reportes diarios personalizados)."""
    conn = _connect(db_path)
    with conn:
        cur = conn.execute(
            "INSERT INTO news_items (title, body, created_at, username) VALUES (?, ?, ?, ?)",
            (title, body, datetime.now(timezone.utc).isoformat(timespec="seconds"), username),
        )
        return cur.lastrowid


def list_news_for_user(username: str, db_path: Path = DEFAULT_USERS_DB) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute(
        """
        SELECT n.id, n.title, n.body, n.created_at,
               CASE WHEN r.username IS NULL THEN 0 ELSE 1 END AS is_read
        FROM news_items n
        LEFT JOIN news_reads r ON r.news_id = n.id AND r.username = ?
        WHERE n.username IS NULL OR n.username = ?
        ORDER BY n.id DESC
        """,
        (username, username),
    ).fetchall()
    return [dict(r) for r in rows]


def has_unread_news(username: str, db_path: Path = DEFAULT_USERS_DB) -> bool:
    return any(not item["is_read"] for item in list_news_for_user(username, db_path))


def mark_all_read(username: str, db_path: Path = DEFAULT_USERS_DB) -> None:
    conn = _connect(db_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM news_items").fetchall()]
        conn.executemany(
            "INSERT OR IGNORE INTO news_reads (username, news_id, read_at) VALUES (?, ?, ?)",
            [(username, nid, now) for nid in ids],
        )
