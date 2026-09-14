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

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

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
from paper_trading.trade_journal import daily_breakdown, get_closed_trades, get_todays_closed_trades, weekly_summary  # noqa: E402
from webapp import market_data  # noqa: E402
from webapp.email_sender import send_activation_email  # noqa: E402
from webapp.auth import (  # noqa: E402
    STATUS_ACTIVE,
    STATUS_CODE_SENT,
    STATUS_LEFT,
    STATUS_PENDING,
    STATUS_REJECTED,
    disconnect_user,
    generate_activation_code,
    get_user,
    get_user_by_id,
    has_any_user,
    leave_app,
    list_all_users,
    list_pending_users,
    needs_terms_acceptance,
    record_terms_acceptance,
    register_user,
    reject_user,
    set_robinhood_connected,
    verify_activation_code,
    verify_password,
)

MARKET_TZ = ZoneInfo("America/New_York")
PREMARKET_OPEN_TIME = dtime(4, 0)
MARKET_OPEN_TIME = dtime(9, 30)
MARKET_CLOSE_TIME = dtime(16, 0)

# Cada usuario tiene su PROPIA base de datos de trading, totalmente
# separada de la de los demas (2026-09-10 -- reusa el patron ya probado
# hoy en trading-bot/family_sim.py con 10 cuentas simuladas, en vez de
# agregar user_id a las tablas compartidas de paper_trading/engine.py).
USERS_DATA_DIR = ROOT / "data" / "users"
DEFAULT_INITIAL_BALANCE = 10000.0  # placeholder hasta que cada quien pueda fijar su propio capital


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
        user = get_user(username) if username else None
        if not username or not password or user is None or not verify_password(username, password):
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
            session.clear()
            session["user"] = username
            session["is_admin"] = bool(user["is_admin"])
            return redirect(url_for("dashboard"))
    return render_template("login.html", error=error)


@app.route("/terminos")
def terms():
    return render_template("terms.html")


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
            return redirect(url_for("dashboard"))
    return render_template("accept_terms.html", error=error)


@app.route("/registro", methods=["GET", "POST"])
def signup():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        terms_accepted = request.form.get("terms_accepted") == "on"
        try:
            initial_capital = float(request.form.get("initial_capital", ""))
        except (TypeError, ValueError):
            initial_capital = None
        if not username or not email or not password:
            error = "Usuario, correo y contrasena son obligatorios."
        elif not terms_accepted:
            error = "Tenes que aceptar los Terminos y Condiciones y el Aviso de Riesgo para crear la cuenta."
        elif initial_capital is None or initial_capital < 100:
            error = "El capital inicial debe ser un numero de al menos $100."
        else:
            try:
                register_user(username, email, password, terms_accepted=True)
                # Cuenta de trading creada YA con el capital real elegido
                # (2026-09-10, a pedido del usuario -- antes todos
                # arrancaban con el mismo $10,000 placeholder, se creaba
                # recien en el primer acceso). No hace falta esperar a
                # que se active -- la cuenta de trading es independiente
                # del estado de aprobacion, solo el LOGIN esta bloqueado
                # hasta 'active'.
                db_path = USERS_DATA_DIR / username / "paper_trading.db"
                init_db(initial_balance=initial_capital, db_path=db_path)
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
    return render_template(
        "admin.html",
        pending=list_pending_users(),
        users=list_all_users(),
        username=session.get("user"),
    )


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
    email_sent = send_activation_email(user["email"], user["username"], code)
    return jsonify({"ok": True, "username": user["username"], "activation_code": code, "email_sent": email_sent})


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
    all_trades = []
    for username, db_path in hc._active_user_db_paths():
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
    usernames = [u for u, _ in hc._active_user_db_paths()]
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

    all_positions = []
    for username, db_path in hc._active_user_db_paths():
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
    usernames = [u for u, _ in hc._active_user_db_paths()]
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

    active_users = len([u for u in list_all_users() if u["status"] == STATUS_ACTIVE])
    disconnected_users = len([u for u in list_all_users() if u["status"] == STATUS_REJECTED])
    pending_count = len(list_pending_users())

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


@app.route("/admin/heal", methods=["POST"])
@admin_required
def admin_heal():
    """Corre el mismo chequeo-y-reparacion que watchdog.py hace solo cada
    10 min, PERO AHORA, a pedido del admin -- reinicia lo que este caido
    o colgado y devuelve que accion tomo. No corrige datos de cuenta a
    ciegas (eso sigue siendo solo-reporte, ver watchdog.py)."""
    import watchdog as wd
    try:
        actions = wd.check_and_heal_once()
    except Exception as e:
        return jsonify({"error": f"Fallo la reparacion: {e}"}), 500
    return jsonify({"ok": True, "actions": actions or ["Sin problemas encontrados -- todo en orden."]})


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
    return render_template(
        "dashboard.html", username=session.get("user"), is_admin=session.get("is_admin", False),
        robinhood_connected=bool(user and user.get("robinhood_connected_at")),
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
    user = get_user(session["user"])
    return render_template(
        "dashboard.html", username=session.get("user"), is_admin=session.get("is_admin", False),
        robinhood_connected=bool(user and user.get("robinhood_connected_at")),
    )


@app.route("/api/robinhood-connected", methods=["POST"])
@login_required
def api_robinhood_connected():
    # Autodeclarado -- ver nota en webapp/auth.py::set_robinhood_connected.
    # No otorga NINGUN acceso real ni activa ejecucion de ordenes; solo
    # queda registrado para que el admin sepa quien ya siguio la guia.
    data = request.get_json(silent=True) or {}
    connected = bool(data.get("connected"))
    user = get_user(session["user"])
    set_robinhood_connected(user["id"], connected)
    return jsonify({"ok": True, "connected": connected})


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
            price = market_data.get_option_price(p["ticker"], od["expiration"], od["strike"], od["option_type"])
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
        status["cash_balance"],
        sizing_mode=settings.get("sizing_mode", "auto"), fixed_contracts=int(settings["contracts_per_trade"]),
    )
    if settings.get("sizing_mode") == "manual":
        budget = status["cash_balance"] / max(int(settings["contracts_per_trade"]), 1)
    else:
        budget = min(status["total_account_value"] * settings["risk_pct_per_trade"] / 100, status["cash_balance"])
    return jsonify({
        "affordable": affordable,
        "total": len(all_symbols),
        "budget": round(budget, 2),
    })


@app.route("/api/bot-status", methods=["GET", "POST"])
@login_required
def api_bot_status():
    db_path = _user_db_path()
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        enabled = bool(data.get("enabled"))
        settings = update_settings(bot_enabled=enabled, db_path=db_path)
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
