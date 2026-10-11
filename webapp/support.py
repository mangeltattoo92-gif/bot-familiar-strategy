"""Soporte tecnico dentro de la app -- 2026-09-28, a pedido del usuario
("quiero al panel de administrador le agregues un soporte tecnico donde
los usuarios puedan escribirme dentro de la aplicacion... es personal para
cada usuario y solo ellos tienen acceso a las conversaciones personales" /
"cada usuario escribe independiente y solo ellos pueden ver los chat
privados con el administrador").

Un hilo de mensajes POR CUENTA familiar, privado entre esa cuenta y el
admin -- ninguna otra cuenta familiar puede ver el hilo de otra. Guarda
los mensajes en users.db (misma base que auth.py/news.py), tabla nueva
support_messages. Mismo patron que news.py (_connect crea/migra el
esquema, funciones simples sin clase)."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_USERS_DB = Path(__file__).resolve().parent.parent / "data" / "users.db"


def _connect(db_path: Path = DEFAULT_USERS_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS support_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            sender TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            read_by_user INTEGER NOT NULL DEFAULT 0,
            read_by_admin INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(support_messages)")}
    if "brand" not in cols:
        # 2026-10-06, a pedido explicito ("si escribo desde metabot tiene
        # que llegarle al admin de metabot, si escribo desde bot familiar
        # al admin de bot familiar"): antes el hilo se agrupaba SOLO por
        # username, y que bandeja de admin lo veia dependia del
        # familiar_access FIJO de esa cuenta -- no servia para la cuenta
        # admin propia, que puede escribir "disfrazada" de cualquiera de
        # las dos marcas (ver "Ver mi panel como Metabot/Bot Familiar").
        # Ahora cada mensaje queda marcado con la marca activa en el
        # momento de escribirlo. NULL en filas viejas (de antes de esta
        # columna) -- list_support_thread/list_support_threads_summary
        # caen al familiar_access de la cuenta para esas, mismo
        # comportamiento que ya tenian.
        conn.execute("ALTER TABLE support_messages ADD COLUMN brand TEXT")
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_support_username ON support_messages (username)")
    return conn


_EFFECTIVE_BRAND_SQL = (
    "COALESCE(m.brand, CASE WHEN u.familiar_access = 'approved' THEN 'familiar' ELSE 'metabot' END)"
)


def send_support_message(
    username: str, sender: str, body: str, brand: str | None = None, db_path: Path = DEFAULT_USERS_DB,
) -> int:
    """username: de que cuenta familiar es el hilo (siempre la cuenta, nunca
    'admin' -- el admin escribe EN el hilo de esa cuenta, no en uno propio).
    sender: 'user' (lo escribio el familiar) o 'admin' (lo escribio el
    administrador). Un mensaje del propio dueño del hilo ya queda leido por
    el; uno del otro lado queda sin leer por el dueño del hilo.
    brand: 'metabot' o 'familiar' -- la marca activa cuando se escribio
    (ver nota de la columna en _connect()); None para dejar que
    list_support_thread/list_support_threads_summary caigan al
    familiar_access de la cuenta."""
    if sender not in ("user", "admin"):
        raise ValueError("sender debe ser 'user' o 'admin'")
    if not body or not body.strip():
        raise ValueError("el mensaje no puede estar vacio")
    conn = _connect(db_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    read_by_user = 1 if sender == "user" else 0
    read_by_admin = 1 if sender == "admin" else 0
    with conn:
        cur = conn.execute(
            "INSERT INTO support_messages (username, sender, body, created_at, read_by_user, read_by_admin, brand) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (username, sender, body.strip(), now, read_by_user, read_by_admin, brand),
        )
        return cur.lastrowid


def list_support_thread(username: str, brand: str | None = None, db_path: Path = DEFAULT_USERS_DB) -> list[dict]:
    """Todo el hilo de ESA cuenta, orden cronologico (mas viejo primero,
    como un chat). Nunca mezcla hilos de otras cuentas. `brand` (opcional):
    solo los mensajes de esa marca -- una cuenta admin que escribio
    "disfrazada" de las dos marcas tiene en los hechos DOS hilos
    separados bajo el mismo username (ver nota de la columna brand)."""
    conn = _connect(db_path)
    if brand is None:
        rows = conn.execute(
            "SELECT id, sender, body, created_at FROM support_messages WHERE username = ? ORDER BY id ASC",
            (username,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT m.id, m.sender, m.body, m.created_at
            FROM support_messages m LEFT JOIN users u ON u.username = m.username
            WHERE m.username = ? AND {_EFFECTIVE_BRAND_SQL} = ?
            ORDER BY m.id ASC
            """,
            (username, brand),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_thread_read(
    username: str, reader: str, brand: str | None = None, db_path: Path = DEFAULT_USERS_DB,
) -> None:
    """reader='user': marca leidos los mensajes del admin en ese hilo (los
    del propio usuario ya estaban leidos por el). reader='admin': marca
    leidos los del usuario en ese hilo especifico. `brand` (opcional):
    solo marca leidos los de esa marca -- para no marcar sin querer el
    sub-hilo de la OTRA marca de una cuenta que escribio disfrazada de
    las dos."""
    if reader not in ("user", "admin"):
        raise ValueError("reader debe ser 'user' o 'admin'")
    conn = _connect(db_path)
    col = "read_by_user" if reader == "user" else "read_by_admin"
    other_sender = "admin" if reader == "user" else "user"
    with conn:
        if brand is None:
            conn.execute(
                f"UPDATE support_messages SET {col} = 1 WHERE username = ? AND sender = ?",
                (username, other_sender),
            )
        else:
            conn.execute(
                f"""
                UPDATE support_messages SET {col} = 1 WHERE id IN (
                    SELECT m.id FROM support_messages m LEFT JOIN users u ON u.username = m.username
                    WHERE m.username = ? AND m.sender = ? AND {_EFFECTIVE_BRAND_SQL} = ?
                )
                """,
                (username, other_sender, brand),
            )


def has_unread_support(username: str, db_path: Path = DEFAULT_USERS_DB) -> bool:
    """Para el badge del lado del usuario -- hay algun mensaje del admin
    en SU hilo que todavia no leyo."""
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT 1 FROM support_messages WHERE username = ? AND sender = 'admin' AND read_by_user = 0 LIMIT 1",
        (username,),
    ).fetchone()
    return row is not None


def list_support_threads_summary(brand: str | None = None, db_path: Path = DEFAULT_USERS_DB) -> list[dict]:
    """Para el panel de admin -- una fila por (cuenta, marca) que tiene al
    menos un mensaje de esa marca, con el ultimo mensaje y cuantos sin
    leer por el admin. Normalmente una cuenta tiene una sola marca (la de
    su familiar_access) y por lo tanto una sola fila -- pero la cuenta
    admin, que puede escribir disfrazada de las dos, puede generar DOS
    filas con el mismo username (ver nota de la columna brand en
    _connect()). `brand` (opcional): solo las filas de esa marca -- esto
    reemplaza el filtro-por-username que se usaba antes (_usernames_for_group
    en app.py), que no distinguia marca por mensaje. No incluye cuentas
    sin ningun mensaje todavia."""
    conn = _connect(db_path)
    rows = conn.execute(
        f"""
        SELECT m.username, m.sender, m.body, m.created_at, m.read_by_admin,
               {_EFFECTIVE_BRAND_SQL} AS eff_brand
        FROM support_messages m LEFT JOIN users u ON u.username = m.username
        ORDER BY m.id ASC
        """
    ).fetchall()
    groups: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["username"], r["eff_brand"])
        g = groups.setdefault(key, {"username": r["username"], "brand": r["eff_brand"], "unread_by_admin": 0})
        g["last_sender"] = r["sender"]
        g["last_body"] = r["body"]
        g["last_created_at"] = r["created_at"]
        if r["sender"] == "user" and not r["read_by_admin"]:
            g["unread_by_admin"] += 1
    summaries = list(groups.values())
    if brand is not None:
        summaries = [s for s in summaries if s["brand"] == brand]
    summaries.sort(key=lambda s: s["last_created_at"], reverse=True)
    return summaries


def admin_has_unread_support(db_path: Path = DEFAULT_USERS_DB) -> bool:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT 1 FROM support_messages WHERE sender = 'user' AND read_by_admin = 0 LIMIT 1"
    ).fetchone()
    return row is not None
