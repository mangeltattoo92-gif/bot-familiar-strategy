"""
Autenticacion y gestion de usuarios multi-tenant para bot-familiar.

Las contrasenas y los codigos de activacion NUNCA se guardan en texto
plano: se derivan con werkzeug.security.generate_password_hash
(PBKDF2-SHA256 + salt aleatorio) y solo se guarda el hash en SQLite.

Flujo de alta de un familiar (2026-09-10, orden confirmado por el
usuario -- NO es invitacion previa):
  1. Se registra solo (register_user): usuario + correo + contrasena.
     Queda en estado 'pending' -- no puede entrar todavia.
  2. El usuario principal (admin) lo ve en list_pending_users() y lo
     aprueba (approve_user) o rechaza (reject_user).
  3. Al aprobar, generate_activation_code() crea un codigo numerico de
     un solo uso (vencimiento 48h) y devuelve el codigo EN TEXTO PLANO
     una unica vez (para mandarlo por correo -- ver nota sobre email
     mas abajo). El estado pasa a 'code_sent'.
  4. La persona ingresa ese codigo (verify_activation_code) -- si es
     correcto y no vencio, el estado pasa a 'active' y ya puede entrar
     de lleno.

Envio de correo real: TODAVIA NO IMPLEMENTADO -- se necesita un
proveedor (SendGrid/Mailgun/SES o similar) y sus credenciales, que no
existen hoy en este proyecto. Por ahora `generate_activation_code()`
devuelve el codigo en texto plano para que quien llame decida que hacer
con el (mostrarlo en el panel de admin como solucion temporal, hasta
que se conecte un proveedor de email real). NUNCA loguear el codigo en
un archivo compartido -- es una credencial de un solo uso.
"""

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

DEFAULT_USERS_DB = Path(__file__).resolve().parent.parent / "data" / "users.db"

STATUS_PENDING = "pending"        # se registro, esperando que el admin lo apruebe
STATUS_CODE_SENT = "code_sent"    # admin aprobo, codigo generado, esperando que lo ingrese
STATUS_ACTIVE = "active"          # activado, acceso completo
STATUS_REJECTED = "rejected"      # admin lo rechazo
STATUS_LEFT = "left"              # el propio usuario eligio abandonar la app (ver leave_app)

ACTIVATION_CODE_TTL_HOURS = 48

# Version del documento en webapp/templates/terms.html -- subir este
# numero cada vez que el texto cambie de forma sustantiva, para poder
# distinguir en la base de datos quien acepto la version vieja vs la
# nueva (2026-09-13, a pedido explicito del usuario: registro real de
# aceptacion, no solo un link decorativo en el formulario).
TERMS_VERSION = "1.2-2026-09-13"


def _connect(db_path: Path = DEFAULT_USERS_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            approved_at TEXT,
            activated_at TEXT
        )
        """
    )
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "terms_accepted_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN terms_accepted_at TEXT")
    if "terms_version" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN terms_version TEXT")
    if "robinhood_connected_at" not in cols:
        # Autodeclarado por el propio usuario ("ya segui la guia y conecte
        # mi Robinhood") -- NUNCA se guarda ninguna credencial real aca, la
        # conexion de verdad vive en la cuenta de Claude de cada persona
        # (conector MCP). Esto es solo para que el admin sepa quien esta
        # listo antes de habilitar dinero real de verdad (2026-09-13, a
        # pedido explicito: "no hemos creado todavia la forma en la que el
        # usuario va a agregar... su agentic").
        conn.execute("ALTER TABLE users ADD COLUMN robinhood_connected_at TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activation_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            code_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["is_admin"] = bool(d.get("is_admin", 0))
    return d


# ---------------------------------------------------------------------------
# Registro / login
# ---------------------------------------------------------------------------

def register_user(
    username: str, email: str, password: str, terms_accepted: bool = False, db_path: Path = DEFAULT_USERS_DB,
) -> dict:
    """Auto-registro de un familiar. Queda en estado 'pending' -- no puede
    entrar hasta que el admin lo apruebe y active con el codigo.

    `terms_accepted`: debe ser True -- el formulario de registro exige el
    checkbox de Terminos y Condiciones / Aviso de Riesgo marcado antes de
    poder enviar. Se guarda CUANDO y QUE VERSION acepto (terms_accepted_at,
    terms_version), no solo un booleano -- para tener registro real de que
    hubo aceptacion explicita, verificable despues si hiciera falta."""
    username = username.strip()
    email = email.strip().lower()
    if not username or not email or not password:
        raise ValueError("usuario, correo y contrasena son obligatorios")
    if not terms_accepted:
        raise ValueError("Tenes que aceptar los Terminos y Condiciones y el Aviso de Riesgo para crear la cuenta.")
    conn = _connect(db_path)
    try:
        existing = conn.execute(
            "SELECT 1 FROM users WHERE username = ? OR email = ?", (username, email)
        ).fetchone()
        if existing:
            raise ValueError("Ya existe una cuenta con ese usuario o correo.")
        password_hash = generate_password_hash(password)
        now = _now_iso()
        cur = conn.execute(
            "INSERT INTO users (username, email, password_hash, status, is_admin, created_at, "
            "terms_accepted_at, terms_version) VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
            (username, email, password_hash, STATUS_PENDING, now, now, TERMS_VERSION),
        )
        conn.commit()
        return _row_to_dict(conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def needs_terms_acceptance(user: dict) -> bool:
    """True si este usuario nunca acepto los Terminos, o si acepto una
    version VIEJA (TERMS_VERSION se subio despues de que el/ella acepto).
    Se usa en CADA request (ver login_required en app.py), no solo al
    loguearse -- si se actualiza el documento mientras alguien tiene una
    sesion abierta, se le pide aceptar la version nueva en su proximo
    click, no recien la proxima vez que inicie sesion (2026-09-13, a
    pedido explicito del usuario: "para poder usar la app... es
    obligado aceptar terminos y condiciones")."""
    return user.get("terms_accepted_at") is None or user.get("terms_version") != TERMS_VERSION


def record_terms_acceptance(user_id: int, db_path: Path = DEFAULT_USERS_DB) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE users SET terms_accepted_at = ?, terms_version = ? WHERE id = ?",
            (_now_iso(), TERMS_VERSION, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_robinhood_connected(user_id: int, connected: bool, db_path: Path = DEFAULT_USERS_DB) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE users SET robinhood_connected_at = ? WHERE id = ?",
            (_now_iso() if connected else None, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_user(username: str, db_path: Path = DEFAULT_USERS_DB) -> dict | None:
    conn = _connect(db_path)
    try:
        return _row_to_dict(conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone())
    finally:
        conn.close()


def get_user_by_id(user_id: int, db_path: Path = DEFAULT_USERS_DB) -> dict | None:
    conn = _connect(db_path)
    try:
        return _row_to_dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
    finally:
        conn.close()


def verify_password(username: str, password: str, db_path: Path = DEFAULT_USERS_DB) -> bool:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            return False
        return check_password_hash(row["password_hash"], password)
    finally:
        conn.close()


def has_any_user(db_path: Path = DEFAULT_USERS_DB) -> bool:
    conn = _connect(db_path)
    try:
        return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def create_admin_user(username: str, email: str, password: str, db_path: Path = DEFAULT_USERS_DB) -> dict:
    """Crea (o promueve) al usuario principal -- el UNICO con is_admin=1,
    activo desde el arranque (no pasa por el flujo de aprobacion, es
    quien aprueba a los demas). Pensado para el setup inicial del
    servidor, no para uso normal."""
    conn = _connect(db_path)
    try:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        now = _now_iso()
        if existing:
            conn.execute(
                "UPDATE users SET is_admin = 1, status = ?, activated_at = ? WHERE id = ?",
                (STATUS_ACTIVE, now, existing["id"]),
            )
            user_id = existing["id"]
        else:
            password_hash = generate_password_hash(password)
            cur = conn.execute(
                "INSERT INTO users (username, email, password_hash, status, is_admin, created_at, activated_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?)",
                (username, email, password_hash, STATUS_ACTIVE, now, now),
            )
            user_id = cur.lastrowid
        conn.commit()
        return _row_to_dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Panel de administrador: aprobar / rechazar / codigos de activacion
# ---------------------------------------------------------------------------

def list_pending_users(db_path: Path = DEFAULT_USERS_DB) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM users WHERE status = ? ORDER BY created_at ASC", (STATUS_PENDING,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_all_users(db_path: Path = DEFAULT_USERS_DB, include_admin: bool = False) -> list[dict]:
    """Para el panel de admin -- NUNCA incluir balance/P&L/posiciones aca,
    eso vive en la base de datos de trading de cada usuario, separada.

    include_admin=False (default) excluye la cuenta admin -- correcto para
    el panel ("no mostrarte a vos mismo en tu propia lista de familiares").
    Bug real encontrado 2026-09-14: multi_user_entry.py y health_check.py
    reusaban esta funcion tal cual para decidir a quien ESCANEAR/CHEQUEAR
    para operar -- heredaban sin querer la exclusion del admin, asi que la
    cuenta admin (activa) nunca se operaba ni se chequeaba pase lo que
    pase. Esos dos usos deben pasar include_admin=True."""
    conn = _connect(db_path)
    try:
        if include_admin:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY created_at DESC").fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def reject_user(user_id: int, db_path: Path = DEFAULT_USERS_DB) -> None:
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE users SET status = ? WHERE id = ?", (STATUS_REJECTED, user_id))
        conn.commit()
    finally:
        conn.close()


def leave_app(user_id: int, db_path: Path = DEFAULT_USERS_DB) -> None:
    """El propio usuario elige abandonar la app (a diferencia de
    disconnect_user/reject_user, que son acciones del ADMIN sobre otra
    cuenta). Estado separado ('left', no 'rejected') para que el panel de
    admin pueda distinguir "se fue por su cuenta" de "lo desconecto el
    admin" -- no borra nada (balance/posiciones/historial quedan intactos
    por si vuelve), solo bloquea el login. El admin puede reactivarla con
    el mismo boton de reconectar que ya existe para cuentas rechazadas."""
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE users SET status = ? WHERE id = ?", (STATUS_LEFT, user_id))
        conn.commit()
    finally:
        conn.close()


def disconnect_user(user_id: int, db_path: Path = DEFAULT_USERS_DB) -> None:
    """Desconecta/suspende una cuenta ya activa -- vuelve a 'rejected'
    (bloquea el login) sin borrar nada. El admin puede reconectarla
    generando un codigo nuevo (approve_and_send_code)."""
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE users SET status = ? WHERE id = ?", (STATUS_REJECTED, user_id))
        conn.commit()
    finally:
        conn.close()


def generate_activation_code(user_id: int, db_path: Path = DEFAULT_USERS_DB) -> str:
    """Aprueba al usuario (o reconecta uno desconectado) y genera un
    codigo numerico de 6 digitos, un solo uso, vence en
    ACTIVATION_CODE_TTL_HOURS horas. Devuelve el codigo EN TEXTO PLANO
    -- es la UNICA vez que existe sin hashear, quien llama es
    responsable de mostrarlo/mandarlo una sola vez."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = generate_password_hash(code)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=ACTIVATION_CODE_TTL_HOURS)
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE users SET status = ?, approved_at = ? WHERE id = ?",
            (STATUS_CODE_SENT, now.isoformat(timespec="seconds"), user_id),
        )
        conn.execute(
            "INSERT INTO activation_codes (user_id, code_hash, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (user_id, code_hash, now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")),
        )
        conn.commit()
        return code
    finally:
        conn.close()


def verify_activation_code(username: str, code: str, db_path: Path = DEFAULT_USERS_DB) -> bool:
    """Si el codigo es correcto y no vencio, activa la cuenta (status ->
    active) y marca el codigo usado. Nunca reutilizable."""
    conn = _connect(db_path)
    try:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user is None or user["status"] != STATUS_CODE_SENT:
            return False
        row = conn.execute(
            "SELECT * FROM activation_codes WHERE user_id = ? AND used_at IS NULL ORDER BY id DESC LIMIT 1",
            (user["id"],),
        ).fetchone()
        if row is None:
            return False
        if not check_password_hash(row["code_hash"], code):
            return False
        expires_at = datetime.fromisoformat(row["expires_at"])
        if datetime.now(timezone.utc) > expires_at:
            return False
        now = _now_iso()
        conn.execute("UPDATE activation_codes SET used_at = ? WHERE id = ?", (now, row["id"]))
        conn.execute("UPDATE users SET status = ?, activated_at = ? WHERE id = ?", (STATUS_ACTIVE, now, user["id"]))
        conn.commit()
        return True
    finally:
        conn.close()
