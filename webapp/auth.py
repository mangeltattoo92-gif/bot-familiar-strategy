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

# 2026-10-04, a pedido explicito del usuario: metabot pasa a ser el
# espacio PUBLICO/visible (donde cualquiera se registra, pensado para
# escalar a 500+ suscriptores) y "Bot Familiar" pasa a ser un espacio
# PARALELO oculto dentro del mismo login -- un boton "BF" en el menu que
# cualquier usuario ya logueado puede usar para PEDIR entrada, y que el
# admin aprueba o rechaza a mano desde el panel, por separado de la
# aprobacion de cuenta normal (status de arriba). Es una segunda capa de
# acceso, no reemplaza la de arriba.
FAMILIAR_ACCESS_NONE = "none"          # nunca lo pidio
FAMILIAR_ACCESS_PENDING = "pending"    # lo pidio, esperando al admin
FAMILIAR_ACCESS_APPROVED = "approved"  # admin se lo dio, ve el espacio Bot Familiar
FAMILIAR_ACCESS_REJECTED = "rejected"  # admin lo rechazo (puede volver a pedirlo)

ACTIVATION_CODE_TTL_HOURS = 48

# 2026-10-05, a pedido explicito ("los terminos y condiciones de cada
# uno separados porque bot familiar es gratis y metabot es comercial"):
# dos documentos distintos (webapp/templates/terms.html para Metabot,
# terms_familiar.html para Bot Familiar), cada uno con su propia
# version -- subir el que corresponda cada vez que ESE texto cambie de
# forma sustantiva. Que version le toca a un usuario depende de su
# familiar_access actual (ver needs_terms_acceptance/record_terms_acceptance
# mas abajo), no es un valor fijo por usuario -- si a alguien le
# aprueban el acceso a Bot Familiar despues de haber aceptado los de
# Metabot, se le vuelve a pedir aceptar (son documentos distintos de
# verdad, no la misma cosa con otro nombre).
TERMS_VERSION_METABOT = "3.0-2026-10-07"
TERMS_VERSION_FAMILIAR = "2.0-2026-10-05"


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
    if "familiar_access" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN familiar_access TEXT NOT NULL DEFAULT 'none'")
        # Backfill UNA sola vez (solo corre cuando la columna se crea por
        # primera vez): todo usuario que YA existia antes de este cambio
        # es, por definicion, un familiar/amigo ya en uso real del
        # sistema -- no tiene sentido pedirle que "solicite" algo a lo
        # que ya tenia acceso completo. Los usuarios NUEVOS (registrados
        # despues de este cambio, via el alta publica de metabot) arrancan
        # en 'none' y tienen que pedirlo como cualquiera.
        conn.execute("UPDATE users SET familiar_access = ?", (FAMILIAR_ACCESS_APPROVED,))
        # Commit INMEDIATO aca -- bug real encontrado 2026-10-04: la
        # funcion que dispara esta migracion (ej. has_any_user, un SELECT
        # cualquiera) no siempre llama conn.commit() antes de cerrar, asi
        # que el UPDATE de arriba quedaba revertido al cerrar la conexion
        # (el ALTER si persistia, por eso la columna existia pero TODOS
        # quedaban en el DEFAULT 'none' -- 7 usuarios reales de familia
        # afectados en produccion antes de notarlo).
        conn.commit()
    if "admin_scope" not in cols:
        # 2026-10-05/06, a pedido explicito ("son dos admin diferentes...
        # los usuarios de metabot se conectan con admin de metabot y los
        # de bot familiar con el admin de bot familiar"): hasta ahora
        # is_admin era un simple on/off, UN SOLO admin vela las dos marcas
        # (con pestañas de solo-filtro, no de permiso real). Ahora cada
        # cuenta admin tiene un alcance: 'all' (el admin original, ve y
        # crea todo -- default para CUALQUIER fila ya existente, admin o
        # no, no afecta a nadie) o 'metabot'/'familiar' (un admin nuevo,
        # exclusivo de esa marca -- ver _usernames_for_group en app.py,
        # que ahora fuerza el grupo segun este valor en vez de confiar en
        # el parametro de la URL).
        conn.execute("ALTER TABLE users ADD COLUMN admin_scope TEXT NOT NULL DEFAULT 'all'")
        conn.commit()
    if "plan" not in cols:
        # 2026-10-05, a pedido explicito ("ahora a metabot le vamos a
        # poner el modo pro... haz todo el proceso para los cobros
        # automaticos mensuales... primero crea toda la estructura,
        # despues agregamos stripe"): estructura lista para Stripe, sin
        # conectar todavia -- ver webapp/billing.py. 'free' para TODOS
        # los usuarios existentes (incluida la familia, que nunca paga)
        # y para cualquier alta nueva hasta que se conecte el cobro real.
        conn.execute("ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")
        conn.execute("ALTER TABLE users ADD COLUMN subscription_status TEXT NOT NULL DEFAULT 'none'")
        conn.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN plan_current_period_end TEXT")
        conn.commit()
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
    # 2026-09-17, a pedido del usuario (auditoria de seguridad real: "el
    # login no tiene limite de intentos... alguien podria probar
    # contraseñas sin parar"): sin esto, /login no tenia NINGUNA
    # proteccion contra fuerza bruta. Guardado en SQLite (no en memoria)
    # porque gunicorn corre varios workers -- un contador en memoria de
    # un solo proceso no cubriria intentos que caen en otro worker.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identifier TEXT NOT NULL,
            attempted_at TEXT NOT NULL
        )
        """
    )
    # 2026-10-11, a pedido explicito ("todos los usuarios que tengan
    # problemas para conectarse... en la seccion de Solicitudes del panel
    # de administrador"): registro persistente (no solo el contador de
    # bloqueo de login_attempts, que vive en una ventana corta) de CUALQUIER
    # problema real de conexion -- login fallido de una cuenta que SI
    # existe, o token de Tradier invalido al conectar el broker -- para
    # que el admin los vea y ayude. issue_type: 'login' o 'broker'.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS connection_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            issue_type TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL,
            resolved_at TEXT
        )
        """
    )
    # 2026-10-11, a pedido explicito ("agregar una seccion plegable para
    # enviar el correo a la persona que quiera agregar con el link
    # directo a subscribirse a bot familiar"): invitacion de un solo uso
    # para alguien que TODAVIA NO tiene cuenta -- distinto del flujo de
    # "pedir acceso" (que es para quien ya es usuario de Metabot). El
    # token nunca se guarda en texto plano (token_hash, igual que las
    # contrasenas).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS familiar_invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT
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
            # register_user() es SIEMPRE el alta publica de Metabot (Bot
            # Familiar no tiene autorregistro, ver seccion 2 de
            # terms_familiar.html) -- version de Metabot aca.
            (username, email, password_hash, STATUS_PENDING, now, now, TERMS_VERSION_METABOT),
        )
        conn.commit()
        return _row_to_dict(conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def needs_terms_acceptance(user: dict) -> bool:
    """True si este usuario nunca acepto los Terminos del espacio en el
    que esta AHORA, o si acepto una version VIEJA de ESE documento (la
    version correspondiente se subio despues de que el/ella acepto). Se
    usa en CADA request (ver login_required en app.py), no solo al
    loguearse -- si se actualiza el documento mientras alguien tiene una
    sesion abierta, se le pide aceptar la version nueva en su proximo
    click, no recien la proxima vez que inicie sesion (2026-09-13, a
    pedido explicito del usuario: "para poder usar la app... es
    obligado aceptar terminos y condiciones"). Metabot y Bot Familiar
    son documentos DISTINTOS (2026-10-05) -- a alguien que le aprueban
    el acceso a Bot Familiar despues de haber aceptado los de Metabot se
    le vuelve a pedir aceptar, porque nunca acepto ESE documento."""
    expected = TERMS_VERSION_FAMILIAR if user.get("familiar_access") == FAMILIAR_ACCESS_APPROVED else TERMS_VERSION_METABOT
    return user.get("terms_accepted_at") is None or user.get("terms_version") != expected


def record_terms_acceptance(user_id: int, db_path: Path = DEFAULT_USERS_DB) -> None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT familiar_access FROM users WHERE id = ?", (user_id,)).fetchone()
        version = TERMS_VERSION_FAMILIAR if row and row["familiar_access"] == FAMILIAR_ACCESS_APPROVED else TERMS_VERSION_METABOT
        conn.execute(
            "UPDATE users SET terms_accepted_at = ?, terms_version = ? WHERE id = ?",
            (_now_iso(), version, user_id),
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


def find_user_by_login(identifier: str, db_path: Path = DEFAULT_USERS_DB) -> dict | None:
    """Busca un usuario por su 'username' O su 'email', sin distinguir
    mayusculas/minusculas -- SOLO para el formulario de login, donde
    alguien facilmente puede escribir su correo en vez de su nombre de
    usuario (bug real encontrado 2026-09-16: un usuario nuevo no pudo
    entrar 3 veces seguidas por esto exacto). get_user() sigue igual
    (match exacto) para el resto del codigo, que siempre lo llama con
    el username canonico ya guardado en la sesion, nunca con texto que
    tipeo una persona."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE",
            (identifier, identifier),
        ).fetchone()
        return _row_to_dict(row)
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


def create_admin_user(
    username: str, email: str, password: str, db_path: Path = DEFAULT_USERS_DB,
    admin_scope: str = "all",
) -> dict:
    """Crea (o promueve) una cuenta admin, activa desde el arranque (no
    pasa por el flujo de aprobacion). `admin_scope` ('all' por defecto):
    'all' ve y administra las dos marcas (el admin original); 'metabot' o
    'familiar' queda limitado a esa marca unicamente -- 2026-10-06, a
    pedido explicito ("son dos admin diferentes... los usuarios de
    metabot se conectan con admin de metabot"), ver nota de la columna
    admin_scope en _connect()."""
    if admin_scope not in ("all", "metabot", "familiar"):
        raise ValueError("admin_scope debe ser 'all', 'metabot' o 'familiar'")
    conn = _connect(db_path)
    try:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        now = _now_iso()
        if existing:
            conn.execute(
                "UPDATE users SET is_admin = 1, admin_scope = ?, status = ?, activated_at = ? WHERE id = ?",
                (admin_scope, STATUS_ACTIVE, now, existing["id"]),
            )
            user_id = existing["id"]
        else:
            password_hash = generate_password_hash(password)
            # familiar_access se setea en linea con admin_scope ('familiar'
            # -> approved) solo para que _usernames_for_group (app.py) lo
            # ubique en el bucket correcto -- sin esto, un admin nuevo con
            # admin_scope='familiar' aparecia igual en la tabla de
            # "Suscriptores Metabot" del admin 'all' (familiar_access
            # default es 'none', que cae del lado metabot).
            familiar_access = FAMILIAR_ACCESS_APPROVED if admin_scope == "familiar" else FAMILIAR_ACCESS_NONE
            cur = conn.execute(
                "INSERT INTO users (username, email, password_hash, status, is_admin, admin_scope, "
                "familiar_access, created_at, activated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (username, email, password_hash, STATUS_ACTIVE, admin_scope, familiar_access, now, now),
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


def expire_unpaid_trials(trial_days: int = 7, db_path: Path = DEFAULT_USERS_DB) -> list[str]:
    """2026-10-11, a pedido explicito ("si no hace ese pago en un plazo
    de 7 dias la misma plataforma lo saca automaticamente del sistema"):
    desconecta (reusa disconnect_user -- 'rejected', reactivable por el
    admin con el boton 'Reconectar' que ya existe, nunca borra nada) a
    toda cuenta ACTIVA que sigue en plan Gratis mas de trial_days desde
    que se activo (activated_at) sin haber pagado Pro. Pensada para
    correr sola, periodicamente (ver trial_expiry_check.py), no para
    llamarse desde una request HTTP.

    Nunca toca: cuentas admin, cuentas ya en plan Pro (el plan cambia a
    'pro' apenas paga -- quedan afuera del filtro automaticamente, el
    aviso de vencimiento deja de aplicarles), ni cuentas con acceso
    aprobado a Bot Familiar (ese espacio es gratis siempre y nunca ve la
    pagina de precios -- mismo criterio exacto que ya usa la ruta
    subscription() para ocultarsela)."""
    conn = _connect(db_path)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=trial_days)).isoformat(timespec="seconds")
        rows = conn.execute(
            "SELECT id, username FROM users WHERE status = ? AND is_admin = 0 "
            "AND (plan IS NULL OR plan = ?) AND familiar_access != ? "
            "AND activated_at IS NOT NULL AND activated_at <= ?",
            (STATUS_ACTIVE, PLAN_FREE, FAMILIAR_ACCESS_APPROVED, cutoff),
        ).fetchall()
    finally:
        conn.close()
    expired_usernames = []
    for row in rows:
        disconnect_user(row["id"], db_path=db_path)
        expired_usernames.append(row["username"])
    return expired_usernames


def request_familiar_access(user_id: int, db_path: Path = DEFAULT_USERS_DB) -> None:
    """El propio usuario (ya logueado en metabot) pide entrar al espacio
    Bot Familiar. Solo avanza desde 'none' o 'rejected' -- si ya esta
    'pending' o 'approved' no hace nada (evita pedidos duplicados)."""
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE users SET familiar_access = ? WHERE id = ? AND familiar_access IN (?, ?)",
            (FAMILIAR_ACCESS_PENDING, user_id, FAMILIAR_ACCESS_NONE, FAMILIAR_ACCESS_REJECTED),
        )
        conn.commit()
    finally:
        conn.close()


def approve_familiar_access(user_id: int, db_path: Path = DEFAULT_USERS_DB) -> None:
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE users SET familiar_access = ? WHERE id = ?", (FAMILIAR_ACCESS_APPROVED, user_id))
        conn.commit()
    finally:
        conn.close()


def reject_familiar_access(user_id: int, db_path: Path = DEFAULT_USERS_DB) -> None:
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE users SET familiar_access = ? WHERE id = ?", (FAMILIAR_ACCESS_REJECTED, user_id))
        conn.commit()
    finally:
        conn.close()


def create_familiar_invite(email: str, db_path: Path = DEFAULT_USERS_DB) -> str:
    """El admin invita a alguien SIN cuenta todavia al plan privado --
    devuelve el token sin cifrar UNA SOLA VEZ (nunca se guarda asi, solo
    el hash). Vence a los 7 dias. Ver familiar_invite_signup() en app.py."""
    token = secrets.token_urlsafe(24)
    token_hash = generate_password_hash(token)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=7)
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO familiar_invites (email, token_hash, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (email.strip().lower(), token_hash, now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def verify_familiar_invite(token: str, db_path: Path = DEFAULT_USERS_DB) -> dict | None:
    """None si el token no existe, ya se uso, o vencio."""
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM familiar_invites WHERE used_at IS NULL").fetchall()
    finally:
        conn.close()
    for row in rows:
        if check_password_hash(row["token_hash"], token):
            if datetime.now(timezone.utc) > datetime.fromisoformat(row["expires_at"]):
                return None
            return dict(row)
    return None


def consume_familiar_invite(invite_id: int, db_path: Path = DEFAULT_USERS_DB) -> None:
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE familiar_invites SET used_at = ? WHERE id = ?", (_now_iso(), invite_id))
        conn.commit()
    finally:
        conn.close()


def register_familiar_invitee(username: str, email: str, password: str, db_path: Path = DEFAULT_USERS_DB) -> dict:
    """Alta DIRECTA (sin pasar por 'pending') para alguien invitado por el
    admin al plan privado -- el admin ya decidio al mandar la invitacion,
    asi que entra con familiar_access aprobado y status activo de una,
    nunca pisa register_user() (esa es siempre el alta publica de
    Metabot, status 'pending', documento distinto)."""
    username = username.strip()
    email = email.strip().lower()
    if not username or not email or not password:
        raise ValueError("Usuario y contrasena son obligatorios.")
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
            "INSERT INTO users (username, email, password_hash, status, is_admin, created_at, activated_at, "
            "familiar_access, terms_accepted_at, terms_version) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
            (username, email, password_hash, STATUS_ACTIVE, now, now, FAMILIAR_ACCESS_APPROVED, now, TERMS_VERSION_FAMILIAR),
        )
        conn.commit()
        return _row_to_dict(conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def list_familiar_access_requests(db_path: Path = DEFAULT_USERS_DB) -> list[dict]:
    """Para el panel de admin -- quienes pidieron entrar a Bot Familiar y
    todavia estan esperando respuesta."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM users WHERE familiar_access = ? ORDER BY created_at ASC", (FAMILIAR_ACCESS_PENDING,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_approved_familiar_usernames(db_path: Path = DEFAULT_USERS_DB) -> set[str]:
    """2026-10-11, a pedido explicito ("todavia los usuarios de bot
    familiar siguen con yahoo finance cambia la recopilacion de datos a
    tradier"): nombres de usuario con acceso aprobado al plan privado
    (Bot Familiar) -- multi_user_entry.py/app.py los suman al cohorte que
    usa Tradier como fuente de datos (ver tradier_cycle.py) en vez de
    yfinance, sin que cada uno tenga que conectar su propio token."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT username FROM users WHERE familiar_access = ? AND status = ?",
            (FAMILIAR_ACCESS_APPROVED, STATUS_ACTIVE),
        ).fetchall()
        return {r["username"] for r in rows}
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


# 2026-09-17, auditoria de seguridad real ("el login no tiene limite de
# intentos"): bloquea por `identifier` (username/correo tal cual lo
# escribio la persona, normalizado a minusculas -- no hace falta que
# exista de verdad, asi tambien protege contra probar usuarios al azar)
# despues de MAX_LOGIN_ATTEMPTS fallos dentro de LOGIN_LOCKOUT_WINDOW_MINUTES.
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_WINDOW_MINUTES = 15


def is_login_locked_out(identifier: str, db_path: Path = DEFAULT_USERS_DB) -> tuple[bool, int]:
    """True + minutos restantes de bloqueo si `identifier` tuvo
    MAX_LOGIN_ATTEMPTS o mas intentos fallidos en la ultima ventana."""
    conn = _connect(db_path)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=LOGIN_LOCKOUT_WINDOW_MINUTES)).isoformat()
        rows = conn.execute(
            "SELECT attempted_at FROM login_attempts WHERE identifier = ? AND attempted_at > ? "
            "ORDER BY attempted_at DESC LIMIT ?",
            (identifier.strip().lower(), cutoff, MAX_LOGIN_ATTEMPTS),
        ).fetchall()
        if len(rows) < MAX_LOGIN_ATTEMPTS:
            return False, 0
        oldest_of_the_last_n = datetime.fromisoformat(rows[-1]["attempted_at"])
        elapsed_min = (datetime.now(timezone.utc) - oldest_of_the_last_n).total_seconds() / 60
        remaining = max(1, round(LOGIN_LOCKOUT_WINDOW_MINUTES - elapsed_min))
        return True, remaining
    finally:
        conn.close()


def record_failed_login(identifier: str, db_path: Path = DEFAULT_USERS_DB) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO login_attempts (identifier, attempted_at) VALUES (?, ?)",
            (identifier.strip().lower(), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Plan / suscripcion (Metabot Pro) -- estructura lista para Stripe, sin
# conectar todavia (2026-10-05). Ver webapp/billing.py para la parte que
# habla con la API de Stripe de verdad.
# ---------------------------------------------------------------------------

PLAN_FREE = "free"
PLAN_PRO = "pro"

SUB_STATUS_NONE = "none"              # nunca tuvo Pro
SUB_STATUS_ACTIVE = "active"          # Pro activo, cobro al dia
SUB_STATUS_PAST_DUE = "past_due"      # Stripe reintentando un cobro fallido
SUB_STATUS_CANCELING = "canceling"    # cancelo, pero le queda periodo pago
SUB_STATUS_CANCELED = "canceled"      # ya se cancelo del todo


def get_user_by_stripe_customer_id(stripe_customer_id: str, db_path: Path = DEFAULT_USERS_DB) -> dict | None:
    """Usado por los webhooks de Stripe para encontrar a que Usuario
    corresponde un evento (Stripe manda su customer id, no nuestro
    user_id)."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE stripe_customer_id = ?", (stripe_customer_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def set_stripe_customer_id(user_id: int, stripe_customer_id: str, db_path: Path = DEFAULT_USERS_DB) -> None:
    """Se llama UNA vez, la primera vez que un Usuario inicia un checkout
    -- despues se reusa el mismo customer de Stripe para ese Usuario en
    vez de crear uno nuevo cada vez."""
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?", (stripe_customer_id, user_id))
        conn.commit()
    finally:
        conn.close()


def apply_subscription_update(
    stripe_customer_id: str,
    *,
    plan: str,
    status: str,
    stripe_subscription_id: str | None = None,
    current_period_end: str | None = None,
    db_path: Path = DEFAULT_USERS_DB,
) -> None:
    """Sincroniza el plan/estado local con lo que Stripe reporta -- se
    llama desde los webhooks (checkout.session.completed,
    customer.subscription.updated/deleted), NUNCA desde el panel
    directamente (el panel solo PIDE el cambio, Stripe confirma con el
    webhook que de verdad paso)."""
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE users SET plan = ?, subscription_status = ?, "
            "stripe_subscription_id = COALESCE(?, stripe_subscription_id), "
            "plan_current_period_end = ? WHERE stripe_customer_id = ?",
            (plan, status, stripe_subscription_id, current_period_end, stripe_customer_id),
        )
        conn.commit()
    finally:
        conn.close()


def can_open_positions(user: dict | None) -> bool:
    """2026-10-06, a pedido explicito ("cuando los subscriptores de metabot
    cumplan los requisitos y hagan el pago van a tener acceso a operar"):
    Bot Familiar es gratis (acceso aprobado por el admin, sin cobro); en
    Metabot solo puede abrir posiciones nuevas quien tenga Pro activo al
    dia. Las salidas no dependen de esto -- una cuenta sin Pro puede cerrar
    lo que ya tiene abierto."""
    if not user:
        return False
    if user.get("familiar_access") == FAMILIAR_ACCESS_APPROVED:
        return True
    return user.get("plan") == PLAN_PRO and user.get("subscription_status") == SUB_STATUS_ACTIVE


def set_plan_manual(username: str, plan: str, db_path: Path = DEFAULT_USERS_DB) -> None:
    """Para el admin mientras Stripe no esta conectado: activa o quita Pro a
    mano. Cuando Stripe este conectado, el cambio real lo hace el webhook
    (apply_subscription_update) y esto deja de usarse."""
    if plan not in (PLAN_FREE, PLAN_PRO):
        raise ValueError("plan debe ser 'free' o 'pro'")
    status = SUB_STATUS_ACTIVE if plan == PLAN_PRO else SUB_STATUS_NONE
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE users SET plan = ?, subscription_status = ? WHERE username = ?",
            (plan, status, username),
        )
        conn.commit()
    finally:
        conn.close()


def clear_login_attempts(identifier: str, db_path: Path = DEFAULT_USERS_DB) -> None:
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM login_attempts WHERE identifier = ?", (identifier.strip().lower(),))
        conn.commit()
    finally:
        conn.close()


def record_connection_issue(username: str, issue_type: str, detail: str, db_path: Path = DEFAULT_USERS_DB) -> None:
    """issue_type: 'login' (cuenta real, contrasena incorrecta) o 'broker'
    (token de Tradier invalido al conectar). Ver connection_issues en
    _connect() para el motivo completo."""
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO connection_issues (username, issue_type, detail, created_at) VALUES (?, ?, ?, ?)",
            (username, issue_type, detail, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def list_connection_issues(db_path: Path = DEFAULT_USERS_DB) -> list[dict]:
    """Para el panel de admin -- solo las NO resueltas, mas recientes primero."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM connection_issues WHERE resolved_at IS NULL ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def resolve_connection_issue(issue_id: int, db_path: Path = DEFAULT_USERS_DB) -> None:
    conn = _connect(db_path)
    try:
        conn.execute("UPDATE connection_issues SET resolved_at = ? WHERE id = ?", (_now_iso(), issue_id))
        conn.commit()
    finally:
        conn.close()
