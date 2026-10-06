"""Conexiones de broker por usuario, con credenciales cifradas.

Todas las funciones reciben user_id y lo usan en el WHERE: una conexion
nunca se lee ni se revoca desde la cuenta de otro usuario.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from webapp.brokers import vault

DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "users.db"
ALLOWED_BROKERS = ("tradier", "alpaca")
ALLOWED_ENVS = ("paper", "live")


def _connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS broker_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            broker TEXT NOT NULL,
            account_id TEXT NOT NULL,
            env TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            data_blob BLOB,
            wrapped_dek BLOB,
            key_version INTEGER,
            connected_at TEXT NOT NULL,
            revoked_at TEXT,
            UNIQUE(user_id, broker, account_id)
        )
        """
    )
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_connection(user_id: int, broker: str, account_id: str, env: str, tokens: dict[str, Any],
                      db_path: Path = DEFAULT_DB) -> int:
    if broker not in ALLOWED_BROKERS:
        raise ValueError(f"broker no soportado: {broker}")
    if env not in ALLOWED_ENVS:
        raise ValueError("env debe ser 'paper' o 'live'")
    data_blob, wrapped_dek, version = vault.seal(tokens)
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO broker_connections (user_id, broker, account_id, env, status, data_blob, wrapped_dek, "
            "key_version, connected_at) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?) "
            "ON CONFLICT(user_id, broker, account_id) DO UPDATE SET env=excluded.env, status='active', "
            "data_blob=excluded.data_blob, wrapped_dek=excluded.wrapped_dek, key_version=excluded.key_version, "
            "connected_at=excluded.connected_at, revoked_at=NULL",
            (user_id, broker, account_id, env, data_blob, wrapped_dek, version, _now()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM broker_connections WHERE user_id = ? AND broker = ? AND account_id = ?",
            (user_id, broker, account_id),
        ).fetchone()
        return row["id"]
    finally:
        conn.close()


def list_connections(user_id: int, db_path: Path = DEFAULT_DB) -> list[dict[str, Any]]:
    """Metadatos solamente: nunca devuelve tokens."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, broker, account_id, env, status, connected_at, revoked_at FROM broker_connections "
            "WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_tokens(user_id: int, connection_id: int, db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    """Uso interno del motor de trading. Descifra solo si la conexion es del usuario y esta activa."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT data_blob, wrapped_dek, key_version, status FROM broker_connections WHERE id = ? AND user_id = ?",
            (connection_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row["status"] != "active":
        raise PermissionError("Conexion no disponible para este usuario")
    return vault.open_sealed(row["data_blob"], row["wrapped_dek"], row["key_version"])


def revoke_connection(user_id: int, connection_id: int, db_path: Path = DEFAULT_DB) -> bool:
    """Borra las credenciales y marca la conexion como revocada. Conserva el registro para auditoria."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE broker_connections SET status = 'revoked', revoked_at = ?, data_blob = NULL, "
            "wrapped_dek = NULL WHERE id = ? AND user_id = ? AND status = 'active'",
            (_now(), connection_id, user_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()
