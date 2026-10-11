"""Historial de ordenes REALES colocadas en Tradier, una fila por orden,
ligada a la conexion de broker (webapp/brokers/store.py) del usuario.

Esto es la cuenta real -- separado por completo del paper_trading.db
simulado de cada usuario (que sigue existiendo igual que siempre). Una
orden de entrada y su salida (OCO) quedan relacionadas via parent_order_id.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "users.db"


def _connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS real_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            connection_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            ticker TEXT NOT NULL,
            option_type TEXT NOT NULL,
            option_symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            limit_price REAL,
            tradier_order_id TEXT,
            status TEXT NOT NULL DEFAULT 'attempt',
            fill_price REAL,
            profit_target_pct REAL,
            stop_loss_pct REAL,
            parent_order_id INTEGER,
            reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_real_orders_connection ON real_orders (connection_id)")
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_order(
    connection_id: int, user_id: int, username: str, ticker: str, option_type: str, option_symbol: str,
    side: str, quantity: int, limit_price: float | None = None, status: str = "attempt",
    profit_target_pct: float | None = None, stop_loss_pct: float | None = None,
    parent_order_id: int | None = None, reason: str = "", db_path: Path = DEFAULT_DB,
) -> int:
    conn = _connect(db_path)
    try:
        now = _now()
        cur = conn.execute(
            "INSERT INTO real_orders (connection_id, user_id, username, ticker, option_type, option_symbol, "
            "side, quantity, limit_price, status, profit_target_pct, stop_loss_pct, parent_order_id, reason, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (connection_id, user_id, username, ticker, option_type, option_symbol, side, quantity, limit_price,
             status, profit_target_pct, stop_loss_pct, parent_order_id, reason, now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_order(
    order_id: int, status: str | None = None, tradier_order_id: str | None = None,
    fill_price: float | None = None, db_path: Path = DEFAULT_DB,
) -> None:
    conn = _connect(db_path)
    try:
        sets, params = [], []
        if status is not None:
            sets.append("status = ?"); params.append(status)
        if tradier_order_id is not None:
            sets.append("tradier_order_id = ?"); params.append(tradier_order_id)
        if fill_price is not None:
            sets.append("fill_price = ?"); params.append(fill_price)
        sets.append("updated_at = ?"); params.append(_now())
        params.append(order_id)
        conn.execute(f"UPDATE real_orders SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
    finally:
        conn.close()


def get_order(order_id: int, db_path: Path = DEFAULT_DB) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM real_orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_orders_for_connection(connection_id: int, limit: int = 200, db_path: Path = DEFAULT_DB) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM real_orders WHERE connection_id = ? ORDER BY id DESC LIMIT ?",
            (connection_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def entries_awaiting_exit(connection_id: int, db_path: Path = DEFAULT_DB) -> list[dict[str, Any]]:
    """Entradas (buy_to_open) ya llenadas que todavia no tienen una salida OCO colocada."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM real_orders WHERE connection_id = ? AND side = 'buy_to_open' AND status = 'filled' "
            "AND id NOT IN (SELECT parent_order_id FROM real_orders WHERE parent_order_id IS NOT NULL)",
            (connection_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def pending_entries(connection_id: int, db_path: Path = DEFAULT_DB) -> list[dict[str, Any]]:
    """Entradas colocadas (tienen tradier_order_id) que todavia no se sabe si llenaron."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM real_orders WHERE connection_id = ? AND side = 'buy_to_open' AND status = 'pending' "
            "AND tradier_order_id IS NOT NULL",
            (connection_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def buys_today(connection_id: int, db_path: Path = DEFAULT_DB) -> list[dict[str, Any]]:
    """Compras de HOY (UTC) que no fueron rechazadas/canceladas -- cuentan para el limite diario."""
    today = datetime.now(timezone.utc).date().isoformat()
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM real_orders WHERE connection_id = ? AND side = 'buy_to_open' "
            "AND status NOT IN ('rejected', 'canceled', 'cancelled') AND created_at >= ?",
            (connection_id, today),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def last_action_at(connection_id: int, db_path: Path = DEFAULT_DB) -> datetime | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT created_at FROM real_orders WHERE connection_id = ? ORDER BY id DESC LIMIT 1",
            (connection_id,),
        ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["created_at"])
    finally:
        conn.close()
