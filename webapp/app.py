"""
Panel local de monitoreo del bot de paper trading.

No coloca ninguna orden REAL, nunca. La unica accion de escritura desde
aqui es /api/positions/close: cierra (vende, en modo SIMULADO) una posicion
abierta especifica a peticion explicita del usuario en el panel -- cada
boton de cierre actua solo sobre su propia posicion, identificada por su
key, sin afectar ninguna otra.
Pensado para ejecutarse SOLO en localhost (127.0.0.1).
"""

import json
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paper_trading.bollinger_strategy import is_near_session_close  # noqa: E402
from paper_trading.engine import (  # noqa: E402
    PaperTradingError,
    _connect,
    _ensure_trades_columns,
    _position_key,
    get_account_value_history,
    get_error_log,
    get_history,
    get_position_by_key,
    get_settings,
    get_status,
    get_trades_count_today,
    init_db,
    record_trade,
    update_settings,
)
from paper_trading.family_sizing import estimate_affordable_symbols  # noqa: E402
from paper_trading import nyse_calendar  # noqa: E402
from multi_user_entry import _current_exit_price  # noqa: E402
from paper_trading.trade_journal import daily_breakdown, get_closed_trades, get_todays_closed_trades, weekly_summary  # noqa: E402
from webapp import billing, market_data  # noqa: E402
from webapp.email_sender import send_activation_email, send_alert_email  # noqa: E402
from webapp.brokers import store as broker_store  # noqa: E402
from webapp.brokers.tradier import TradierClient, TradierError  # noqa: E402
from webapp import terms_signing  # noqa: E402
from webapp.news import (  # noqa: E402
    has_unread_news,
    list_news_for_user,
    mark_all_read,
)
from webapp.support import (  # noqa: E402
    has_unread_support,
    list_support_thread,
    list_support_threads_summary,
    mark_thread_read,
    send_support_message,
)
from webapp.auth import (  # noqa: E402
    FAMILIAR_ACCESS_APPROVED,
    PLAN_FREE,
    can_open_positions,
    set_plan_manual,
    FAMILIAR_ACCESS_NONE,
    FAMILIAR_ACCESS_PENDING,
    FAMILIAR_ACCESS_REJECTED,
    STATUS_ACTIVE,
    STATUS_CODE_SENT,
    STATUS_LEFT,
    STATUS_PENDING,
    STATUS_REJECTED,
    approve_familiar_access,
    clear_login_attempts,
    consume_familiar_invite,
    create_admin_user,
    create_familiar_invite,
    disconnect_user,
    find_user_by_login,
    generate_activation_code,
    get_user,
    get_user_by_id,
    has_any_user,
    is_login_locked_out,
    leave_app,
    list_all_users,
    list_approved_familiar_usernames,
    list_connection_issues,
    list_familiar_access_requests,
    list_pending_users,
    needs_terms_acceptance,
    record_connection_issue,
    record_failed_login,
    record_terms_acceptance,
    register_familiar_invitee,
    register_user,
    reject_familiar_access,
    reject_user,
    request_familiar_access,
    resolve_connection_issue,
    verify_familiar_invite,
    verify_activation_code,
    verify_password,
)

# 2026-09-17: la seccion de novedades empezo mostrandose solo para
# miguel perez ("agregamelo a mi para ver como se ve y despues decidimos
# si agregarla a los otros usuarios") -- a pedido explicito del usuario
# el mismo dia, ya se extendio a todas las cuentas.
NEWS_ENABLED_FOR_ALL = True

# 2026-09-17, a pedido del usuario ("al lado de donde dice dinero real
MARKET_TZ = ZoneInfo("America/New_York")
PREMARKET_OPEN_TIME = dtime(4, 0)
MARKET_OPEN_TIME = dtime(9, 30)
MARKET_CLOSE_TIME = dtime(16, 0)

# Cada usuario tiene su PROPIA base de datos de trading, totalmente
# separada de la de los demas (2026-09-10 -- reusa el patron ya probado
# hoy en trading-bot/family_sim.py con 10 cuentas simuladas, en vez de
# agregar user_id a las tablas compartidas de paper_trading/engine.py).
USERS_DATA_DIR = ROOT / "data" / "users"
DEFAULT_INITIAL_BALANCE = 100000.0  # 2026-10-11: mismo capital con el que arranca por defecto una cuenta sandbox de Tradier, a pedido explicito ("el balance de papel money de tradier es de 100 000 por defecto entonces ese seria el monto de cada persona")

# 2026-10-07: cuentas cuyo ciclo de entradas/salidas corre con datos de
# Tradier en vez de yfinance (ver tradier_cycle.py / multi_user_entry.py)
# -- mismo archivo que usa el motor, se lee aca solo para mostrar el
# indicador en el panel, no para decidir nada. 2026-10-11: se le suman
# los usuarios con una conexion Tradier sandbox propia conectada desde
# "Mi suscripcion" (ver webapp/brokers/store.tradier_paper_usernames) --
# mismo criterio dinamico que ya usa multi_user_entry.py, para que el
# aviso en el panel no quede desactualizado respecto a lo que el motor
# realmente procesa.
TRADIER_ONLY_USERS_FILE = ROOT / "data" / "tradier_only_users.json"


def _tradier_only_usernames_set() -> set[str]:
    try:
        usernames = set(json.loads(TRADIER_ONLY_USERS_FILE.read_text(encoding="utf-8")))
    except Exception:
        usernames = set()
    try:
        from webapp.brokers.store import tradier_paper_usernames
        usernames |= tradier_paper_usernames()
    except Exception:
        pass
    try:
        # 2026-10-11: todo el plan privado (Bot Familiar) usa Tradier por
        # defecto -- mismo criterio que multi_user_entry.py, para que
        # este aviso visual no quede desactualizado.
        usernames |= list_approved_familiar_usernames()
    except Exception:
        pass
    return usernames


def _tradier_active_for(username: str | None) -> bool:
    return bool(username) and username in _tradier_only_usernames_set()


TRIAL_DAYS = 7
TRIAL_WARNING_DAYS = 5  # a partir de este dia muestra el conteo regresivo


def _trial_deadline_for(user: dict | None, brand_space: str) -> str | None:
    """2026-10-11, a pedido explicito: devuelve la fecha limite (ISO) para
    que una cuenta Metabot en plan Gratis pague Pro antes de que
    trial_expiry_check.py la desconecte, o None si no aplica (ya es Pro,
    es admin, o esta en el espacio Bot Familiar -- gratis siempre, nunca
    ve el aviso de precios). El JS del dashboard decide si ya esta
    dentro de la ventana de aviso (ultimos TRIAL_DAYS - TRIAL_WARNING_DAYS
    dias) comparando esta fecha contra la hora actual."""
    if not user or user.get("is_admin") or brand_space == "familiar":
        return None
    if (user.get("plan") or PLAN_FREE) != PLAN_FREE:
        return None
    activated_at = user.get("activated_at")
    if not activated_at:
        return None
    try:
        activated = datetime.fromisoformat(activated_at)
    except ValueError:
        return None
    return (activated + timedelta(days=TRIAL_DAYS)).isoformat(timespec="seconds")


def _user_db_path() -> Path:
    """Ruta a la base de datos de trading del usuario ACTUALMENTE
    logueado (session['user']). Se crea sola (init_db) la primera vez
    que se pide, con el balance inicial por defecto."""
    username = session.get("user")
    db_path = USERS_DATA_DIR / username / "paper_trading.db"
    if not db_path.exists():
        init_db(initial_balance=DEFAULT_INITIAL_BALANCE, db_path=db_path)
    return db_path


def get_market_status() -> dict:
    """Fase del mercado de EEUU: premarket (4:00-9:30 ET), regular
    (9:30 ET a la hora de cierre real de ese dia -- 16:00 normal, 13:00 en
    cierre anticipado) o cerrado. Tiene en cuenta festivos del NYSE (ver
    paper_trading.nyse_calendar) -- un dia de semana que sea festivo (ej.
    Labor Day) se marca como cerrado todo el dia, no solo fuera de horario.
    El analisis de la estrategia (bollinger_strategy, con prepost=True) ya
    vigila el mercado tambien durante el premarket."""
    now_et = datetime.now(timezone.utc).astimezone(MARKET_TZ)
    today = now_et.date()
    is_trading_day = nyse_calendar.is_trading_day(today)
    t = now_et.time()
    market_close = nyse_calendar.market_close_time(today) if is_trading_day else MARKET_CLOSE_TIME

    is_premarket = is_trading_day and PREMARKET_OPEN_TIME <= t < MARKET_OPEN_TIME
    is_open = is_trading_day and MARKET_OPEN_TIME <= t < market_close

    if is_open:
        phase, label = "open", "Mercado abierto — operando"
    elif is_premarket:
        phase, label = "premarket", "Pre-mercado — analizando"
    elif not is_trading_day and today.weekday() < 5:
        phase, label = "holiday", "Mercado cerrado — festivo del NYSE"
    else:
        phase, label = "closed", "Mercado cerrado"

    monday = today - timedelta(days=today.weekday())
    day_initials = ["L", "M", "X", "J", "V", "S", "D"]
    week_days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        week_days.append({
            "date": d.isoformat(),
            "initial": day_initials[i],
            "is_today": d == today,
            "is_trading_day": nyse_calendar.is_trading_day(d) if d.weekday() < 5 else False,
        })

    return {
        "is_open": is_open,
        "is_premarket": is_premarket,
        "is_trading_day": is_trading_day,
        "is_early_close": is_trading_day and nyse_calendar.is_early_close(today),
        "phase": phase,
        "time_et": now_et.strftime("%H:%M"),
        "label": label,
        "week_days": week_days,
    }


SECRET_KEY_PATH = ROOT / "data" / "secret_key.txt"


def _load_or_create_secret_key() -> str:
    SECRET_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_text().strip()
    key = secrets.token_hex(32)
    SECRET_KEY_PATH.write_text(key)
    return key


app = Flask(__name__)
app.secret_key = _load_or_create_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # 2026-09-17, auditoria de seguridad real: sin esto, la cookie de
    # sesion tecnicamente podia viajar por HTTP plano si alguna vez
    # existiera esa ruta -- en produccion siempre se entra por nginx con
    # TLS real (Let's Encrypt), asi que forzarla a HTTPS-only no rompe
    # nada real y cierra ese hueco.
    SESSION_COOKIE_SECURE=True,
    TEMPLATES_AUTO_RELOAD=True,
)


@app.template_global()
def asset_url(filename: str) -> str:
    """url_for('static', ...) + `?v=<mtime>` -- cache-buster real (2026-09-14,
    a pedido del usuario: cambios de UI que no se veian reflejados en el
    navegador aunque el servidor ya tenia el archivo nuevo, confirmado NO
    era un bug del backend -- el navegador/PWA se quedaba con la version
    vieja en cache pese al header Cache-Control: no-cache). Cada deploy
    cambia el mtime del archivo -> cambia la URL -> el navegador esta
    OBLIGADO a pedir el archivo nuevo, sin depender de que el usuario haga
    un refresh forzado."""
    path = Path(app.static_folder) / filename
    try:
        version = int(path.stat().st_mtime)
    except OSError:
        version = 0
    return f"{url_for('static', filename=filename)}?v={version}"


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login"))
        # Chequeo de estado EN CADA REQUEST, no solo al loguearse -- si el
        # admin desconecta a alguien mientras tenia una sesion abierta, se
        # le corta el acceso de inmediato en el proximo click, no recien
        # cuando vuelva a intentar loguearse.
        user = get_user(session["user"])
        if user is None or user["status"] != STATUS_ACTIVE:
            session.clear()
            return redirect(url_for("login"))
        # Obligatorio en CADA request, no solo al loguearse -- si se
        # actualiza el documento de Terminos mientras alguien tiene una
        # sesion abierta, se le pide aceptar la version nueva antes de
        # seguir usando la app (2026-09-13, a pedido explicito del
        # usuario). accept_terms() NO pasa por aca (queda afuera de este
        # chequeo) para no generar un loop de redirects.
        if needs_terms_acceptance(user):
            return redirect(url_for("accept_terms"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if not has_any_user():
        return render_template(
            "login.html",
            error="No hay ningun usuario administrador creado todavia. Ejecuta "
                  "'python webapp/manage_users.py --admin <usuario> <correo>' en tu terminal.",
        )
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        # 2026-09-17, auditoria de seguridad real ("el login no tiene
        # limite de intentos"): bloquea ANTES de tocar la base de
        # usuarios/hashear nada, para que un atacante ni siquiera pueda
        # usar el tiempo de respuesta para distinguir cuentas que existen
        # de las que no. Se chequea por lo que la persona ESCRIBIO (no
        # el username canonico), asi tambien cubre el caso de alguien
        # probando usuarios al azar.
        locked, minutes_left = is_login_locked_out(username) if username else (False, 0)
        if locked:
            error = f"Demasiados intentos fallidos. Espera {minutes_left} minuto(s) antes de volver a intentar."
            return render_template("login.html", error=error)
        # 2026-09-16, bug real encontrado: un usuario nuevo escribio su
        # correo en el campo de usuario y no pudo entrar 3 veces seguidas
        # -- find_user_by_login() acepta username O email, sin distinguir
        # mayusculas/minusculas. verify_password() sigue necesitando el
        # username CANONICO (guardado en la BD), no lo que la persona
        # tipeo, por eso se usa user["username"] una vez resuelto.
        user = find_user_by_login(username) if username else None
        if not username or not password or user is None or not verify_password(user["username"], password):
            if username:
                record_failed_login(username)
            # 2026-10-11, a pedido explicito ("todos los usuarios que
            # tengan problemas para conectarse... en la seccion de
            # Solicitudes"): solo se registra si la cuenta SI existe
            # (contrasena incorrecta de un usuario real) -- un username
            # que no existe es ruido (gente probando al azar, bots), no
            # "alguien con problemas" que el admin deba ayudar.
            if user is not None:
                record_connection_issue(user["username"], "login", "Contraseña incorrecta.")
            error = "Usuario o contrasena incorrectos."
        elif user["status"] == STATUS_PENDING:
            error = "Tu cuenta todavia esta pendiente de aprobacion."
        elif user["status"] == STATUS_CODE_SENT:
            session["pending_activation_user"] = username
            return redirect(url_for("activate"))
        elif user["status"] == STATUS_REJECTED:
            error = "Tu cuenta no tiene acceso. Consulta con el administrador."
        elif user["status"] == STATUS_LEFT:
            error = "Elegiste abandonar la app. Si querés volver, consultá con el administrador para reactivar tu cuenta."
        else:
            clear_login_attempts(username)
            session.clear()
            session["user"] = username
            session["is_admin"] = bool(user["is_admin"])
            session["admin_scope"] = user.get("admin_scope") or "all"
            return redirect(url_for("dashboard"))
    return render_template("login.html", error=error)


@app.route("/familiar-acceso", methods=["GET", "POST"])
def familiar_login():
    """Puerta de entrada APARTE para Bot Familiar -- a pedido explicito
    del usuario ("el link de bot familiar tiene que ser diferente al de
    metabot porque el de bot familiar es personal y familiar"). Misma
    autenticacion que /login (misma tabla de usuarios, mismas reglas de
    bloqueo), pero con su propia URL y marca -- NO esta enlazada desde
    ninguna pagina publica (landing, /login, /registro); solo la
    conocen quienes ya la tienen a mano. No tiene link de "crear
    cuenta" -- el alta a Bot Familiar es por solicitud desde adentro de
    Metabot (ver /familiar/solicitar), no auto-registro publico aca."""
    if not has_any_user():
        return render_template(
            "familiar_login.html",
            error="No hay ningun usuario administrador creado todavia. Ejecuta "
                  "'python webapp/manage_users.py --admin <usuario> <correo>' en tu terminal.",
        )
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        locked, minutes_left = is_login_locked_out(username) if username else (False, 0)
        if locked:
            error = f"Demasiados intentos fallidos. Espera {minutes_left} minuto(s) antes de volver a intentar."
            return render_template("familiar_login.html", error=error)
        user = find_user_by_login(username) if username else None
        if not username or not password or user is None or not verify_password(user["username"], password):
            if username:
                record_failed_login(username)
            if user is not None:
                record_connection_issue(user["username"], "login", "Contraseña incorrecta (entrada Bot Familiar).")
            error = "Usuario o contrasena incorrectos."
        elif user["status"] == STATUS_PENDING:
            error = "Tu cuenta todavia esta pendiente de aprobacion."
        elif user["status"] == STATUS_CODE_SENT:
            session["pending_activation_user"] = username
            return redirect(url_for("activate"))
        elif user["status"] == STATUS_REJECTED:
            error = "Tu cuenta no tiene acceso. Consulta con el administrador."
        elif user["status"] == STATUS_LEFT:
            error = "Elegiste abandonar la app. Si querés volver, consultá con el administrador para reactivar tu cuenta."
        else:
            clear_login_attempts(username)
            session.clear()
            session["user"] = username
            session["is_admin"] = bool(user["is_admin"])
            session["admin_scope"] = user.get("admin_scope") or "all"
            return redirect(url_for("dashboard"))
    return render_template("familiar_login.html", error=error)


@app.route("/bienvenida")
def landing():
    """Pagina publica de presentacion de Metabot -- sin login, pensada
    para compartir como la URL de entrada para el publico en general
    (2026-10-04, a pedido explicito: presentacion con calidad/estilo
    premium como referencia de diseño que paso el usuario)."""
    return render_template(
        "landing.html",
        current_year=datetime.now(timezone.utc).year,
        pro_price=billing.PRO_PLAN_DISPLAY_PRICE_USD,
    )


@app.route("/terminos")
def terms():
    return render_template("terms.html")


@app.route("/familiar-terminos")
def terms_familiar():
    return render_template("terms_familiar.html")


@app.route("/suscripcion")
@login_required
def subscription():
    """Plan/suscripcion de Metabot Pro (2026-10-05). Bot Familiar es
    gratis siempre -- a un Usuario de ese espacio ni se le muestra esta
    pagina (mismo principio de "no sabe que existe Metabot" aplicado
    tambien a precios: se lo manda de vuelta al dashboard)."""
    user = get_user(session["user"])
    brand = _brand_for_user(user)
    if brand["brand_space"] == "familiar":
        return redirect(url_for("dashboard"))
    return render_template(
        "subscription.html",
        username=session.get("user"),
        is_admin=session.get("is_admin", False),
        plan=user.get("plan", "free"),
        subscription_status=user.get("subscription_status", "none"),
        plan_current_period_end=user.get("plan_current_period_end"),
        billing_configured=billing.is_configured(),
        pro_price=billing.PRO_PLAN_DISPLAY_PRICE_USD,
        checkout=request.args.get("checkout"),
        canceled=request.args.get("canceled"),
        error=request.args.get("error"),
        firmado=request.args.get("firmado"),
        signed_terms=terms_signing.has_signed(user["id"]),
        **brand,
    )


@app.route("/suscripcion/iniciar-pro", methods=["POST"])
@login_required
def subscription_start_pro():
    # 2026-10-07, a pedido explicito ("una vez que el cliente haya hecho y
    # cumplido todos los pasos y requisitos pasa directo al panel de
    # usuario"): tras un pago exitoso, Stripe manda al usuario derecho a
    # SU panel (no se queda en la pagina de suscripcion) -- el flag
    # pro=ok dispara un aviso breve ahi mismo (ver dashboard.html). Si
    # cancela el checkout sin pagar, sigue volviendo a /suscripcion (ahi
    # si tiene sentido mostrarle que no se cobro nada).
    user = get_user(session["user"])
    # 2026-10-07, a pedido explicito ("una vez que lo firme... es que puede
    # continuar el proceso de agregar tarjeta"): sin firma de la version
    # VIGENTE de los Terminos, no se deja avanzar al pago -- lo manda
    # primero a firmar.
    if terms_signing.has_signed(user["id"]) is None:
        return redirect(url_for("sign_terms", next="pro"))
    checkout_url = billing.create_checkout_session(
        user,
        success_url=request.url_root.rstrip("/") + url_for("dashboard") + "?pro=ok",
        cancel_url=request.url_root.rstrip("/") + url_for("subscription") + "?checkout=cancelado",
    )
    if checkout_url:
        return redirect(checkout_url)
    return redirect(url_for("subscription", error="procesador_no_configurado"))


@app.route("/terminos/firmar", methods=["GET", "POST"])
@login_required
def sign_terms():
    """2026-10-07, a pedido explicito: documento de Terminos con nombre
    completo + firma dibujada, que genera un PDF guardado en el servidor
    (data/terms_signed/) e identificado por usuario -- requisito antes de
    pagar Pro y conectar el broker (ver gate en subscription_start_pro)."""
    user = get_user(session["user"])
    existing = terms_signing.has_signed(user["id"])
    if request.method == "POST":
        if existing is not None:
            return redirect(url_for("sign_terms", next=request.args.get("next", "")))
        full_name = (request.form.get("full_name") or "").strip()
        signature_data_url = request.form.get("signature_data_url") or ""
        accepted = request.form.get("accepted") == "on"
        if not accepted:
            return render_template(
                "sign_terms.html", username=session.get("user"), is_admin=session.get("is_admin", False),
                sections=terms_signing.TERMS_SECTIONS, terms_version=terms_signing.TERMS_TEXT_VERSION,
                signed=None, error="Tenés que marcar la casilla de \"Leído y firmado\" para continuar.",
                full_name=full_name, next=request.args.get("next", ""),
            )
        try:
            terms_signing.save_signature(
                user["id"], session["user"], full_name, signature_data_url,
                ip_address=request.remote_addr or "",
            )
        except ValueError as e:
            return render_template(
                "sign_terms.html", username=session.get("user"), is_admin=session.get("is_admin", False),
                sections=terms_signing.TERMS_SECTIONS, terms_version=terms_signing.TERMS_TEXT_VERSION,
                signed=None, error=str(e), full_name=full_name, next=request.args.get("next", ""),
            )
        # 2026-10-07: vuelve a /suscripcion ya firmado -- ahi el boton
        # "Suscribirme a Pro" pasa el gate de arriba sin problema (POST
        # normal, no hace falta una ruta intermedia).
        # 2026-10-11, plan privado (Bot Familiar): no hay pago que pasar
        # -- firmado esto, vuelve derecho al panel (ahi mismo esta ahora
        # el conector de broker, ver dashboard.html).
        if request.args.get("next") == "privado":
            return redirect(url_for("familiar_space"))
        return redirect(url_for("subscription", firmado="ok"))

    return render_template(
        "sign_terms.html", username=session.get("user"), is_admin=session.get("is_admin", False),
        sections=terms_signing.TERMS_SECTIONS, terms_version=terms_signing.TERMS_TEXT_VERSION,
        signed=existing, error=None, full_name="", next=request.args.get("next", ""),
    )


@app.route("/terminos/mi-pdf")
@login_required
def download_my_signed_terms():
    user = get_user(session["user"])
    record = terms_signing.has_signed(user["id"])
    if record is None:
        return jsonify({"error": "Todavía no firmaste los Términos vigentes."}), 404
    path = Path(record["pdf_path"])
    if not path.exists():
        return jsonify({"error": "El archivo ya no está disponible."}), 404
    return send_file(path, as_attachment=True, download_name=f"terminos-metabot-{session['user']}.pdf")


@app.route("/admin/terminos-firmados/<int:signing_id>/pdf")
@admin_required
def admin_download_signed_terms(signing_id: int):
    record = terms_signing.get_signature_by_id(signing_id)
    if record is None:
        return jsonify({"error": "No existe esa firma."}), 404
    path = Path(record["pdf_path"])
    if not path.exists():
        return jsonify({"error": "El archivo ya no está disponible."}), 404
    return send_file(path, as_attachment=True, download_name=f"terminos-{record['username']}.pdf")


@app.route("/suscripcion/cancelar", methods=["POST"])
@login_required
def subscription_cancel():
    user = get_user(session["user"])
    ok = billing.cancel_subscription(user.get("stripe_subscription_id"))
    if ok:
        return redirect(url_for("subscription", canceled="ok"))
    return redirect(url_for("subscription", error="no_se_pudo_cancelar"))


@app.route("/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    """Endpoint que Stripe llama directamente (nunca un navegador) para
    avisar que paso de verdad con un cobro/suscripcion -- NO requiere
    sesion de usuario (Stripe no tiene una). La verificacion de que el
    request es realmente de Stripe la hace billing.handle_webhook_event
    via la firma Stripe-Signature, no el login."""
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    ok, message = billing.handle_webhook_event(payload, sig_header)
    return jsonify({"received": ok, "message": message}), (200 if ok else 400)


@app.route("/aceptar-terminos", methods=["GET", "POST"])
def accept_terms():
    # Guardia liviana propia (NO login_required): necesita sesion activa
    # como cualquier pagina protegida, pero justamente NO puede exigir
    # "terminos ya aceptados" -- es la pagina que hace que se acepten.
    if not session.get("user"):
        return redirect(url_for("login"))
    user = get_user(session["user"])
    if user is None or user["status"] != STATUS_ACTIVE:
        session.clear()
        return redirect(url_for("login"))
    if not needs_terms_acceptance(user):
        return redirect(url_for("dashboard"))

    error = None
    if request.method == "POST":
        if request.form.get("terms_accepted") != "on":
            error = "Tenes que marcar la casilla para poder continuar."
        else:
            record_terms_acceptance(user["id"])
            session["is_admin"] = bool(user["is_admin"])
            session["admin_scope"] = user.get("admin_scope") or "all"
            return redirect(url_for("dashboard"))
    return render_template("accept_terms.html", error=error, **_brand_for_user(user))


@app.route("/registro", methods=["GET", "POST"])
def signup():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        terms_accepted = request.form.get("terms_accepted") == "on"
        if not username or not email or not password:
            error = "Usuario, correo y contrasena son obligatorios."
        elif not terms_accepted:
            error = "Tenes que aceptar los Terminos y Condiciones y el Aviso de Riesgo para crear la cuenta."
        else:
            try:
                register_user(username, email, password, terms_accepted=True)
                # Cuenta de trading creada YA con capital de papel fijo
                # (2026-09-15, a pedido del usuario -- revierte el capital
                # inicial elegido por cada quien del 2026-09-10: ahora todos
                # arrancan con el mismo monto generico DEFAULT_INITIAL_BALANCE,
                # sin pedirlo en el formulario). No hace falta esperar a
                # que se active -- la cuenta de trading es independiente
                # del estado de aprobacion, solo el LOGIN esta bloqueado
                # hasta 'active'.
                db_path = USERS_DATA_DIR / username / "paper_trading.db"
                init_db(initial_balance=DEFAULT_INITIAL_BALANCE, db_path=db_path)
                return render_template(
                    "signup.html",
                    success="Cuenta creada -- queda pendiente hasta que el administrador la apruebe. "
                            "Te va a llegar un codigo cuando este lista para activar.",
                )
            except ValueError as e:
                error = str(e)
    return render_template("signup.html", error=error)


@app.route("/activar", methods=["GET", "POST"])
def activate():
    username = session.get("pending_activation_user")
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip() or username
        code = request.form.get("code", "").strip()
        if username and code and verify_activation_code(username, code):
            session.pop("pending_activation_user", None)
            return render_template("activate.html", success=True)
        error = "Codigo incorrecto o vencido."
    return render_template("activate.html", username=username, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/leave-app", methods=["POST"])
@login_required
def api_leave_app():
    """Accion del propio usuario (no del admin) para abandonar la app --
    parametro de confirmacion: reingresar su propia contrasena, igual que
    cualquier accion destructiva de cuenta (patron estandar, evita un
    click accidental o de otra persona con la sesion abierta). No borra
    balance/posiciones/historial -- solo bloquea el login (ver leave_app
    en webapp/auth.py). La cuenta admin no puede usar esto -- no tiene
    sentido "abandonar" la cuenta principal desde aca."""
    if session.get("is_admin"):
        return jsonify({"error": "La cuenta administradora no puede abandonarse desde aca."}), 400
    password = (request.get_json(silent=True) or {}).get("password", "")
    username = session["user"]
    if not password or not verify_password(username, password):
        return jsonify({"error": "Contraseña incorrecta."}), 400
    user = get_user(username)
    leave_app(user["id"])
    session.clear()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Panel de administrador -- SOLO el usuario principal. NUNCA expone
# balance/P&L/posiciones de nadie (ver CLAUDE.md "CORRECCION DE ALCANCE").
# ---------------------------------------------------------------------------

@app.route("/admin")
@admin_required
def admin_panel():
    # 2026-09-23, a pedido del admin ("en el panel de administrador crea la
    # funcion de conectar manual porque no la tengo"): el admin no tenia
    # forma de reactivar a mano una cuenta que el circuit breaker apago
    # (bot_enabled) -- tenia que pedirmelo a mi. bot_enabled vive en la
    # base de trading de CADA usuario (paper_trading.db), no en users.db,
    # asi que se lee aparte por cuenta aca.
    # 2026-10-06, a pedido explicito ("son dos admin diferentes"): esta
    # tabla completa de usuarios/correos (y pending/familiar_requests mas
    # abajo) se pasaba SIN filtrar por marca -- un admin con admin_scope
    # limitado veia igual los usuarios, correos y solicitudes de la otra
    # marca. _usernames_for_group ya fuerza el alcance por sesion.
    allowed = _usernames_for_group(None)
    users = list_all_users(include_admin=True)
    if allowed is not None:
        users = [u for u in users if u["username"] in allowed]
    for u in users:
        if u["status"] == "active" and not u["is_admin"]:
            db_path = USERS_DATA_DIR / u["username"] / "paper_trading.db"
            u["bot_enabled"] = get_settings(db_path=db_path)["bot_enabled"] if db_path.exists() else True
        else:
            u["bot_enabled"] = None
    admin_scope = session.get("admin_scope", "all")
    # list_pending_users() son siempre altas publicas de Metabot (Bot
    # Familiar no tiene auto-registro); list_familiar_access_requests()
    # son siempre pedidos de acceso a Bot Familiar -- un admin limitado a
    # la OTRA marca no tiene por que ver ninguna de las dos.
    pending = list_pending_users() if admin_scope in ("all", "metabot") else []
    familiar_requests = list_familiar_access_requests() if admin_scope in ("all", "familiar") else []
    # 2026-10-04, a pedido explicito ("los de bot familiar no tienen que
    # ver metabot"): el admin es, por definicion, alguien ya adentro de
    # Bot Familiar -- su propio panel tampoco debe mostrar marca metabot.
    admin_user = get_user(session["user"])
    # 2026-10-11, a pedido explicito ("todos los usuarios que tengan
    # problemas para conectarse... en la seccion de Solicitudes del panel
    # de administrador"): mismo filtro de marca que ya usa `users` arriba
    # -- un admin limitado a una marca no tiene por que ver problemas de
    # cuentas de la otra.
    connection_issues = list_connection_issues()
    if allowed is not None:
        connection_issues = [i for i in connection_issues if i["username"] in allowed]
    return render_template(
        "admin.html",
        pending=pending,
        users=users,
        username=session.get("user"),
        familiar_requests=familiar_requests,
        admin_scope=admin_scope,
        tradier_only_usernames=_tradier_only_usernames_set(),
        signed_terms_list=terms_signing.list_all_signed(),
        terms_text_version=terms_signing.TERMS_TEXT_VERSION,
        connection_issues=connection_issues,
        **_brand_for_user(admin_user),
    )


@app.route("/admin/enable-bot/<int:user_id>", methods=["POST"])
@admin_required
def admin_enable_bot(user_id: int):
    """Reactiva a mano el bot de una cuenta que el circuit breaker apago
    (bot_enabled=0) -- 2026-09-23, mismo efecto que el interruptor que ya
    tiene cada usuario en su propio panel (api_bot_status), pero desde el
    panel de administrador para que el admin no tenga que pedirmelo."""
    user = get_user_by_id(user_id)
    if user is None:
        return jsonify({"error": "Usuario no encontrado."}), 404
    db_path = USERS_DATA_DIR / user["username"] / "paper_trading.db"
    if not db_path.exists():
        return jsonify({"error": "Esta cuenta todavia no tiene datos de trading."}), 404
    settings = update_settings(bot_enabled=True, breaker_override_today=True, db_path=db_path)
    return jsonify({"ok": True, "username": user["username"], "enabled": settings["bot_enabled"]})


@app.route("/admin/approve/<int:user_id>", methods=["POST"])
@admin_required
def admin_approve(user_id: int):
    user = get_user_by_id(user_id)
    if user is None:
        return jsonify({"error": "Usuario no encontrado."}), 404
    code = generate_activation_code(user_id)
    # Se manda por correo real via Gmail SMTP (2026-09-13). Si las
    # credenciales no estan configuradas o el envio falla (red, Gmail
    # caido, etc.), send_activation_email devuelve False sin lanzar --
    # el codigo se devuelve igual en la respuesta para que el admin se
    # lo pase a mano como respaldo.
    email_sent = send_activation_email(user["email"], user["username"], code)
    return jsonify({"ok": True, "username": user["username"], "activation_code": code, "email_sent": email_sent})


@app.route("/admin/reject/<int:user_id>", methods=["POST"])
@admin_required
def admin_reject(user_id: int):
    reject_user(user_id)
    return jsonify({"ok": True})


@app.route("/admin/disconnect/<int:user_id>", methods=["POST"])
@admin_required
def admin_disconnect(user_id: int):
    disconnect_user(user_id)
    return jsonify({"ok": True})


@app.route("/admin/reconnect/<int:user_id>", methods=["POST"])
@admin_required
def admin_reconnect(user_id: int):
    user = get_user_by_id(user_id)
    if user is None:
        return jsonify({"error": "Usuario no encontrado."}), 404
    code = generate_activation_code(user_id)
    # 2026-10-11, a pedido explicito: reconnect es generico (cualquier
    # cuenta desconectada, de cualquier marca) -- a diferencia de
    # admin_approve (siempre Metabot), aca hay que mirar familiar_access
    # para saber de que marca mandar el correo.
    brand_name = "Bot Familiar" if user.get("familiar_access") == FAMILIAR_ACCESS_APPROVED else "Metabot"
    email_sent = send_activation_email(user["email"], user["username"], code, brand_name=brand_name)
    return jsonify({"ok": True, "username": user["username"], "activation_code": code, "email_sent": email_sent})


def _usernames_for_group(group: str | None) -> set[str] | None:
    """2026-10-04, a pedido explicito ("las cuentas de bot familiar no se
    pueden ver en metabot"): las tablas de Posiciones/Trades del admin
    mostraban TODAS las cuentas sin importar la pestaña activa -- quien
    mirara la pestaña Metabot veia igual los nombres de usuario reales de
    la familia. None = sin filtro (compatibilidad si no se manda group).

    2026-10-06, a pedido explicito ("son dos admin diferentes... los
    usuarios de metabot se conectan con admin de metabot y los de bot
    familiar con el admin de bot familiar"): un admin con admin_scope
    limitado queda CLAVADO en su marca sin importar que pida la URL --
    antes `group` era solo un filtro de conveniencia para la pestaña
    activa, cualquier admin podia pedir la otra marca a mano via la API.
    Ahora es un limite real de permisos, no solo de UI."""
    admin_scope = session.get("admin_scope", "all")
    if admin_scope in ("familiar", "metabot"):
        group = admin_scope
    if group not in ("familiar", "metabot"):
        return None
    users = list_all_users(include_admin=True)
    if group == "familiar":
        return {u["username"] for u in users if u.get("familiar_access") == FAMILIAR_ACCESS_APPROVED}
    return {u["username"] for u in users if u.get("familiar_access") != FAMILIAR_ACCESS_APPROVED}


@app.route("/admin/trades")
@admin_required
def admin_trades():
    """Entradas y salidas RECIENTES de todas las cuentas familiares
    activas (a pedido explicito del admin, 2026-09-11) -- para verificar
    que el motor esta operando de verdad en las cuentas de todos, no solo
    la propia. Se muestra ticker/lado/estrategia/vencimiento, pero NO
    precio, cantidad ni P&L -- eso sigue siendo privado de cada cuenta,
    misma regla que el resto del panel de administrador."""
    import health_check as hc

    limit = int(request.args.get("limit", 40))
    allowed = _usernames_for_group(request.args.get("group"))
    all_trades = []
    for username, db_path in hc._active_user_db_paths():
        if allowed is not None and username not in allowed:
            continue
        conn = _connect(db_path)
        try:
            _ensure_trades_columns(conn)  # DBs viejas de algun usuario pueden no tener entry_strategy todavia
            rows = conn.execute(
                "SELECT timestamp, ticker, asset_type, side, option_details, entry_strategy, entry_signal "
                "FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
        for r in rows:
            od = json.loads(r["option_details"]) if r["option_details"] else None
            all_trades.append({
                "username": username,
                "timestamp": r["timestamp"],
                "ticker": r["ticker"],
                "asset_type": r["asset_type"],
                "side": r["side"],
                "option_type": od.get("option_type") if od else None,
                "expiration": od.get("expiration") if od else None,
                "strategy": r["entry_strategy"],
            })

    all_trades.sort(key=lambda t: t["timestamp"], reverse=True)
    usernames = [u for u, _ in hc._active_user_db_paths() if allowed is None or u in allowed]
    return jsonify({"trades": all_trades[:limit], "usernames": usernames})


@app.route("/admin/positions")
@admin_required
def admin_positions():
    """Posiciones ABIERTAS ahora mismo de todas las cuentas familiares
    activas (a pedido explicito del admin, 2026-09-11 -- ventana plegable
    para verificar de un vistazo que el motor esta funcionando en las 9
    cuentas, no solo la propia).

    P&L TEMPORAL (2026-09-11, a pedido explicito del admin: "es solo por
    ahora despues lo quitamos, necesito ver que esta funcionando") --
    rompe a proposito la regla de privacidad de no mostrar P&L mientras
    se verifica que el motor multi-usuario funciona de punta a punta.
    QUITAR cuando ya no haga falta verificar en vivo."""
    import health_check as hc

    allowed = _usernames_for_group(request.args.get("group"))
    all_positions = []
    for username, db_path in hc._active_user_db_paths():
        if allowed is not None and username not in allowed:
            continue
        status = get_status(db_path=db_path)
        current_prices = {}
        for p in status["positions"]:
            if p["asset_type"] == "option":
                od = p["option_details"]
                price = market_data.get_option_price(p["ticker"], od["expiration"], od["strike"], od["option_type"])
            else:
                price = market_data.get_quote(p["ticker"])
            if price is not None:
                current_prices[p["key"] if p["asset_type"] == "option" else p["ticker"]] = price
        status = get_status(current_prices=current_prices, db_path=db_path)
        for p in status["positions"]:
            od = p.get("option_details") or {}
            all_positions.append({
                "username": username,
                "ticker": p["ticker"],
                "asset_type": p["asset_type"],
                "option_type": od.get("option_type"),
                "strike": od.get("strike"),
                "expiration": od.get("expiration"),
                "opened_at": p.get("opened_at"),
                "unrealized_pnl": p.get("unrealized_pnl"),
                "unrealized_pnl_pct": (
                    (p["market_price"] - p["avg_cost"]) / p["avg_cost"] * 100
                    if p.get("avg_cost") else None
                ),
            })

    all_positions.sort(key=lambda p: (p["username"], p["opened_at"] or ""))
    usernames = [u for u, _ in hc._active_user_db_paths() if allowed is None or u in allowed]
    return jsonify({"positions": all_positions, "usernames": usernames})


@app.route("/admin/health")
@admin_required
def admin_health():
    """Estado de salud del sistema COMPLETO -- todos los procesos de
    fondo y la matematica de CADA cuenta activa (health_check.py ya
    itera usuario por usuario). Nunca muestra balance/P&L, solo si algo
    esta roto y de quien es (username, sin montos)."""
    import health_check as hc
    import watchdog as wd

    problems = []
    for label, check_fn in (
        ("Matematica de cuenta", hc.check_account_math),
        ("Precios de posiciones", hc.check_position_prices),
        ("Posiciones duplicadas", hc.check_duplicate_positions),
        ("Procesos de fondo", hc.check_background_processes),
    ):
        for p in check_fn():
            problems.append({"label": label, "message": p})

    # Estado de cada proceso, estructurado (para la grilla visual del
    # panel -- no solo texto de "problema encontrado"). Flask se chequea
    # por el puerto (ya esta corriendo en el mismo proceso que responde
    # esto, pero se confirma igual por consistencia con los demas).
    processes = []
    for name, spec in wd.MANAGED_PROCESSES.items():
        if name == "run_dashboard.py":
            running = not wd._flask_down()
        else:
            running = hc._script_is_running(spec["match"])
        processes.append({"name": name, "running": bool(running)})
    # watchdog.py NO esta en MANAGED_PROCESSES (no se puede reiniciar a si
    # mismo si se cae) pero igual es importante que el admin vea si el
    # censor en si esta vivo -- se chequea aparte, misma logica de ruta
    # absoluta que health_check.py.
    processes.append({
        "name": "watchdog.py",
        "running": bool(hc._script_is_running(str(hc.ROOT / "watchdog.py"))),
    })
    # 2026-10-05, a pedido explicito ("motor de vigilancia... que corrija
    # errores en segundos" + "agregarlo a la pestaña salud del sistema"):
    # mismo chequeo aparte que watchdog.py, tampoco puede reiniciarse a si
    # mismo.
    processes.append({
        "name": "fast_watchdog.py",
        "running": bool(hc._script_is_running(str(hc.ROOT / "fast_watchdog.py"))),
    })

    # 2026-10-05, a pedido explicito ("en metabot se siguen viendo los
    # usuarios de bot familiar"): estos conteos venian de list_all_users()
    # SIN filtrar por marca -- el admin veia "Cuentas activas" con el total
    # combinado (las 7 cuentas familiares incluidas) aunque estuviera
    # parado en la pestaña Metabot. Mismo _usernames_for_group que ya usan
    # Posiciones/Trades/Soporte.
    allowed = _usernames_for_group(request.args.get("group"))
    all_users = list_all_users()
    if allowed is not None:
        all_users = [u for u in all_users if u["username"] in allowed]
    active_users = len([u for u in all_users if u["status"] == STATUS_ACTIVE])
    disconnected_users = len([u for u in all_users if u["status"] == STATUS_REJECTED])
    # list_pending_users() son siempre altas publicas de Metabot (Bot
    # Familiar no tiene auto-registro) -- en la pestaña Bot Familiar el
    # conteo correcto es 0, no el total.
    pending_count = 0 if request.args.get("group") == "familiar" else len(list_pending_users())

    recent_errors = get_error_log(limit=30)
    return jsonify({
        "problems": problems,
        "recent_errors": recent_errors,
        "processes": processes,
        "stats": {
            "active_users": active_users,
            "disconnected_users": disconnected_users,
            "pending_count": pending_count,
            "errors_today": sum(
                1 for e in recent_errors
                if (e.get("timestamp") or "").startswith(datetime.now(timezone.utc).date().isoformat())
            ),
        },
    })


@app.route("/admin/create-admin", methods=["POST"])
@admin_required
def admin_create_admin():
    """2026-10-06, a pedido explicito ("son dos admin diferentes... los
    usuarios de metabot se conectan con admin de metabot y los de bot
    familiar con el admin de bot familiar"): crea una cuenta admin NUEVA,
    limitada a una sola marca (ve y responde SOLO el soporte/cuentas de
    esa marca, ver _usernames_for_group). Restringido al admin
    admin_scope='all' -- un admin ya limitado a una marca no puede crear
    otros admins (ni para su propia marca ni para la otra), para que esto
    no se use para escalar permisos."""
    if session.get("admin_scope", "all") != "all":
        return jsonify({"error": "No tenes permiso para crear otras cuentas de administrador."}), 403
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    scope = data.get("admin_scope") or ""
    if scope not in ("metabot", "familiar"):
        return jsonify({"error": "Elegi a que marca pertenece este administrador."}), 400
    if not username or not email or not password:
        return jsonify({"error": "Usuario, correo y contraseña son obligatorios."}), 400
    if len(password) < 8:
        return jsonify({"error": "La contraseña tiene que tener al menos 8 caracteres."}), 400
    existing = get_user(username)
    if existing:
        return jsonify({"error": "Ya existe una cuenta con ese usuario."}), 400
    try:
        create_admin_user(username, email, password, admin_scope=scope)
    except Exception as e:
        return jsonify({"error": f"No se pudo crear: {e}"}), 500
    return jsonify({"ok": True})


@app.route("/admin/plan/<username>", methods=["POST"])
@admin_required
def admin_set_plan(username: str):
    """2026-10-06: activa o quita Metabot Pro a mano, mientras Stripe no esta
    conectado. Solo aplica a cuentas de Metabot (las de Bot Familiar son
    gratis y no tienen plan)."""
    data = request.get_json(silent=True) or {}
    plan = data.get("plan")
    if plan not in ("free", "pro"):
        return jsonify({"error": "El plan tiene que ser 'free' o 'pro'."}), 400
    target = get_user(username)
    if not target:
        return jsonify({"error": "No existe esa cuenta."}), 404
    if target.get("familiar_access") == FAMILIAR_ACCESS_APPROVED:
        return jsonify({"error": "Las cuentas de Bot Familiar no tienen plan."}), 400
    allowed = _usernames_for_group(None)
    if allowed is not None and username not in allowed:
        return jsonify({"error": "Esa cuenta no es de tu marca."}), 403
    set_plan_manual(username, plan)
    return jsonify({"ok": True, "plan": plan})


@app.route("/admin/heal", methods=["POST"])
@admin_required
def admin_heal():
    """Corre el mismo chequeo-y-reparacion que watchdog.py hace solo cada
    10 min, PERO AHORA, a pedido del admin -- reinicia lo que este caido
    o colgado, autocorrige matematica de cuenta / posiciones duplicadas
    si hacia falta, y devuelve que accion tomo (ver watchdog.py)."""
    import watchdog as wd
    try:
        actions = wd.check_and_heal_once()
    except Exception as e:
        return jsonify({"error": f"Fallo la reparacion: {e}"}), 500
    return jsonify({"ok": True, "actions": actions or ["Sin problemas encontrados -- todo en orden."]})


# 2026-10-04: metabot es ahora el espacio PUBLICO/visible por defecto
# (pensado para escalar a 500+ suscriptores); Bot Familiar pasa a ser el
# espacio paralelo oculto (boton "BF" en el menu, ver /familiar abajo).
# Mismo dashboard.html en los dos casos -- lo unico que cambia es el
# nombre/marca en el header, via estas dos variables.
BRAND_METABOT = {
    "brand_label": "Meta<strong>bot</strong>", "page_title": "Metabot", "brand_space": "metabot",
    "brand_icon": "img/metabot-icon-192.png",
}
BRAND_FAMILIAR = {
    "brand_label": "Bot <strong>Familiar</strong>", "page_title": "Bot Familiar", "brand_space": "familiar",
    "brand_icon": "img/icon-192.png",
}


def _brand_for_user(user: dict | None) -> dict:
    """2026-10-04, a pedido explicito del usuario ("los que ya estan
    dentro de bot familiar no ven el dashboard de metabot"): quien YA
    tiene familiar_access='approved' entra DIRECTO al espacio Bot
    Familiar por defecto (en '/' y '/mi-panel'), nunca ve la marca
    metabot -- metabot es solo para el resto (publico nuevo, sin
    aprobar todavia). El boton "BF" del menu ya se auto-oculta para
    ellos porque brand_space ya queda en 'familiar'."""
    access = user.get("familiar_access", FAMILIAR_ACCESS_NONE) if user else FAMILIAR_ACCESS_NONE
    return BRAND_FAMILIAR if access == FAMILIAR_ACCESS_APPROVED else BRAND_METABOT


@app.route("/")
@login_required
def dashboard():
    # El admin NUNCA aterriza en el dashboard normal (Resumen/Posiciones
    # con valores en dolares de SU PROPIA cuenta) -- a pedido explicito
    # del usuario 2026-09-11 ("elimina lo que dice resumen que esta
    # poniendo un panel como si estuviera operando desde esa pestaña"):
    # el rol de admin es administracion, no mostrar como si estuviera
    # operando el mismo. Los demas usuarios (no admin) siguen viendo su
    # dashboard normal sin ningun cambio.
    if session.get("is_admin"):
        return redirect(url_for("admin_panel"))
    user = get_user(session["user"])
    brand = _brand_for_user(user)
    return render_template(
        "dashboard.html", username=session.get("user"), is_admin=session.get("is_admin", False),
        tradier_active=_tradier_active_for(session.get("user")),
        news_enabled=NEWS_ENABLED_FOR_ALL,
        familiar_access=user.get("familiar_access", FAMILIAR_ACCESS_NONE) if user else FAMILIAR_ACCESS_NONE,
        can_operate=can_open_positions(user),
        pro_activated=request.args.get("pro") == "ok",
        trial_deadline=_trial_deadline_for(user, brand.get("brand_space")),
        signed_terms=terms_signing.has_signed(user["id"]) is not None if user else False,
        **brand,
    )


@app.route("/mi-panel")
@login_required
def my_dashboard():
    # El admin tambien tiene su propia cuenta de paper trading (con la
    # que el bot prueba las estrategias) -- "/" lo manda siempre a
    # /admin (arriba), pero necesita una forma explicita de ver SU
    # propio dashboard normal cuando quiere, sin que sea lo primero que
    # ve al entrar (2026-09-13, a pedido explicito: "agrega la seccion
    # de usuario que ya yo tenia... con la que esta probando las
    # estrategias").
    # 2026-10-07, a pedido explicito ("lo puedes poner como estaba de
    # metabot y no lo cambies mas"): este panel es SIEMPRE Metabot para
    # el admin, sin importar su familiar_access (que es 'approved' por
    # definicion, ver _brand_for_user) -- fijo, no vuelve a cambiar solo.
    user = get_user(session["user"])
    brand = BRAND_METABOT if session.get("is_admin") else _brand_for_user(user)
    return render_template(
        "dashboard.html", username=session.get("user"), is_admin=session.get("is_admin", False),
        tradier_active=_tradier_active_for(session.get("user")),
        news_enabled=NEWS_ENABLED_FOR_ALL,
        familiar_access=user.get("familiar_access", FAMILIAR_ACCESS_NONE) if user else FAMILIAR_ACCESS_NONE,
        can_operate=can_open_positions(user),
        trial_deadline=_trial_deadline_for(user, brand.get("brand_space")),
        signed_terms=terms_signing.has_signed(user["id"]) is not None if user else False,
        **brand,
    )


@app.route("/familiar")
@login_required
def familiar_space():
    """Espacio paralelo Bot Familiar -- oculto, no aparece en ningun
    registro publico. Se llega aca solo por el boton "BF" del menu.
    Pedido explicito del usuario (2026-10-04): metabot (publico, 500+
    suscriptores) es ahora el espacio visible por defecto; Bot Familiar
    queda reservado para familia/amigos cercanos, cada uno tiene que
    PEDIR entrada aca dentro y el admin la aprueba/rechaza a mano desde
    el panel -- es una segunda capa de acceso, aparte de poder loguearse
    en metabot."""
    user = get_user(session["user"])
    access = user.get("familiar_access", FAMILIAR_ACCESS_NONE) if user else FAMILIAR_ACCESS_NONE
    if access != FAMILIAR_ACCESS_APPROVED:
        return render_template(
            "familiar_gate.html", username=session.get("user"), is_admin=session.get("is_admin", False),
            access=access,
        )
    return render_template(
        "dashboard.html", username=session.get("user"), is_admin=session.get("is_admin", False),
        tradier_active=_tradier_active_for(session.get("user")),
        news_enabled=NEWS_ENABLED_FOR_ALL,
        familiar_access=access,
        can_operate=can_open_positions(user),
        signed_terms=terms_signing.has_signed(user["id"]) is not None,
        **BRAND_FAMILIAR,
    )


@app.route("/familiar/solicitar", methods=["POST"])
@login_required
def familiar_request():
    user = get_user(session["user"])
    if user:
        request_familiar_access(user["id"])
    return redirect(url_for("familiar_space"))


@app.route("/admin/familiar/aprobar/<int:user_id>", methods=["POST"])
@admin_required
def admin_familiar_approve(user_id: int):
    # 2026-10-11, a pedido explicito ("entran a traves de un enlace por
    # correo que se les envia una vez que se subscriban"): se mantiene
    # EXACTAMENTE el mismo flujo que ya existia (el usuario pide acceso
    # desde su cuenta, el admin lo aprueba aca) -- lo unico nuevo es que
    # al aprobar le llega un correo con el link directo a /privado (el
    # mismo espacio de siempre, /familiar, solo que ahora el usuario
    # entra ahi derecho a firmar terminos y conectar su broker en vez de
    # tener que volver a pedir acceso el mismo). Igual que
    # send_activation_email, si el correo falla no corta el aprobado --
    # el admin ya lo ve aprobado en el panel de todas formas.
    user = get_user_by_id(user_id)
    approve_familiar_access(user_id)
    if user:
        link = request.url_root.rstrip("/") + url_for("familiar_space")
        send_alert_email(
            user["email"],
            "Tu acceso a Metabot Privado ya esta aprobado",
            f"Hola {user['username']},\n\nTu acceso al plan privado de Metabot ya esta aprobado. "
            f"Entra con tu usuario y contrasena de siempre y segui el link de abajo para firmar los "
            f"terminos y conectar tu cuenta de Tradier:\n\n{link}\n\nMetabot",
            allow_user=True,
            from_name="Bot Familiar",
        )
    return jsonify({"ok": True})


@app.route("/admin/familiar/rechazar/<int:user_id>", methods=["POST"])
@admin_required
def admin_familiar_reject(user_id: int):
    reject_familiar_access(user_id)
    return jsonify({"ok": True})


@app.route("/admin/familiar/invitar", methods=["POST"])
@admin_required
def admin_familiar_invite():
    # 2026-10-11, a pedido explicito ("agregar una seccion plegable para
    # enviar el correo a la persona que quiera agregar con el link
    # directo a subscribirse a bot familiar"): para alguien que TODAVIA
    # no tiene cuenta -- distinto de aprobar una solicitud ya hecha. El
    # link vence en 7 dias y es de un solo uso (ver familiar_invites).
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    if not email or "@" not in email:
        return jsonify({"error": "Falta un correo valido."}), 400
    token = create_familiar_invite(email)
    link = request.url_root.rstrip("/") + url_for("familiar_invite_signup", token=token)
    email_sent = send_alert_email(
        email,
        "Te invitaron al plan privado de Metabot",
        f"Te invitaron a sumarte al plan privado (gratis) de Metabot.\n\n"
        f"Entra a este link para crear tu cuenta, firmar los terminos y conectar tu Tradier "
        f"(vence en 7 dias):\n\n{link}\n\nMetabot",
        allow_user=True,
        from_name="Bot Familiar",
    )
    return jsonify({"ok": True, "link": link, "email_sent": email_sent})


@app.route("/admin/problemas-conexion/<int:issue_id>/resolver", methods=["POST"])
@admin_required
def admin_resolve_connection_issue(issue_id: int):
    resolve_connection_issue(issue_id)
    return jsonify({"ok": True})


@app.route("/privado/registro/<token>", methods=["GET", "POST"])
def familiar_invite_signup(token: str):
    invite = verify_familiar_invite(token)
    if invite is None:
        return render_template(
            "login.html",
            error="Este link de invitación ya no es válido (vencido o ya usado). Consultá con el administrador.",
        )
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        accepted = request.form.get("terms_accepted") == "on"
        if not accepted:
            return render_template(
                "familiar_invite_signup.html", email=invite["email"], token=token,
                error="Tenés que aceptar los Términos y Condiciones y el Aviso de Riesgo para continuar.",
            )
        try:
            user = register_familiar_invitee(username, invite["email"], password)
        except ValueError as e:
            return render_template(
                "familiar_invite_signup.html", email=invite["email"], token=token, error=str(e),
            )
        consume_familiar_invite(invite["id"])
        session.clear()
        session["user"] = user["username"]
        return redirect(url_for("sign_terms", next="privado"))
    return render_template("familiar_invite_signup.html", email=invite["email"], token=token, error=None)


@app.route("/api/news")
@login_required
def api_news_list():
    if not NEWS_ENABLED_FOR_ALL:
        return jsonify({"items": []})
    return jsonify({"items": list_news_for_user(session["user"])})


@app.route("/api/news/mark-read", methods=["POST"])
@login_required
def api_news_mark_read():
    if not NEWS_ENABLED_FOR_ALL:
        return jsonify({"ok": False}), 403
    mark_all_read(session["user"])
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Soporte tecnico -- 2026-09-28, a pedido del usuario ("soporte tecnico donde
# los usuarios puedan escribirme dentro de la aplicacion... es personal para
# cada usuario y solo ellos tienen acceso a las conversaciones personales").
# Un hilo POR CUENTA, privado entre esa cuenta y el admin -- la cuenta admin
# NUNCA aparece en list_all_users() para las demas cuentas, y aca cada
# usuario solo puede leer/escribir en SU PROPIO hilo (session["user"]),
# nunca el de otra cuenta -- no hay forma de pedir el hilo de otro username
# desde las rutas de usuario.
# ---------------------------------------------------------------------------

@app.route("/api/support", methods=["GET", "POST"])
@login_required
def api_support():
    # GET no marca nada leido (mismo patron que /api/news -- leer la
    # lista y "marcar como leido" son acciones separadas, la 2da la pide
    # la UI explicitamente cuando el usuario realmente abre el panel).
    username = session["user"]
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        body = (data.get("body") or "").strip()
        if not body:
            return jsonify({"error": "El mensaje no puede estar vacio."}), 400
        brand = _brand_for_user(get_user(username)).get("brand_space")
        send_support_message(username, "user", body, brand=brand)
    return jsonify({"thread": list_support_thread(username)})


@app.route("/api/support/mark-read", methods=["POST"])
@login_required
def api_support_mark_read():
    mark_thread_read(session["user"], "user")
    return jsonify({"ok": True})


@app.route("/api/support/unread")
@login_required
def api_support_unread():
    return jsonify({"unread": has_unread_support(session["user"])})


def _effective_support_brand(requested: str | None) -> str | None:
    """2026-10-06: la marca que de verdad se usa para filtrar soporte --
    un admin con admin_scope limitado queda clavado en la suya (mismo
    criterio que _usernames_for_group), el admin 'all' usa lo que pida la
    pestaña activa."""
    admin_scope = session.get("admin_scope", "all")
    if admin_scope in ("familiar", "metabot"):
        return admin_scope
    return requested if requested in ("familiar", "metabot") else None


@app.route("/admin/support")
@admin_required
def admin_support_inbox():
    # 2026-10-06, a pedido explicito ("si escribo desde metabot tiene que
    # llegarle al admin de metabot..."): list_support_threads_summary ya
    # agrupa por la marca de CADA MENSAJE (ver support.py), no por el
    # username -- reemplaza el filtro por _usernames_for_group que tenia
    # esta ruta antes (ese filtraba por familiar_access fijo de la
    # cuenta, no servia para la cuenta admin, que puede escribir
    # disfrazada de las dos marcas).
    threads = list_support_threads_summary(brand=_effective_support_brand(request.args.get("group")))
    has_unread = any(t["unread_by_admin"] > 0 for t in threads)
    return jsonify({"threads": threads, "has_unread": has_unread})


@app.route("/admin/support/<username>", methods=["GET", "POST"])
@admin_required
def admin_support_thread(username: str):
    # Solo cuentas familiares reales tienen hilo -- si alguien intenta
    # pedir un username inventado, list_support_thread() simplemente
    # devuelve una lista vacia (no rompe, no expone nada).
    data = request.get_json(silent=True) or {}
    brand = _effective_support_brand(request.args.get("group") or data.get("brand"))
    if request.method == "POST":
        body = (data.get("body") or "").strip()
        if not body:
            return jsonify({"error": "El mensaje no puede estar vacio."}), 400
        send_support_message(username, "admin", body, brand=brand)
    return jsonify({"thread": list_support_thread(username, brand=brand)})


@app.route("/admin/support/<username>/mark-read", methods=["POST"])
@admin_required
def admin_support_mark_read(username: str):
    data = request.get_json(silent=True) or {}
    mark_thread_read(username, "admin", brand=_effective_support_brand(request.args.get("group") or data.get("brand")))
    return jsonify({"ok": True})


# 2026-10-07, a pedido explicito ("quiero que se vea de esta forma pero que
# los usuarios no puedan entrar a mi panel de administrador, cada uno con el
# suyo personal"): cada suscriptor conecta SU PROPIA cuenta de Tradier con un
# token personal (OAuth esta bloqueado hasta que Tradier apruebe la app como
# socio -- no hay fecha). El token se valida contra Tradier ANTES de
# guardarlo, se cifra con webapp/brokers/vault.py y se guarda scoped a su
# user_id via webapp/brokers/store.py -- la misma proteccion de store.py
# (WHERE user_id = ?) es lo que ya impide que un usuario vea la conexion de
# otro; @login_required + @admin_required (rutas separadas) es lo que ya
# impide que un usuario no-admin entre al panel de administrador.
@app.route("/api/broker/connect", methods=["POST"])
@login_required
def api_broker_connect():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    env_ui = (data.get("env") or "sandbox").strip()  # termino de Tradier: "sandbox" o "production"
    if not token:
        return jsonify({"error": "Falta el token."}), 400
    if env_ui not in ("sandbox", "production"):
        return jsonify({"error": "Ambiente invalido."}), 400

    try:
        client = TradierClient(token=token, env=env_ui)
        profile = client.profile()
    except TradierError:
        # 2026-10-11, a pedido explicito ("todos los usuarios que tengan
        # problemas para conectarse... en la seccion de Solicitudes"):
        # token de Tradier invalido queda registrado para que el admin
        # vea a quien ayudar, no se pierde en el error que solo ve el
        # usuario en su pantalla.
        record_connection_issue(session["user"], "broker", f"Token de Tradier invalido (ambiente: {env_ui}).")
        return jsonify({"error": "No se pudo validar el token contra Tradier -- revisa que sea correcto."}), 400

    account = (profile.get("profile") or {}).get("account")
    if isinstance(account, list):
        account = account[0] if account else None
    if not account or account.get("status") != "active":
        record_connection_issue(session["user"], "broker", f"Cuenta de Tradier inactiva o ilegible (ambiente: {env_ui}).")
        return jsonify({"error": "La cuenta de Tradier no esta activa o no se pudo leer."}), 400

    user = get_user(session["user"])
    store_env = "live" if env_ui == "production" else "paper"
    broker_store.create_connection(
        user["id"], "tradier", account["account_number"], store_env, {"token": token, "env": env_ui},
    )
    return jsonify({"ok": True, "account_id": account["account_number"], "env": env_ui})


@app.route("/api/broker/status")
@login_required
def api_broker_status():
    user = get_user(session["user"])
    connections = broker_store.list_connections(user["id"])
    return jsonify({"connections": [c for c in connections if c["status"] == "active"]})


@app.route("/api/broker/disconnect", methods=["POST"])
@login_required
def api_broker_disconnect():
    data = request.get_json(silent=True) or {}
    connection_id = data.get("connection_id")
    if not connection_id:
        return jsonify({"error": "Falta connection_id."}), 400
    user = get_user(session["user"])
    ok = broker_store.revoke_connection(user["id"], int(connection_id))
    return jsonify({"ok": ok})


# 2026-10-07, a pedido explicito ("despues va a elegir operar dinero real
# para que el bot trabaje"): paso aparte de conectar el broker -- conectar
# solo guarda el token cifrado, este interruptor es el que el usuario
# prende cuando de verdad quiere que el bot opere con esa cuenta. El motor
# que de verdad ejecuta ordenes todavia solo corre para la cuenta admin
# (ver tradier_cycle.py / data/tradier_only_users.json) -- este interruptor
# guarda la intencion del usuario, generalizar el motor a todas las
# cuentas marcadas asi es trabajo aparte, todavia pendiente.
@app.route("/api/broker/live-trading", methods=["POST"])
@login_required
def api_broker_live_trading():
    data = request.get_json(silent=True) or {}
    connection_id = data.get("connection_id")
    enabled = bool(data.get("enabled"))
    if not connection_id:
        return jsonify({"error": "Falta connection_id."}), 400
    user = get_user(session["user"])
    ok = broker_store.set_live_trading(user["id"], int(connection_id), enabled)
    if not ok:
        return jsonify({"error": "Conexion no disponible."}), 404
    return jsonify({"ok": True, "enabled": enabled})


def _build_current_prices(positions: list[dict]) -> dict:
    prices = {}
    for p in positions:
        if p["asset_type"] == "equity":
            price = market_data.get_quote(p["ticker"])
            if price is not None:
                prices[p["ticker"]] = price
        elif p["asset_type"] == "crypto":
            price = market_data.get_quote(f"{p['ticker']}-USD")
            if price is not None:
                prices[p["ticker"]] = price
        elif p["asset_type"] == "option":
            od = p["option_details"]
            # 2026-09-16, a pedido del usuario: antes esto mostraba el punto
            # medio (bid/ask promedio), pero las salidas REALES (stop-loss,
            # objetivo, toma rapida) deciden con el BID real -- una posicion
            # podia verse en +13% en el panel y cerrarse en +8.96% de
            # verdad, generando confusion ("por que no vendio si estaba en
            # +13%"). Ahora el panel usa la MISMA funcion que usa
            # run_exits_for_account(), asi el numero que se ve es el mismo
            # que decide.
            price = _current_exit_price(p)
            if price is not None:
                key = _position_key(p["ticker"], "option", od["expiration"], od["strike"], od["option_type"])
                prices[key] = price
    return prices


def _add_day_trading_info(positions: list[dict]) -> None:
    """Enriquece cada posicion (in-place) con info de day trading: hace
    cuanto se abrio, precio objetivo/stop si el plan de salida esta fijado,
    y el estado actual (objetivo alcanzado / stop alcanzado / cerrar por
    fin de sesion / abierta)."""
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(MARKET_TZ)
    past_eod = is_near_session_close(now_et)

    for p in positions:
        opened_at = p.get("opened_at")
        elapsed_minutes = None
        if opened_at:
            try:
                opened_dt = datetime.fromisoformat(opened_at)
                elapsed_minutes = round((now_utc - opened_dt).total_seconds() / 60)
            except ValueError:
                pass
        p["elapsed_minutes"] = elapsed_minutes

        target_pct = p.get("profit_target_pct")
        stop_pct = p.get("stop_loss_pct")
        target_price = p["avg_cost"] * (1 + target_pct) if target_pct is not None else None
        stop_price = p["avg_cost"] * (1 - stop_pct) if stop_pct is not None else None
        p["target_price"] = target_price
        p["stop_price"] = stop_price

        if target_price is not None and p["market_price"] >= target_price:
            status = "objetivo"
        elif stop_price is not None and p["market_price"] <= stop_price:
            status = "stop"
        elif past_eod:
            status = "cerrar_sesion"
        else:
            status = "abierta"
        p["day_trade_status"] = status


@app.route("/api/status")
@login_required
def api_status():
    db_path = _user_db_path()
    raw = get_status(db_path=db_path)
    prices = _build_current_prices(raw["positions"])
    status = get_status(current_prices=prices, db_path=db_path) if prices else raw
    _add_day_trading_info(status["positions"])
    status["closed_today"] = get_todays_closed_trades(db_path=db_path)
    return jsonify(status)


@app.route("/api/positions/close", methods=["POST"])
@login_required
def api_close_position():
    """Cierra (vende, SIMULADO) una posicion abierta especifica, identificada
    por su key -- nunca afecta otras posiciones. Precio de cierre: el mismo
    precio en vivo (bid/ask o quote) que ya usa el resto del panel."""
    data = request.get_json(silent=True) or {}
    key = data.get("key")
    if not key:
        return jsonify({"error": "Falta 'key' de la posicion a cerrar."}), 400

    db_path = _user_db_path()
    pos = get_position_by_key(key, db_path=db_path)
    if pos is None:
        return jsonify({"error": "Esa posicion ya no existe (puede que se haya cerrado en otro ciclo)."}), 404

    if pos["asset_type"] == "equity":
        price = market_data.get_quote(pos["ticker"])
    elif pos["asset_type"] == "crypto":
        price = market_data.get_quote(f"{pos['ticker']}-USD")
    elif pos["asset_type"] == "option":
        od = json.loads(pos["option_details"])
        # Ejecucion realista (portado de trading-bot, 2026-09-10): se
        # vende al BID real, no al punto medio. Si no hay bid en vivo
        # para ese strike exacto cae al punto medio.
        bid, _ask = market_data.get_option_bid_ask(pos["ticker"], od["expiration"], od["strike"], od["option_type"])
        price = bid if bid is not None else market_data.get_option_price(
            pos["ticker"], od["expiration"], od["strike"], od["option_type"]
        )
    else:
        price = None

    if price is None:
        return jsonify({"error": "No se pudo obtener un precio actual para cerrar esta posicion. Intenta de nuevo en unos segundos."}), 503

    kwargs = dict(
        side="sell",
        ticker=pos["ticker"],
        asset_type=pos["asset_type"],
        quantity=pos["quantity"],
        price=price,
        multiplier=pos["multiplier"],
        reason="Cierre manual desde el panel web (boton individual de esta posicion).",
        db_path=db_path,
    )
    if pos["asset_type"] == "option":
        od = json.loads(pos["option_details"])
        kwargs.update(expiration=od["expiration"], strike=od["strike"], option_type=od["option_type"])

    try:
        record_trade(**kwargs)
    except PaperTradingError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"ok": True})


@app.route("/api/history")
@login_required
def api_history():
    limit = request.args.get("limit", default=100, type=int)
    return jsonify(get_history(limit=limit, db_path=_user_db_path()))


@app.route("/api/closed-trades")
@login_required
def api_closed_trades():
    """Historial COMPLETO de posiciones cerradas (compra+venta emparejadas,
    con P&L ya calculado) -- nunca se borra nada de la base de datos; esto
    es lo mismo que 'cerradas hoy' del panel principal pero sin el filtro
    de fecha, para la pestaña Historial."""
    limit = request.args.get("limit", default=200, type=int)
    trades = get_closed_trades(db_path=_user_db_path())
    trades.sort(key=lambda t: t["closed_at"], reverse=True)
    return jsonify(trades[:limit])


@app.route("/api/account-value-history")
@login_required
def api_account_value_history():
    return jsonify(get_account_value_history(db_path=_user_db_path()))


@app.route("/api/market-analysis")
@login_required
def api_market_analysis():
    return jsonify({
        "weekly_summary": market_data.get_weekly_market_summary(),
        "top_etfs": market_data.get_top_etfs_for_day_trading(),
        "known_etfs": market_data.get_known_etfs_recap(),
    })


_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-\^=]{1,10}$")


@app.route("/api/technical-outlook")
@login_required
def api_technical_outlook():
    symbol = request.args.get("symbol", "SPY").strip().upper()
    if not _SYMBOL_RE.match(symbol):
        return jsonify({"error": f"Simbolo invalido: '{symbol}'"}), 400
    outlook = market_data.get_technical_outlook(symbol)
    if not outlook["timeframes"]:
        return jsonify({"error": f"No se encontraron datos de mercado para '{symbol}'"}), 404
    return jsonify(outlook)


@app.route("/api/market-status")
@login_required
def api_market_status():
    return jsonify(get_market_status())


@app.route("/api/error-log")
@login_required
def api_error_log():
    limit = request.args.get("limit", default=50, type=int)
    return jsonify(get_error_log(limit=limit, db_path=_user_db_path()))


@app.route("/api/watchlist")
@login_required
def api_watchlist():
    settings = get_settings(db_path=_user_db_path())
    return jsonify(market_data.get_watchlist_overview(settings["watchlist"]))


@app.route("/api/fast-watchlist")
@login_required
def api_fast_watchlist():
    settings = get_settings(db_path=_user_db_path())
    return jsonify(market_data.get_watchlist_overview(settings["fast_watchlist"]))


@app.route("/api/affordable-symbols")
@login_required
def api_affordable_symbols():
    # 2026-09-14, a pedido del usuario: mostrarle a cada cuenta CUALES
    # tickers de su watchlist son accesibles con su poder de compra
    # actual -- reusa el mismo pre-filtro que ya usa el motor de
    # entradas real (paper_trading.family_sizing.estimate_affordable_symbols)
    # para que lo que ve en pantalla sea EXACTAMENTE lo que el motor usa
    # para decidir que analizar, no una aproximacion aparte.
    db_path = _user_db_path()
    settings = get_settings(db_path=db_path)
    status = get_status(db_path=db_path)
    all_symbols = list(dict.fromkeys(settings["watchlist"] + settings["fast_watchlist"]))
    affordable = estimate_affordable_symbols(
        all_symbols, status["total_account_value"], float(settings["risk_pct_per_trade"]),
        status["cash_balance_available"],
        sizing_mode=settings.get("sizing_mode", "auto"), fixed_contracts=int(settings["contracts_per_trade"]),
    )
    if settings.get("sizing_mode") == "manual":
        budget = status["cash_balance_available"] / max(int(settings["contracts_per_trade"]), 1)
    else:
        budget = min(status["total_account_value"] * settings["risk_pct_per_trade"] / 100, status["cash_balance_available"])
    return jsonify({
        "affordable": affordable,
        "total": len(all_symbols),
        "budget": round(budget, 2),
    })


@app.route("/api/daily-guide")
@login_required
def api_daily_guide():
    # 2026-09-15, a pedido del usuario: guia diaria por ticker basada en
    # backtest real de 60 dias (ver daily_guide.py) -- mismos datos que
    # usa el motor de entradas para exigir mas confianza en tickers
    # 'cuidado', expuestos aca para que el usuario los pueda leer.
    try:
        with open(ROOT / "data" / "daily_guide.json", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return jsonify({"updated_at": None, "tickers": {}})
    return jsonify(data)


@app.route("/api/bot-status", methods=["GET", "POST"])
@login_required
def api_bot_status():
    db_path = _user_db_path()
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        enabled = bool(data.get("enabled"))
        # Encender a mano anula el circuit breaker por hoy (limite x2, ver
        # multi_user_entry._check_circuit_breaker); apagar limpia la anulacion.
        settings = update_settings(bot_enabled=enabled, breaker_override_today=enabled, db_path=db_path)
    else:
        settings = get_settings(db_path=db_path)
    return jsonify({"enabled": settings["bot_enabled"]})


@app.route("/api/journal-summary")
@login_required
def api_journal_summary():
    db_path = _user_db_path()
    return jsonify({
        "daily": daily_breakdown(days=14, db_path=db_path),
        "weekly": weekly_summary(db_path=db_path),
    })


@app.route("/api/settings", methods=["GET", "POST"])
@login_required
def api_settings():
    db_path = _user_db_path()
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        try:
            max_trades = int(data.get("max_trades_per_day"))
        except (TypeError, ValueError):
            return jsonify({"error": "max_trades_per_day debe ser un numero entero."}), 400
        watchlist = data.get("watchlist")
        fast_watchlist = data.get("fast_watchlist")
        watchlist_enabled = data.get("watchlist_enabled")
        fast_watchlist_enabled = data.get("fast_watchlist_enabled")
        default_capital_list_enabled = data.get("default_capital_list_enabled")
        max_daily_loss_pct = data.get("max_daily_loss_pct")
        if max_daily_loss_pct is not None:
            try:
                max_daily_loss_pct = float(max_daily_loss_pct)
            except (TypeError, ValueError):
                return jsonify({"error": "max_daily_loss_pct debe ser un numero."}), 400
        risk_pct_per_trade = data.get("risk_pct_per_trade")
        if risk_pct_per_trade is not None:
            try:
                risk_pct_per_trade = float(risk_pct_per_trade)
            except (TypeError, ValueError):
                return jsonify({"error": "risk_pct_per_trade debe ser un numero."}), 400
        sizing_mode = data.get("sizing_mode")
        contracts_per_trade = data.get("contracts_per_trade")
        if contracts_per_trade is not None:
            try:
                contracts_per_trade = int(contracts_per_trade)
            except (TypeError, ValueError):
                return jsonify({"error": "contracts_per_trade debe ser un numero entero."}), 400
        try:
            settings = update_settings(
                max_trades_per_day=max_trades,
                watchlist=watchlist, fast_watchlist=fast_watchlist,
                watchlist_enabled=watchlist_enabled, fast_watchlist_enabled=fast_watchlist_enabled,
                default_capital_list_enabled=default_capital_list_enabled,
                max_daily_loss_pct=max_daily_loss_pct,
                risk_pct_per_trade=risk_pct_per_trade,
                sizing_mode=sizing_mode, contracts_per_trade=contracts_per_trade,
                db_path=db_path,
            )
        except PaperTradingError as e:
            return jsonify({"error": str(e)}), 400
    else:
        settings = get_settings(db_path=db_path)

    settings = dict(settings)
    settings["trades_today"] = get_trades_count_today(db_path=db_path)
    return jsonify(settings)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False)
