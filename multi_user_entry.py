#!/usr/bin/env python3
"""
Motor de entradas Y salidas AUTONOMO multi-usuario -- version real de
`trading-bot/family_sim.py` (que uso 10 cuentas de PRUEBA hardcodeadas)
adaptada a los usuarios REALES del sistema (tabla `users` de
`webapp/auth.py`, solo los que estan `active`).

Cada usuario:
- Tiene su propia base de datos SQLite independiente
  (data/users/<username>/paper_trading.db) -- la MISMA ruta que usa
  webapp/app.py::_user_db_path(), para que el panel web y este motor
  vean siempre la misma cuenta.
- Comparte la MISMA señal (el watchlist se escanea UNA sola vez por
  ciclo, no una vez por usuario) pero cada uno elige su propio contrato
  segun lo que puede pagar (paper_trading/family_sizing.py).
- Tiene sus propias reglas independientes: bot_enabled,
  max_trades_per_day, max_daily_loss_pct (circuit breaker por cuenta).
- Ejecuta a una limite marcable cerca del ask (compra) / cerca del bid
  (venta), no al ASK/BID completo -- portado de trading-bot 2026-09-10,
  ajustado 2026-09-29 (ver LIMIT_FILL_FRACTION) para no pagar el 100%
  del spread bid/ask en cada operacion.
- Respeta el cooldown de reingreso de 15 min y la ventana de apertura
  para giro_sma20 (premarket-10:30 ET), mismas reglas que trading-bot.

Uso:
  python multi_user_entry.py --once            # un ciclo (pruebas)
  python multi_user_entry.py --interval 120     # loop continuo -- OJO,
    ver multi_user_entry_loop.py para el wrapper recomendado en
    produccion (proceso fresco por ciclo, evita el problema de threads
    colgados que se encontro hoy en trading-bot con el loop largo).
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from paper_trading.bollinger_strategy import (
    NO_NEW_ENTRY_MINUTES_BEFORE_CLOSE,
    RECENT_MOMENTUM_MINUTES,
    analyze,
    current_vix,
    evaluate_open_position_exit,
    get_trend,
    has_earnings_today,
    is_near_session_close,
    recent_momentum_pct,
    scan_all_signals,
    too_close_to_open_new_position,
)
from paper_trading.engine import (
    DEFAULT_FAST_WATCHLIST,
    DEFAULT_WATCHLIST,
    _connect,
    get_daily_realized_pnl,
    get_settings,
    get_status,
    init_db,
    log_error,
    record_trade,
    update_settings,
)
from daily_guide import load_guide as load_daily_guide
from daily_market_bias import load_bias as load_daily_market_bias
from news_watch import has_recent_news
from paper_trading.family_sizing import estimate_affordable_symbols, select_affordable_contract
from paper_trading.nyse_calendar import is_trading_day, market_close_time
from paper_trading.option_selection import MAX_SPREAD_PCT
from paper_trading.singleton_lock import acquire_single_instance_lock, exits_critical_section
from webapp import market_data
from webapp.auth import STATUS_ACTIVE, can_open_positions, list_all_users

# 2026-09-22, a pedido del usuario ("porque smci se fue contrario... arregla
# el error"): SMCI y NVDA se cerraron via 'rechazo_de_ruptura' a los 4
# segundos de abrirse (SMCI, -13.89% real con el subyacente PLANO; NVDA,
# +-0.00%, cerrada antes de tener oportunidad). Causa raiz: rejection_signal
# compara el precio contra la banda de Bollinger que la posicion "rompio" al
# entrar -- pero esa premisa solo es cierta para estrategias de RUPTURA real
# (squeeze_breakout / squeeze_breakout_temprano, que exigen precio fuera de
# la banda para generar la señal). giro_sma20 (y el resto: gap_fade_apertura,
# opening_range_breakout, volatility_breakout, rsi_reversal, vwap_cross) NO
# exigen eso -- el precio de entrada casi siempre esta DENTRO de la banda, asi
# que la "condicion de rechazo" es practicamente cierta desde el instante 1,
# disparando un cierre casi inmediato sin que haya pasado nada de verdad.
_SQUEEZE_BREAKOUT_STRATEGIES = {"squeeze_breakout", "squeeze_breakout_temprano"}

# 2026-09-29, a pedido del usuario ("arregla el spread"): hasta ahora se
# compraba siempre al ASK completo y se vendia siempre al BID completo --
# paga el 100% del spread bid/ask en cada operacion, ida y vuelta. Real:
# 37 entradas con spread registrado (entry_spread_pct) promediaron 7.30%
# de spread. Un trader real manda una orden limite marcable cerca del
# punto medio en vez de una orden a mercado. LIMIT_FILL_FRACTION = que
# fraccion del camino entre bid y ask se paga/recibe (0.65 = compra al
# 65% del camino desde el bid hacia el ask, vende al 35% del camino desde
# el bid -- mas cerca del extremo "malo" que el punto medio exacto, para
# seguir siendo realista sobre que se llena rapido incluso en mercado
# volatil, pero recuperando ~35% del spread que antes se regalaba). Ver
# uso en fill_price (compra, mas abajo) y _current_exit_price (venta).
LIMIT_FILL_FRACTION = 0.65


def _compute_rejection_signal(entry_strategy: str | None, option_type: str, r: dict) -> bool:
    """True si el precio volvio a cruzar hacia ADENTRO de la banda que la
    posicion rompio al entrar -- SOLO evaluado para estrategias que de
    verdad entran rompiendo la banda (ver _SQUEEZE_BREAKOUT_STRATEGIES y la
    nota de 2026-09-22 mas arriba). Para cualquier otra estrategia (o si no
    se sabe cual la abrio, ej. una posicion vieja de antes de este cambio)
    devuelve False -- no hay nada que "rechazar" si nunca se rompio la banda
    para empezar."""
    if entry_strategy not in _SQUEEZE_BREAKOUT_STRATEGIES:
        return False
    if option_type == "put":
        return r["last_close"] > r["lower_band"]
    return r["last_close"] < r["upper_band"]


DATA_DIR = ROOT / "data"
USERS_DATA_DIR = DATA_DIR / "users"
LOG_PATH = DATA_DIR / "multi_user_entry.log"
PEAK_PNL_STATE_PATH = DATA_DIR / "peak_pnl_tracker.json"

DEFAULT_INTERVAL_SECONDS = 120
REENTRY_COOLDOWN_MINUTES = 15

# 2026-10-01, a pedido del usuario ("busca en la web los tips... agrega la
# mejora"): "calor de portafolio" -- exposicion TOTAL simultanea de una
# cuenta, no solo el riesgo de cada operacion por separado. Practica
# profesional real (6-10% es el rango citado por multiples fuentes de
# gestion de riesgo para day trading). Ver uso junto a select_affordable_contract.
PORTFOLIO_HEAT_CAP_PCT = 10.0
DEFAULT_INITIAL_BALANCE = 100000.0  # mismo valor que webapp/app.py -- capital con el que arranca por defecto una cuenta sandbox de Tradier

CONFIDENCE_ORDER = {"alta": 0, "media": 1, "baja": 2}
ALLOW_LOW_CONFIDENCE = False  # 2026-09-15, a pedido del usuario tras la perdida rapida de TSLA
# (confianza baja = señal que contradice la tendencia mayor; se demostro
# en vivo que falla rapido). Antes era True. Las señales de confianza
# baja siguen detectandose y reportandose, solo ya no se COMPRAN solas.

MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN_BAR_TIME = dtime(9, 30)
GIRO_SMA20_WINDOW_END = dtime(10, 30)


def _giro_sma20_allowed_now() -> bool:
    now_et = datetime.now(MARKET_TZ).time()
    return now_et < MARKET_OPEN_BAR_TIME or now_et <= GIRO_SMA20_WINDOW_END


# 2026-09-18, a pedido del usuario tras el estudio de Bandas de Bollinger
# (backtest_confirmacion.py, 58 sesiones / 45 tickers, mismas funciones de
# señal reales): squeeze_breakout con confianza MEDIA cuya vela de señal cierra
# a las 10:15 ET o despues rindio -0.38% a 30 min (65 señales, 45% de aciertos)
# y fue negativo en las DOS mitades del periodo (-0.60 / -0.21); de 9:45 a
# 10:14 la misma combinacion rindio +0.29%. Con confianza alta despues de las
# 10:15 el resultado es ~0 (-0.04%), asi que se exige alta en vez de bloquear.
SQUEEZE_LATE_ALTA_FROM = dtime(10, 15)

# 2026-10-02, a pedido del usuario tras revisar perdidas reales del dia
# (MARA -22.88%, WMT -23.98%, ambas confianza media): auditoria de TODAS
# las operaciones REALES ya ejecutadas (6 cuentas, no backtest teorico) por
# confianza -- alta: n=52, 35/17 (67%), solo 2 perdidas grandes (<-$15),
# neto +$819.25. media: n=47, 24/23 (51%, moneda al aire), 12 de 47 fueron
# perdidas grandes (1 de cada 4), neto -$501.75 -- NEGATIVO en agregado con
# muestra real. Esto anula la ventana horaria de arriba (que solo exigia
# alta despues de las 10:15): ahora se exige alta TODO el dia para
# squeeze_breakout, igual que para giro_sma20 (ver GIRO_REQUIRES_ALTA mas
# abajo, tambien revertido el mismo dia por la misma evidencia). Para
# REVERTIR: que _squeeze_needs_alta() vuelva a comparar contra
# SQUEEZE_LATE_ALTA_FROM en vez de retornar True siempre.
def _squeeze_needs_alta(now_et: dtime | None = None) -> bool:
    return True


# 2026-09-18, a pedido del usuario tras el estudio fuera de muestra (135 tickers,
# 58 sesiones; 90 tickers nunca usados para afinar): la regla "giro_sma20 exige
# confianza alta" (2026-09-15, decidida con solo 2 operaciones) descartaba las
# MEJORES señales -- giro_sma20 con confianza MEDIA rindio +0.177% a 30 min
# (865 señales, positivo en ambas mitades y en ambos universos) contra +0.062%
# con alta (561 señales). Simulador de cuenta: permitir media sube el retorno
# diario +41% (+57% en los 90 tickers nuevos). ETAPA 1 de un despliegue por
# etapas.
#
# REVERTIDO 2026-10-02: ese criterio era un BACKTEST TEORICO (señales
# simuladas). Auditoria de las operaciones REALES ya ejecutadas desde
# entonces (6 cuentas, ambas estrategias combinadas) por confianza: alta
# n=52, 67% acierto, solo 2 perdidas grandes (<-$15), neto +$819.25; media
# n=47, 51% acierto (moneda al aire), 12 de 47 perdidas grandes (1 de cada
# 4), neto -$501.75 -- NEGATIVO con muestra real de tamaño comparable al
# backtest que justifico permitirla. La realidad ejecutada pesa mas que la
# simulacion que la origino. Disparado por 2 perdidas reales del mismo dia
# (MARA -22.88%, WMT -23.98%, ambas media). Para volver a permitir media:
# GIRO_REQUIRES_ALTA = False y reiniciar bot-familiar-entries.
GIRO_REQUIRES_ALTA = True

# 2026-09-29, a pedido del usuario -- ver la nota junto a giro_baja_exempt
# mas abajo (validado con datos reales, 1h30 adelante, n=54, estable en
# las dos mitades cronologicas: 59%/+0.045% y 59%/+0.012%). Para
# REVERTIR: poner en False y reiniciar bot-familiar-entries.
GIRO_ALLOWS_BAJA = False  # REVERTIDO 2026-09-29: el backtest grande (43
# tickers, 60 dias, n=1424 en baja) contradijo la prueba chica anterior
# (n=54, 18 tickers, 7 dias) que mostraba baja positivo -- con muestra
# real grande, baja rinde -0.155% promedio, 46% acierto, negativo en
# ambas mitades (-0.224% / -0.086%). La prueba chica fue una casualidad
# de muestra insuficiente, el mismo error que ya se descarto con
# Fibonacci hoy. Se mantiene la excepcion en el codigo por si se quiere
# revisar con mas datos mas adelante, pero DESACTIVADA por defecto.


def _giro_blocked_by_confidence(r: dict) -> bool:
    return GIRO_REQUIRES_ALTA and r["strategy"] == "giro_sma20" and r["confidence"] != "alta"


# 2026-09-17, a pedido explicito del usuario ("el horario de entradas y
# salidas es de 9y30 a 4pm, el resto no se opera solo se guardan datos"):
# bug real encontrado el mismo dia -- COST se vendio por stop-loss a las
# 4:08pm ET usando una cotizacion de FUERA de horario (volumen real 0,
# confirmado con datos reales de yfinance: la barra de 3:55-4:00pm tuvo
# 184,493 de volumen real, la de las 4:00pm en adelante tuvo 0). Las
# ENTRADAS ya tenian su propio limite (too_close_to_open_new_position,
# bollinger_strategy.py) -- las SALIDAS nunca lo tuvieron, run_exits_for_account
# corria 24/7 sin chequear si el mercado estaba realmente abierto. Ahora
# las dos usan el mismo horario real (9:30am-4:00pm ET, dias de mercado).
def market_is_open_now(now: datetime | None = None) -> bool:
    now = now or datetime.now(MARKET_TZ)
    day = now.date()
    if not is_trading_day(day):
        return False
    return MARKET_OPEN_BAR_TIME <= now.time() <= market_close_time(day)


def db_path_for(username: str) -> Path:
    return USERS_DATA_DIR / username / "paper_trading.db"


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# 2026-10-07, a pedido del usuario ("mi bot simulado desde ahora va ser
# solo para tradier"): cuentas que ya tienen su propio ciclo dedicado con
# datos de Tradier (ver tradier_cycle.py) -- se excluyen de ESTE motor
# compartido (yfinance) para que ninguna cuenta se procese dos veces.
# Archivo plano para la cuenta de prueba original del admin, mas
# (2026-10-11, "para que el usuario pueda trabajar con la de paper money
# hay que agregar la sandbox") cualquier usuario Pro que conecto su
# propio Tradier en modo sandbox desde "Mi suscripcion".
_TRADIER_ONLY_USERS_FILE = Path(__file__).resolve().parent / "data" / "tradier_only_users.json"


def _tradier_only_usernames() -> set[str]:
    try:
        usernames = set(json.loads(_TRADIER_ONLY_USERS_FILE.read_text(encoding="utf-8")))
    except Exception:
        usernames = set()
    try:
        from webapp.brokers.store import tradier_paper_usernames
        usernames |= tradier_paper_usernames()
    except Exception:
        pass
    try:
        # 2026-10-11, a pedido explicito ("los usuarios de bot familiar
        # siguen con yahoo finance cambia la recopilacion de datos a
        # tradier"): todo el plan privado (Bot Familiar) usa Tradier como
        # fuente de datos por defecto, sin tener que conectar su propio
        # token -- mismo criterio que la cuenta de prueba original.
        from webapp.auth import list_approved_familiar_usernames
        usernames |= list_approved_familiar_usernames()
    except Exception:
        pass
    return usernames


def active_accounts() -> list[tuple[str, Path]]:
    """(username, db_path) de cada usuario ACTIVO -- crea su base de
    datos sola con el balance placeholder si todavia no existe (se
    registro y activo pero nunca abrio su panel web)."""
    tradier_only = _tradier_only_usernames()
    # 2026-10-10, a pedido explicito ("si una funciona la otra no"): quien
    # tiene el interruptor de dinero real encendido queda excluido del
    # motor simulado -- lo procesa real_trading_engine.py en su lugar,
    # nunca los dos motores a la vez sobre la misma cuenta.
    try:
        from webapp.brokers.store import live_trading_usernames
        real_money = live_trading_usernames()
    except Exception:
        real_money = set()
    accounts = []
    for u in list_all_users(include_admin=True):
        if u["status"] != STATUS_ACTIVE:
            continue
        if u["username"] in tradier_only or u["username"] in real_money:
            continue
        path = db_path_for(u["username"])
        if not path.exists():
            init_db(initial_balance=DEFAULT_INITIAL_BALANCE, db_path=path)
            _log(f"[{u['username']}] cuenta creada -- capital inicial ${DEFAULT_INITIAL_BALANCE:,.2f}")
        accounts.append((u["username"], path))
    return accounts


def _minutes_since_last_trade(ticker: str, db_path: Path) -> float | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT timestamp FROM trades WHERE ticker = ? ORDER BY id DESC LIMIT 1", (ticker,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    last_ts = datetime.fromisoformat(row["timestamp"])
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last_ts).total_seconds() / 60


def _current_price(position: dict):
    if position["asset_type"] == "equity":
        return market_data.get_quote(position["ticker"])
    if position["asset_type"] == "crypto":
        return market_data.get_quote(f"{position['ticker']}-USD")
    if position["asset_type"] == "option":
        od = position["option_details"]
        return market_data.get_option_price(position["ticker"], od["expiration"], od["strike"], od["option_type"])
    return None


def _current_exit_price(position: dict):
    """Ver la misma nota en trading-bot/position_monitor.py -- portado
    aca 2026-09-11: si el spread del contrato esta demasiado ancho AHORA
    (no al entrar, sino en el momento de decidir el cierre), el BID no es
    un precio confiable para evaluar stop_loss/profit_target -- cae al
    punto medio en vez de castigar la posicion por ruido de cotizacion
    que nunca fue una perdida real."""
    if position["asset_type"] != "option":
        return _current_price(position)
    od = position["option_details"]
    bid, ask = market_data.get_option_bid_ask(position["ticker"], od["expiration"], od["strike"], od["option_type"])
    if bid is not None and ask is not None and ask > 0 and (ask - bid) / ask > MAX_SPREAD_PCT:
        return _current_price(position)  # spread demasiado ancho ahora mismo -- BID no es confiable
    # 2026-09-29, a pedido del usuario ("arregla el spread"): simetrico con
    # el lado de entrada (ver LIMIT_FILL_FRACTION arriba) -- vender siempre
    # al BID crudo paga el 100% del spread tambien al salir. Se simula una
    # limite marcable que recupera parte del spread (1 - LIMIT_FILL_FRACTION
    # de camino desde el bid hacia el ask) en vez de asumir el peor precio
    # posible en cada cierre.
    if bid is not None and ask is not None and ask > bid > 0:
        return bid + (ask - bid) * (1 - LIMIT_FILL_FRACTION)
    return bid if bid is not None else _current_price(position)


def _is_near_expiration(position: dict, now_utc: datetime) -> bool:
    if position["asset_type"] != "option":
        return False
    exp_date = datetime.strptime(position["option_details"]["expiration"], "%Y-%m-%d").date()
    return (exp_date - now_utc.date()).days <= 1


def _check_circuit_breaker(db_path: Path, username: str, current_prices: dict[str, float]) -> None:
    settings = get_settings(db_path)
    if not settings["bot_enabled"]:
        return
    status = get_status(current_prices=current_prices, db_path=db_path)
    if status["initial_balance"] <= 0:
        return
    daily_realized = get_daily_realized_pnl(db_path)
    daily_pct = (daily_realized + status["unrealized_pnl"]) / status["initial_balance"] * 100
    # 2026-09-18: si el usuario volvio a encender el bot a mano HOY despues de
    # un circuit breaker, el limite se duplica por el resto del dia. Antes el
    # breaker recalculaba la misma perdida (ya por encima del limite) y lo
    # volvia a apagar en el siguiente ciclo, asi que el boton "Bot apagado"
    # parecia no funcionar.
    limit = settings["max_daily_loss_pct"]
    if settings.get("breaker_override_date") == datetime.now(timezone.utc).date().isoformat():
        limit *= 2
    if daily_pct <= -limit:
        update_settings(bot_enabled=False, db_path=db_path)
        msg = (f"[{username}] CIRCUIT BREAKER: perdida del dia {daily_pct:+.2f}% supera "
               f"-{limit:.1f}% -- bot apagado para ESTA cuenta unicamente.")
        log_error(msg, reason="multi_user_entry.py: circuit breaker", db_path=db_path)
        _log(msg)


def _load_peak_state() -> dict:
    if not PEAK_PNL_STATE_PATH.exists():
        return {}
    try:
        return json.loads(PEAK_PNL_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_peak_state(state: dict) -> None:
    PEAK_PNL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PEAK_PNL_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def run_exits_for_account(username: str, db_path: Path) -> int:
    if not market_is_open_now():
        return 0
    status = get_status(db_path=db_path)
    closed = 0
    current_prices: dict[str, float] = {}
    peak_state = _load_peak_state()
    daily_bias = load_daily_market_bias()
    # 2026-09-22, a pedido del usuario ("si una posicion abierta esta dando
    # ganancia y se aproxima la hora de cierre... tomamos ganancia y
    # cerramos"): se calcula UNA vez por ciclo (no por posicion) -- ver
    # is_near_session_close en bollinger_strategy.py (usa el default de 15
    # min, EOD_FORCE_CLOSE_MINUTES_BEFORE_CLOSE).
    near_close = is_near_session_close(datetime.now(MARKET_TZ))
    for p in status["positions"]:
        price = _current_price(p)
        if price is None:
            continue
        current_prices[p["key"] if p["asset_type"] == "option" else p["ticker"]] = price
        exit_price = _current_exit_price(p)
        if exit_price is None:
            exit_price = price

        opened = datetime.fromisoformat(p["opened_at"])
        now_utc = datetime.now(timezone.utc)
        minutes_since_entry = (now_utc - opened).total_seconds() / 60

        exit_signal, hourly_aligned, rejection_signal = False, True, False
        if p["asset_type"] == "option":
            try:
                r = analyze(p["ticker"])
                exit_signal = (r["exit_short_signal"] if p["option_details"]["option_type"] == "put"
                                else r["exit_long_signal"])
                # 2026-09-16, a pedido del usuario ("chequear cualquier
                # posible rechazo... antes de que se vaya en contra") -- ver
                # la nota larga junto a EARLY_REJECTION_WINDOW_MINUTES en
                # bollinger_strategy.py. Mas rapido que exit_signal (que
                # exige cruzar toda la banda media): alcanza con que el
                # precio vuelva a cruzar hacia ADENTRO de la banda que
                # rompio al entrar. 2026-09-22: solo se evalua para
                # estrategias que de verdad entran ROMPIENDO la banda -- ver
                # _compute_rejection_signal y la nota de 2026-09-22 arriba.
                rejection_signal = _compute_rejection_signal(
                    p.get("entry_strategy"), p["option_details"]["option_type"], r,
                )
            except Exception:
                exit_signal = False
            try:
                trend_1h = get_trend(p["ticker"], interval="1h", period="3mo")
            except Exception:
                trend_1h = None
            wanted = "bajista" if p["option_details"]["option_type"] == "put" else "alcista"
            # 2026-09-15, a pedido del usuario ("sepas los riesgos... a la
            # hora de... salir"): el respaldo ahora exige que TANTO la
            # tendencia de 1h COMO el sesgo diario (daily_market_bias.py)
            # vayan a favor -- si cualquiera de los dos esta en contra, la
            # posicion ya no se considera "respaldada", y la regla de
            # asegurar ganancia temprana (EARLY_LOCK_WITHOUT_1H_SUPPORT_PCT)
            # reacciona mas rapido.
            daily_bias_ticker = daily_bias.get(p["ticker"])
            daily_aligned = daily_bias_ticker in (None, "lateral", wanted)
            hourly_aligned = (trend_1h == wanted) and daily_aligned
            # 2026-09-22, a pedido del usuario ("tiene que chequear la
            # tendencia semanal tambien... tienen que coincidir"): solo se
            # pide (llamada de red aparte) cuando de verdad puede hacer
            # falta -- cerca del cierre Y en perdida, que es la unica regla
            # que la usa (cerrar_perdida_sin_respaldo_de_tendencia). Fuera
            # de eso se deja en True (neutral, no bloquea nada) para no
            # gastar pedidos de mas cada 120s por posicion.
            weekly_aligned = True
            if near_close and exit_price is not None and exit_price <= p["avg_cost"]:
                try:
                    trend_weekly = get_trend(p["ticker"], interval="1wk", period="2y")
                except Exception:
                    trend_weekly = None
                weekly_aligned = trend_weekly == wanted
        else:
            weekly_aligned = True

        # Rastreo de pico de ganancia (2026-09-15) -- ver PEAK_PROFIT_LOCK_PCT
        # en bollinger_strategy.py. Estado compartido entre este ciclo (cada
        # 120s) y exit_watch.py (cada 15s), ambos protegidos por
        # exits_critical_section() en el llamador -- nunca corren a la vez.
        #
        # 2026-09-17, a pedido del usuario ("si el impulso frena en 15% ahi
        # vendes, no esperas a que gire de vuelta"): ademas del VALOR del
        # pico, ahora se guarda CUANDO fue la ultima vez que hizo un nuevo
        # maximo (peak_at) -- ver MOMENTUM_STALL_* en bollinger_strategy.py,
        # que usa esto para cerrar si el pico dejo de avanzar por un rato,
        # sin esperar a que encima empiece a caer. Compatible con el
        # formato viejo (un float suelto) para no romper el estado ya
        # guardado en disco de posiciones que ya estaban abiertas.
        peak_id = f"{username}::{p['key']}::{p['opened_at']}"
        current_pnl_pct = (exit_price / p["avg_cost"] - 1) if p["avg_cost"] else 0.0
        prev_entry = peak_state.get(peak_id)
        if isinstance(prev_entry, dict):
            prev_peak, prev_peak_at = prev_entry.get("peak", current_pnl_pct), prev_entry.get("peak_at")
        else:
            prev_peak, prev_peak_at = (prev_entry if prev_entry is not None else current_pnl_pct), None
        if current_pnl_pct > prev_peak:
            peak_pnl_pct, peak_at = current_pnl_pct, now_utc.isoformat()
        else:
            peak_pnl_pct, peak_at = prev_peak, prev_peak_at or now_utc.isoformat()
        peak_state[peak_id] = {"peak": peak_pnl_pct, "peak_at": peak_at}
        seconds_since_peak_update = (now_utc - datetime.fromisoformat(peak_at)).total_seconds()

        result = evaluate_open_position_exit(
            entry_premium=p["avg_cost"], current_premium=exit_price,
            profit_target_pct=p["profit_target_pct"] or 0.10, technical_exit_signal=exit_signal,
            hourly_aligned=hourly_aligned, force_eod_exit=_is_near_expiration(p, now_utc),
            stop_loss_pct=p["stop_loss_pct"] or 0.20, minutes_since_entry=minutes_since_entry,
            peak_pnl_pct=peak_pnl_pct, rejection_signal=rejection_signal,
            seconds_since_peak_update=seconds_since_peak_update, near_close=near_close,
            weekly_aligned=weekly_aligned,
        )
        if result["should_close"]:
            # 2026-09-17, a pedido del usuario (INTC cerro en +3.21% via
            # asegurar_porcion_del_pico sin poder confirmar si el pico real
            # visto en el dashboard -- ~+14% -- coincidio con el pico que
            # esta regla de verdad registro en sus propias fotos de 30s):
            # antes el pico NUNCA quedaba escrito en ningun lado una vez que
            # la posicion cerraba (peak_state.pop() lo borra del estado
            # persistido, y el log solo mostraba el P&L final). Se agrega el
            # pico aca para poder auditar de verdad la proxima vez que esto
            # pase, en vez de no poder confirmar nada con datos reales.
            reason = (f"[multi_user_entry -- cuenta {username}] Cierre automatico por regla "
                      f"'{result['reason']}' (P&L {result['pnl_pct']*100:+.2f}%, pico registrado "
                      f"{peak_pnl_pct*100:+.2f}%). Ejecutado al BID real ${exit_price:.2f}.")
            record_trade(
                side="sell", ticker=p["ticker"], asset_type=p["asset_type"], quantity=p["quantity"],
                price=exit_price, reason=reason, multiplier=p["multiplier"],
                expiration=p["option_details"]["expiration"] if p["asset_type"] == "option" else None,
                strike=p["option_details"]["strike"] if p["asset_type"] == "option" else None,
                option_type=p["option_details"]["option_type"] if p["asset_type"] == "option" else None,
                db_path=db_path,
            )
            _log(f"[{username}] CIERRE {p['ticker']} -> {result['reason']} "
                 f"({result['pnl_pct']*100:+.2f}%, pico {peak_pnl_pct*100:+.2f}%) @ {exit_price:.2f} (bid real)")
            closed += 1
            peak_state.pop(peak_id, None)
    _save_peak_state(peak_state)
    _check_circuit_breaker(db_path, username, current_prices)
    return closed


def run_entry_cycle(accounts: list[tuple[str, Path]]) -> None:
    if not accounts:
        _log("Sin usuarios activos todavia -- nada que escanear.")
        return

    # Mismo rule que trading-bot/auto_entry.py -- no abrir posiciones
    # NUEVAS a menos de NO_NEW_ENTRY_MINUTES_BEFORE_CLOSE minutos del
    # cierre (day trading, sin overnight para ENTRADAS nuevas -- las
    # posiciones YA abiertas si se mantienen overnight, eso no cambia).
    # Bug encontrado 2026-09-10: esto se habia olvidado al portar
    # family_sim.py a multi_user_entry.py.
    if too_close_to_open_new_position():
        _log(f"Cerca del cierre de sesion (<= {NO_NEW_ENTRY_MINUTES_BEFORE_CLOSE} min) o mercado cerrado -- "
             f"no se abren posiciones nuevas para ninguna cuenta.")
        return

    # 2026-09-27, a pedido del usuario ("eso se puede mejorar poner menos
    # [tickers fijos]... darle la oportunidad al usuario de que elija la
    # lista personalizada"): cada cuenta tiene su PROPIA watchlist
    # (columna settings.watchlist, ya editable desde el panel de "Ajustes"
    # -- antes se guardaba pero el motor de entradas la ignoraba por
    # completo y usaba siempre una lista compartida por defecto). Ahora
    # se escanea la UNION de las watchlists personalizadas de todas las
    # cuentas activas -- sigue siendo UNA sola pasada de yfinance por
    # ciclo, no una por usuario, para no perder la eficiencia medida el
    # mismo dia (ver capacity_test.py / memoria). fast_watchlist sigue
    # siendo compartida por ahora (todavia no hay campo en el panel para
    # personalizarla). Cada cuenta despues SOLO considera señales de SU
    # PROPIA lista (ver el filtro dentro del loop por cuenta mas abajo) --
    # eleccion libre del usuario, no un universo fijo acotado (a pedido
    # explicito), asi que el tamaño real de watchlist_union puede crecer
    # con la cantidad de cuentas -- vigilar el tiempo de ciclo si crece
    # mucho.
    settings_main = get_settings()  # fast_watchlist compartida; watchlist NO se usa de aca en adelante
    account_watchlist: dict[str, set[str]] = {}
    account_affordable: dict[str, set[str]] = {}
    watchlist_union: set[str] = set()
    for username, db_path in accounts:
        acc_settings = get_settings(db_path)
        acc_status = get_status(db_path=db_path)
        # 2026-10-05, a pedido explicito ("que cada usuario elija con cual
        # watchlist trabajar"): cada lista ahora tiene su propio
        # interruptor (ver _ensure_settings_table) -- una cuenta puede
        # apagar su watchlist propia, la fast_watchlist compartida, o
        # ambas, sin tener que vaciarlas (las sigue viendo en el panel,
        # el motor simplemente no las escanea).
        my_watchlist: set[str] = set()
        if acc_settings.get("default_capital_list_enabled", False):
            # 2026-10-05, a pedido explicito ("por defecto sale una lista
            # basada en su capital"): en este modo se ignoran las listas
            # personalizadas (ya quedan apagadas en cascada por
            # update_settings()) y se escanea TODO el universo por
            # defecto -- el prefiltro de poder de compra de mas abajo es
            # el que de verdad recorta esto a lo que la cuenta puede
            # pagar, dejando "la lista basada en su capital".
            my_watchlist |= set(DEFAULT_WATCHLIST.split(",")) | set(DEFAULT_FAST_WATCHLIST.split(","))
        else:
            if acc_settings.get("watchlist_enabled", True):
                my_watchlist |= set(acc_settings["watchlist"])
            if acc_settings.get("fast_watchlist_enabled", True):
                my_watchlist |= set(settings_main["fast_watchlist"])
        account_watchlist[username] = my_watchlist
        watchlist_union.update(my_watchlist)
        # Pre-filtro por poder de compra (2026-09-14, a pedido del
        # usuario: "el bot no tiene que analizar los 45") -- solo dentro
        # de la lista de ESTA cuenta, calculado con SOLO el precio spot
        # (barato, ya cacheado), antes de correr el analisis completo de
        # 5 estrategias por ticker.
        # 2026-10-05, a pedido explicito ("a esa lista agregale la opcion
        # de usarla o no"): antes el prefiltro de poder de compra corria
        # SIEMPRE -- ahora cada cuenta puede apagarlo (acepta el costo de
        # analizar tickers que probablemente no pueda pagar, por si el
        # precio cambia o quiere ver el analisis completo igual).
        if acc_settings.get("affordable_filter_enabled", True):
            affordable = estimate_affordable_symbols(
                list(my_watchlist), acc_status["total_account_value"], float(acc_settings["risk_pct_per_trade"]),
                acc_status["cash_balance_available"],
                sizing_mode=acc_settings.get("sizing_mode", "auto"), fixed_contracts=int(acc_settings["contracts_per_trade"]),
            )
        else:
            affordable = list(my_watchlist)
        account_affordable[username] = set(affordable)

    watchlist = list(watchlist_union)
    symbols_to_scan = [s for s in watchlist if any(s in aff for aff in account_affordable.values())]
    if len(symbols_to_scan) < len(watchlist):
        _log(f"Pre-filtro por poder de compra: analizando {len(symbols_to_scan)}/{len(watchlist)} tickers este ciclo (union de watchlists personalizadas de {len(accounts)} cuenta(s)).")

    def _scan(sym):
        try:
            return sym, scan_all_signals(sym), None
        except Exception as e:
            return sym, None, e

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(_scan, symbols_to_scan))

    signals = []
    for symbol, strategy_results, error in results:
        if error is not None:
            _log(f"[scan] ERROR {symbol}: {error}")
            continue
        # 2026-10-01, a pedido del usuario ("unir toda la logica de
        # estrategia... crear mejor entrada y mayor seguridad"): confluencia
        # entre squeeze_breakout y giro_sma20 (las 2 estrategias realmente
        # activas hoy) -- validado con datos reales (44 tickers, 60 dias):
        # cuando las dos coinciden en la MISMA direccion para el mismo
        # ticker en el mismo ciclo, 60% acierto / +0.44% promedio (n=559),
        # estable OOS (67%/+0.58% primera mitad, 53%/+0.30% segunda) --
        # contra 46%/-0.07% de una sola estrategia sola (n=5180). Se eleva
        # la confianza a "alta" (ya ganada con datos propios, no la del
        # respaldo de tendencia de 1h) y se prioriza en el orden de
        # ejecucion. No bloquea nada -- las señales sin confluencia siguen
        # evaluandose igual que siempre.
        by_strategy = {r["strategy"]: r["signal"] for r in strategy_results if r["signal"] != "none"}
        squeeze_sig = by_strategy.get("squeeze_breakout")
        giro_sig = by_strategy.get("giro_sma20")
        has_confluence = squeeze_sig is not None and squeeze_sig == giro_sig
        for r in strategy_results:
            if r["signal"] == "none":
                continue
            # 2026-10-01, refinado via laboratorio de busqueda sistematica
            # (grilla de 36 combinaciones reales, 5772 señales, 44 tickers):
            # confluencia + confianza baja NATURAL (1h claramente en
            # contra) rindio peor que confluencia + media/alta (+0.40%
            # n=247 con baja incluida vs +0.50% n=225 excluyendola) -- las
            # ~22 señales de confluencia+baja arrastraban el promedio hacia
            # abajo. Antes esto subia CUALQUIER confluencia a "alta" sin
            # mirar la confianza natural; ahora solo se eleva si la
            # confianza natural YA es media o alta -- si es baja, la
            # confluencia NO la rescata, sigue bloqueada como cualquier
            # señal de confianza baja.
            if has_confluence and r["strategy"] in ("squeeze_breakout", "giro_sma20") and r["confidence"] != "baja":
                r["has_confluence"] = True
                r["confidence"] = "alta"
            else:
                r["has_confluence"] = False
            signals.append(r)
    signals.sort(key=lambda r: (not r.get("has_confluence", False), CONFIDENCE_ORDER[r["confidence"]], -r.get("volume_ratio", 0)))

    if not signals:
        _log(f"Sin señales de entrada este ciclo (escaneo compartido para {len(accounts)} cuenta(s)).")
        return

    unique_symbols = {r["symbol"] for r in signals}
    spot_prices = {sym: market_data.get_quote(sym) for sym in unique_symbols}

    # Guia diaria por ticker (2026-09-15, a pedido del usuario -- ver
    # daily_guide.py): capa extra de prudencia sobre el historial REAL
    # de cada ticker especifico, no solo la señal del momento. Si el
    # archivo no existe todavia (primer dia, o el cron de la madrugada
    # todavia no corrio), daily_guide devuelve {} -- ningun ticker queda
    # bloqueado por falta de datos, solo se exige mas cuando SI hay
    # evidencia real de que un ticker rinde mal.
    daily_guide = load_daily_guide()

    # Sesgo diario de mercado (2026-09-15, a pedido del usuario -- ver
    # daily_market_bias.py): alcista/bajista/lateral por ticker segun el
    # ultimo cierre diario confirmado. {} si el cron de la mañana
    # todavia no corrio -- ningun ticker queda bloqueado por falta de
    # datos, igual que daily_guide.
    daily_bias = load_daily_market_bias()

    # 2026-09-23, a pedido del usuario ("investiga a profundidad y arregla
    # el problema" -- PLTR entro comprando un call mientras el precio ya
    # venia cayendo en los minutos previos). Ver el estudio real completo
    # y la funcion en bollinger_strategy.recent_momentum_pct: 75 entradas
    # reales de los ultimos 8 dias, en contra del momentum de los ultimos
    # RECENT_MOMENTUM_MINUTES minutos promediaron -2.39% (n=15) vs +6.34%
    # a favor/neutro (n=60). Se calcula UNA vez por ciclo (no por señal ni
    # por cuenta) igual que spot_prices. None (feriado, red caida, simbolo
    # nuevo) significa "sin informacion" y NO bloquea la entrada.
    recent_momentum = {sym: recent_momentum_pct(sym) for sym in unique_symbols}

    # 2026-09-27, mismo pedido que recent_momentum arriba -- solo registro,
    # una vez por ciclo (nivel de mercado, no por ticker).
    vix_now = current_vix()

    # 2026-09-27, a pedido del usuario ("si agregalo igual" -- ver nota
    # junto a has_earnings_today arriba en bollinger_strategy.py). Una vez
    # por ciclo, igual que recent_momentum/vix_now.
    earnings_today = {sym: has_earnings_today(sym) for sym in unique_symbols}

    # 2026-09-15: contador de respaldo -- ver la nota junto a su uso mas
    # abajo (bug real encontrado hoy: watchdog.py reiniciaba este proceso
    # en falso porque, con tantos filtros de entrada nuevos hoy, un ciclo
    # entero podia terminar sin escribir NADA en el log).
    total_trades_this_cycle = 0

    # 2026-09-17, a pedido del usuario ("agrega un log mas detallado"):
    # antes, cuando un ciclo detectaba señales pero ninguna pasaba los
    # filtros, el log solo guardaba el CONTEO -- no habia forma de saber
    # despues (con datos reales) cual filtro especifico las freno. Este
    # dict guarda, por señal (symbol+estrategia+direccion), el motivo del
    # ultimo "continue" que la descarto -- no es 100% preciso por cuenta
    # (los filtros de poder de compra/posicion abierta/cooldown pueden
    # variar entre cuentas), pero para los filtros que NO dependen de la
    # cuenta (confianza, volatilidad, sesgo diario, noticias) el motivo es
    # el mismo para todas, y es la informacion que mas hace falta para
    # diagnosticar sin adivinar.
    rejection_reason: dict[tuple, str] = {}

    # 2026-10-06, a pedido explicito: Metabot exige Pro activo para abrir
    # posiciones nuevas (ver can_open_positions en webapp/auth.py). Las
    # salidas las maneja exit_watch_loop.py, no pasan por aca.
    can_open = {u["username"]: can_open_positions(u) for u in list_all_users(include_admin=True)}

    for username, db_path in accounts:
        settings = get_settings(db_path)
        if not settings["bot_enabled"]:
            continue
        if not can_open.get(username, False):
            continue
        status = get_status(db_path=db_path)
        open_tickers = {p["ticker"] for p in status["positions"]}
        trades_this_cycle = 0
        used_tickers = set()

        my_affordable = account_affordable.get(username, set())
        my_watchlist = account_watchlist.get(username, set())
        for r in signals:
            if trades_this_cycle >= settings["max_trades_per_day"]:
                break
            # 2026-09-27: la señal es de la UNION de todas las cuentas --
            # esta cuenta solo opera si el ticker esta en SU PROPIA
            # watchlist (ver nota junto a account_watchlist mas arriba).
            # Sin rejection_reason -- no es un problema de calidad de la
            # señal, es simplemente que esta cuenta no eligio ese ticker.
            if r["symbol"] not in my_watchlist:
                continue
            key = (r["symbol"], r["strategy"], r["signal"])
            # 2026-09-27: squeeze_breakout_temprano con la logica VIEJA de
            # margen dio 31.2% ganadoras, -3.32% promedio, -53.1pts en 16
            # operaciones reales -- se desactivo. REACTIVADA 2026-10-02 a
            # pedido explicito del usuario ("activala con la logica que
            # hay en la foto"), ahora con logica NUEVA de toque+impulso
            # (ver analyze_forming_bar en bollinger_strategy.py) -- sin
            # backtest ni historial real todavia de esta version. Sigue
            # exigiendo confianza alta (ver chequeo mas abajo). Para
            # REVERTIR: descomentar el bloque de abajo.
            # if r["strategy"] == "squeeze_breakout_temprano":
            #     rejection_reason[key] = "squeeze_breakout_temprano desactivada"
            #     continue
            # REACTIVADO 2026-09-30, a pedido explicito del usuario ("vas a
            # hacer las entradas... tanto put como call"): el 2026-09-27 se
            # habia pausado porque dio +0.10% promedio en 64 operaciones
            # reales -- practicamente empate, muy por debajo de giro_sma20
            # PUT (+3.88%, n=74). El usuario fue avisado de ese historial
            # antes de reactivarlo y pidio seguir igual. Para volver a
            # pausar: reinstalar el bloque de abajo (comentado, no borrado).
            # if r["strategy"] == "giro_sma20" and r["signal"] == "buy_call":
            #     rejection_reason[key] = "giro_sma20 en CALL pausado (2026-09-27, historial real: +0.10% promedio, practicamente empate, n=64)"
            #     continue
            # 2026-09-29, a pedido del usuario ("que cuando la sma20 se gire
            # entre sin esperar confirmacion"): giro_sma20 con confianza
            # BAJA (1h en contra del giro) se probo con datos reales, 1h30
            # adelante (la misma ventana que describio el usuario), n=54,
            # dividido cronologicamente: 59% acierto / +0.045% primera
            # mitad, 59% acierto / +0.012% segunda mitad -- estable en las
            # dos mitades, a diferencia de otras ideas descartadas hoy
            # (Fibonacci, MTF Bollinger) que se invertian. Coincide con el
            # hallazgo previo del estudio OOS de 9/18: "1h-trend confidence
            # no tiene valor predictivo". GIRO_ALLOWS_BAJA solo afecta a
            # giro_sma20 -- las demas estrategias siguen exigiendo al menos
            # confianza media (ALLOW_LOW_CONFIDENCE global sigue False).
            giro_baja_exempt = GIRO_ALLOWS_BAJA and r["strategy"] == "giro_sma20"
            if r["confidence"] == "baja" and not ALLOW_LOW_CONFIDENCE and not giro_baja_exempt:
                rejection_reason[key] = f"confianza baja (se exige media o alta)"
                continue
            # 2026-09-15, a pedido del usuario ("corregir las entradas en
            # falsas volatilidades"): giro_sma20 salio 0/2 hoy en confianza
            # media (SPY -20.69%, QQQ -20.11%), a diferencia de
            # squeeze_breakout (10/13, +4.98% promedio historico) -- a
            # diferencia de squeeze_breakout, giro_sma20 no exige compresion
            # de bandas ni volumen alto por diseño (una de las dos perdidas
            # de hoy tuvo volume_ratio apenas 1.57x, "regular"), asi que es
            # mas propenso a "giros" que son solo ruido. Se exige respaldo
            # real de tendencia 1h (confianza alta) para operarlo, en vez de
            # desactivarlo del todo -- con solo 2 datos no alcanza para
            # descartarlo, pero si para pedir mas confirmacion.
            if _giro_blocked_by_confidence(r):
                rejection_reason[key] = f"giro_sma20 exige confianza alta (tiene {r['confidence']})"
                continue
            # 2026-09-16, a pedido del usuario ("necesitamos mas confianza"):
            # squeeze_breakout_temprano (entra sobre una vela de 15m TODAVIA
            # SIN CERRAR, para no perder tiempo) en confianza media salio
            # 3/13 en el historial real (23.1% de acierto, -5.50% promedio)
            # -- mucho peor que squeeze_breakout normal. Tiene sentido: actua
            # sobre datos incompletos, mas propenso a revertirse antes de que
            # la vela termine de cerrar (caso real hoy: SQQQ se revirtio en
            # 31 segundos). Confianza alta todavia no tiene muestra (n=1),
            # pero exigirla es la misma logica de graduar el riesgo que ya
            # se aplica en giro_sma20.
            if r["strategy"] == "squeeze_breakout_temprano" and r["confidence"] != "alta":
                rejection_reason[key] = f"squeeze_breakout_temprano exige confianza alta (tiene {r['confidence']})"
                continue
            if r["strategy"] == "squeeze_breakout" and r["confidence"] != "alta" and _squeeze_needs_alta():
                rejection_reason[key] = f"squeeze_breakout despues de las 10:15 exige confianza alta (tiene {r['confidence']})"
                continue
            # 2026-09-15, mismo dia: MARA perdio -23.73% con volume_ratio de
            # apenas 1.19x ("regular", el umbral de "extrema" ya definido en
            # bollinger_strategy.STRONG_VOLUME_MULT es 2.0x). Separando el
            # historial de squeeze_breakout por fuerza de volumen: "extrema"
            # dio 8/10 ganadoras (+7.47% promedio), "regular" dio -2.89%
            # promedio (arrastrado por MARA). Igual que con giro_sma20, se
            # exige la confirmacion real de volumen en vez de confiar solo en
            # la confianza -- el propio squeeze_breakout ya calcula esto para
            # el tamaño del objetivo de ganancia, solo faltaba usarlo tambien
            # para decidir si entrar.
            if (r["strategy"] in ("squeeze_breakout", "squeeze_breakout_temprano")
                    and r.get("volatility_strength") != "extrema"):
                rejection_reason[key] = f"exige volatilidad extrema (tiene {r.get('volatility_strength')})"
                continue
            # 2026-09-27, a pedido del usuario ("puedes revisar los
            # squeezes 3 5 en un backtesting" -> "si implementalo"):
            # validado OOS (2 mitades cronologicas + controlado por
            # estrategia + 14 tickers distintos, no concentrado en 1-2
            # nombres): squeeze_breakout con 6+ velas consecutivas de
            # compresion dio +1.59% promedio (n=73) contra +10.48% en
            # 3-5 velas (n=109) -- un squeeze que se alarga demasiado es
            # un setup estancado/menos confiable, no uno "mas comprimido
            # y mejor". El lado corto (1-2 velas) NO se toca -- aislado de
            # squeeze_breakout_temprano (ya desactivada) rindio bien
            # (+9.38%, n=12).
            if r["strategy"] == "squeeze_breakout" and (r.get("consecutive_squeeze_bars") or 0) > 5:
                rejection_reason[key] = f"squeeze_breakout con {r.get('consecutive_squeeze_bars')} velas de compresion consecutivas (>5 rinde peor: +1.59% promedio vs +10.48% en 3-5 velas)"
                continue
            if earnings_today.get(r["symbol"]):
                rejection_reason[key] = f"{r['symbol']} reporta earnings hoy o mañana -- IV crush esperado, se evita por principio (2026-09-27)"
                continue
            # 2026-09-23, a pedido del usuario -- ver nota junto a
            # recent_momentum arriba (estudio real 2026-09-23, n=75).
            mom = recent_momentum.get(r["symbol"])
            if mom is not None:
                aligned_momentum = mom if r["signal"] == "buy_call" else -mom
                if aligned_momentum < 0:
                    rejection_reason[key] = (
                        f"momentum de los ultimos {RECENT_MOMENTUM_MINUTES} min en contra de la señal "
                        f"({mom:+.2f}% en el subyacente)"
                    )
                    continue
            ticker_verdict = daily_guide.get(r["symbol"], {}).get("verdict")
            if ticker_verdict == "cuidado" and r["confidence"] != "alta":
                rejection_reason[key] = f"ticker marcado 'cuidado' en la guia diaria, exige confianza alta (tiene {r['confidence']})"
                continue  # historial real de 60 dias malo en este ticker -- exige confianza alta
            # 2026-09-15, a pedido del usuario ("tener en cuenta si es
            # alcista o bajista... para cuando vayas a operar sepas los
            # riesgos a la hora de entrar"): si la señal va CONTRA el
            # sesgo diario del ticker (CALL en uno bajista, PUT en uno
            # alcista), es una entrada de mas riesgo -- se exige
            # confianza alta, mismo mecanismo que giro_sma20 y "cuidado".
            # Ver daily_market_bias.py.
            ticker_bias = daily_bias.get(r["symbol"])
            wanted_bias = "alcista" if r["signal"] == "buy_call" else "bajista"
            if ticker_bias not in (None, "lateral", wanted_bias) and r["confidence"] != "alta":
                rejection_reason[key] = f"va contra el sesgo diario del ticker ({ticker_bias}), exige confianza alta (tiene {r['confidence']})"
                continue  # señal contra el sesgo diario del ticker -- exige confianza alta
            # 2026-09-15, a pedido del usuario (noticias reales en tiempo
            # real -- ver news_watch.py): si hay una noticia real de Yahoo
            # Finance publicada en las ultimas 6h para este ticker, el
            # precio puede estar moviendose por la noticia y no por la
            # señal tecnica -- se exige confianza alta. Solo se chequea si
            # todavia no es alta (ahorra la llamada de red si ya no hace
            # falta).
            if r["confidence"] != "alta":
                has_news, _headline = has_recent_news(r["symbol"])
                if has_news:
                    rejection_reason[key] = "noticia reciente (< 6h) sobre el ticker, exige confianza alta"
                    continue
            if r["symbol"] not in my_affordable:
                rejection_reason[key] = f"ninguna cuenta puede pagarlo ahora (ultima chequeada: {username})"
                continue  # pre-filtro por poder de compra -- ver estimate_affordable_symbols
            if r["symbol"] in open_tickers or r["symbol"] in used_tickers:
                rejection_reason[key] = f"ya hay posicion abierta en {r['symbol']} (cuenta {username})"
                continue
            if r["strategy"] == "giro_sma20" and not _giro_sma20_allowed_now():
                rejection_reason[key] = "giro_sma20 fuera de su ventana horaria permitida"
                continue
            minutes_since = _minutes_since_last_trade(r["symbol"], db_path)
            if minutes_since is not None and minutes_since < REENTRY_COOLDOWN_MINUTES:
                rejection_reason[key] = f"cooldown de reentrada ({minutes_since:.1f} de {REENTRY_COOLDOWN_MINUTES} min, cuenta {username})"
                continue

            spot = spot_prices.get(r["symbol"])
            if spot is None:
                rejection_reason[key] = "sin precio spot disponible"
                continue
            # 2026-10-01, a pedido del usuario ("busca en la web los tips
            # que necesitamos implementar... agrega la mejora"): "calor de
            # portafolio" -- practica estandar de gestion de riesgo
            # profesional (ver quantwheel.com/learn/position-sizing,
            # tradewink.com/learn/risk-management-day-trading-complete-guide):
            # topear la EXPOSICION TOTAL simultanea de la cuenta (no solo
            # el riesgo de CADA operacion por separado, que ya cubre Kelly/
            # MAX_MANUAL_CONTRACT_PCT_OF_ACCOUNT). Sin esto, nada impedia
            # tener 5 posiciones abiertas al 3% cada una = 15% de la cuenta
            # en riesgo simultaneo. Los profesionales toperan 6-10% total;
            # se usa 10% (el extremo mas permisivo del rango real) para no
            # bloquear de mas sin evidencia de que haga falta mas estricto.
            current_exposure_pct = (
                sum(p.get("market_value") or 0 for p in status["positions"])
                / status["total_account_value"] * 100
                if status["total_account_value"] > 0 else 0
            )
            if current_exposure_pct >= PORTFOLIO_HEAT_CAP_PCT:
                rejection_reason[key] = (
                    f"calor de portafolio al limite ({current_exposure_pct:.1f}% de {PORTFOLIO_HEAT_CAP_PCT}% "
                    f"maximo, cuenta {username}) -- demasiadas posiciones abiertas a la vez"
                )
                continue
            option_type = "call" if r["signal"] == "buy_call" else "put"
            # Tamano de posicion por riesgo (cada usuario tiene su propio
            # settings["risk_pct_per_trade"], % de SU valor de cuenta) --
            # 2026-09-13, reemplaza el contracts_per_trade fijo. La
            # cantidad se deriva DENTRO de select_affordable_contract()
            # segun el precio real del contrato elegido.
            risk_pct = float(settings["risk_pct_per_trade"])
            contract, method, quantity = select_affordable_contract(
                r["symbol"], option_type, spot, status["cash_balance_available"], status["total_account_value"], risk_pct,
                sizing_mode=settings.get("sizing_mode", "auto"), fixed_contracts=int(settings["contracts_per_trade"]),
            )
            if contract is None:
                if method == "capital_insuficiente":
                    # 2026-09-21, a pedido del usuario: sin capital para el contrato
                    # estandar NO se baja a uno mas barato -- se salta este ticker y la
                    # cuenta sigue con la siguiente señal (ver family_sizing.py).
                    rejection_reason[key] = f"capital insuficiente para el contrato estandar de {r['symbol']} (cuenta {username}) -- no se baja a un contrato mas barato, se sigue con otra empresa"
                else:
                    rejection_reason[key] = f"ningun contrato paso los filtros de seleccion (spread/liquidez/prima minima) para {r['symbol']}"
                continue

            # 2026-09-29, a pedido del usuario ("arregla el spread"): comprar
            # siempre al ASK completo paga el 100% del spread bid/ask en cada
            # entrada -- un trader real manda una orden limite marcable cerca
            # del punto medio, no una orden a mercado. Real: 37 entradas con
            # spread registrado promediaron 7.30% de spread (ask vs bid). Se
            # simula una limite marcable al 65% del camino entre bid y ask
            # (mas cerca del ask que del medio, para seguir siendo realista
            # sobre que se llena en un mercado liquido) en vez del 100%.
            # Simetrico con el lado de salida (_current_exit_price usa el
            # mismo LIMIT_FILL_FRACTION). Si no hay bid real (dato
            # faltante), se cae al ask completo -- mismo comportamiento que
            # antes.
            bid_c = contract.get("bid")
            ask_c = contract.get("ask")
            if bid_c is not None and ask_c is not None and ask_c > bid_c > 0:
                fill_price = bid_c + (ask_c - bid_c) * LIMIT_FILL_FRACTION
            else:
                fill_price = ask_c or contract["price"]
            reason = (
                f"[multi_user_entry -- cuenta {username}] Señal {r['strategy']} "
                f"{r['signal'].upper()} (confianza {r['confidence']}). Contrato elegido: {method}. "
                f"Ejecutado a limite marcable ${fill_price:.2f} (ask real {ask_c}). {r['reason']}"
            )
            # 2026-09-27: spread real del contrato elegido, en % del ask --
            # solo registro (ver nota junto a entry_vix en engine.py).
            spread_pct = None
            if contract.get("ask") and contract.get("bid") is not None and contract["ask"] > 0:
                spread_pct = (contract["ask"] - contract["bid"]) / contract["ask"] * 100
            # 2026-09-30, a pedido explicito del usuario ("vas a tomar una
            # ganancia de 10%..." luego "cundo haya confianza media vendes
            # el contrato al 5% sin excusas"): objetivo FIJO para giro_sma20,
            # graduado por confianza -- alta 10%, media 5%. Antes usaba el
            # objetivo dinamico 10-19% segun volumen. Solo afecta giro_sma20
            # -- las demas estrategias siguen con su objetivo dinamico.
            if r["strategy"] == "giro_sma20":
                entry_profit_target = 0.05 if r["confidence"] == "media" else 0.10
            else:
                entry_profit_target = r.get("profit_target_pct")
            try:
                record_trade(
                    side="buy", ticker=r["symbol"], asset_type="option", quantity=quantity,
                    price=fill_price, reason=reason, multiplier=100.0,
                    expiration=contract["expiration"], strike=contract["strike"], option_type=option_type,
                    profit_target_pct=entry_profit_target, stop_loss_pct=r.get("stop_loss_pct"),
                    entry_delta=contract["delta"], entry_signal=r["signal"],
                    entry_volatility_strength=r.get("volatility_strength"),
                    entry_band_width_pct=r.get("band_width_pct"),
                    entry_band_width_percentile=r.get("band_width_percentile"),
                    entry_volume_ratio=r.get("volume_ratio"),
                    entry_consecutive_squeeze_bars=r.get("consecutive_squeeze_bars"),
                    entry_strategy=r["strategy"], entry_implied_volatility=contract.get("implied_volatility"),
                    entry_clv=r.get("clv"), entry_day_pos=r.get("day_pos"),
                    entry_confidence=r["confidence"], entry_spread_pct=spread_pct,
                    entry_open_interest=contract.get("open_interest"), entry_vix=vix_now,
                    db_path=db_path,
                )
                _log(f"[{username}] EJECUTADO {r['signal'].upper()} {r['symbol']} strike {contract['strike']} "
                     f"@ {fill_price:.2f} ask real ({method}) [clv={r.get('clv')} day_pos={r.get('day_pos')}]")
                trades_this_cycle += 1
                total_trades_this_cycle += 1
                used_tickers.add(r["symbol"])
                rejection_reason.pop(key, None)
                status = get_status(db_path=db_path)
            except Exception as e:
                _log(f"[{username}] OMITIDO {r['symbol']}: {e}")

    if total_trades_this_cycle == 0:
        # Bug real encontrado 2026-09-15: `signals` no estaba vacio (por
        # eso no se corto antes con el log de "Sin señales de entrada"),
        # pero TODAS las señales terminaron descartadas por los filtros de
        # cada cuenta (confianza, volumen, sesgo diario, noticias, etc.)
        # sin que ninguna rama del loop de arriba escribiera una linea de
        # log -- el ciclo terminaba en silencio total. watchdog.py mide
        # "colgado" por inactividad del log (> 720s) y reiniciaba este
        # proceso sano una y otra vez durante horas de mercado, cada vez
        # que coincidian varios ciclos seguidos sin ninguna señal que
        # pasara los filtros. Este log de respaldo garantiza que el
        # archivo SIEMPRE crece cada ciclo, pase lo que pase.
        _log(f"{len(signals)} señal(es) detectada(s) este ciclo, ninguna paso los filtros de entrada "
             f"de ninguna cuenta activa.")
        for (symbol, strategy, signal), why in rejection_reason.items():
            _log(f"  [motivo] {symbol} {signal} ({strategy}): {why}")


def run_cycle() -> None:
    accounts = active_accounts()
    closed_total = 0
    with exits_critical_section():
        for username, db_path in accounts:
            closed_total += run_exits_for_account(username, db_path)
    run_entry_cycle(accounts)
    if closed_total:
        _log(f"Ciclo completo -- {closed_total} posicion(es) cerradas en total esta vuelta.")


def main():
    parser = argparse.ArgumentParser(description="Motor de entradas y salidas multi-usuario (usuarios reales, activos)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        run_cycle()
        return

    acquire_single_instance_lock("multi_user_entry")
    _log(f"multi_user_entry.py iniciado -- ciclo cada {args.interval}s.")
    while True:
        try:
            run_cycle()
        except Exception as e:
            _log(f"ERROR en el ciclo: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
