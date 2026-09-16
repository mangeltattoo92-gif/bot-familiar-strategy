"""
Estrategias de Bandas de Bollinger (20 periodos, 2 desviaciones estandar)
para generar SENALES de compra/venta en el sistema de paper trading. Hay
DOS estrategias independientes, ambas usando las mismas bandas pero con
logica de entrada opuesta -- ver scan_all_signals() para correr las dos a
la vez sobre un ticker reutilizando la misma descarga de datos:

  1) squeeze_breakout (analyze()): A FAVOR de una ruptura tras compresion
     (ver reglas de COMPRA/VENTA mas abajo).
  2) gap_fade_apertura (detect_opening_gap_fade()): EN CONTRA de un gap de
     apertura (9:30 ET) extremo -- apuesta a reversion, no a continuacion
     (ver seccion propia mas abajo en el codigo).

Este modulo SOLO analiza datos historicos (via yfinance) y devuelve una
senal + el texto de "razon" que exige el usuario. NO coloca ninguna orden,
real ni simulada, por si solo -- eso se hace a mano (con aprobacion) via
paper_trade.py, usando el texto de razon que aqui se genera.

Reglas de squeeze_breakout (definidas por el usuario):

COMPRA (activo / CALL si es opcion):
  - Las bandas han estado "apretadas" (squeeze / baja volatilidad) durante
    varios periodos consecutivos.
  - El precio rompe hacia arriba, cerrando por encima de la banda superior.
  - El volumen de esa ruptura es mayor al promedio reciente.

VENTA / PUT (o cierre de una posicion larga):
  - El precio cruza hacia abajo la banda media, O
  - El precio rompe hacia abajo la banda inferior con volumen alto.
  - Si se opera con opciones: squeeze + ruptura a la baja con volumen alto
    -> senal para comprar un PUT.

Objetivo de ganancia (toma de beneficio), segun la fuerza de la ruptura:
  - Ruptura con volumen EXTREMO (>= STRONG_VOLUME_MULT veces el promedio):
    objetivo entre +15% y +20% sobre la prima, escalando segun que tan
    extremo sea el volumen (mas volumen = mas cerca del 20%).
  - Ruptura con volumen normal (mayor al promedio pero por debajo del
    umbral extremo): objetivo fijo de +10% sobre la prima.
  - Esto compite con las senales tecnicas de salida y con el cierre
    obligatorio de fin de dia: lo que ocurra primero cierra la posicion.

Dia trading: las posiciones de opciones abiertas con esta estrategia se
deben cerrar el mismo dia (no se mantienen overnight), asi que ademas de
la senal tecnica de salida y el objetivo de ganancia, se marca un aviso
cuando el ultimo dato ya esta cerca del cierre de mercado.

El analisis incluye datos de PREMARKET (prepost=True en yfinance): las
barras intradia (15m/30m/1h) reflejan el movimiento antes de la apertura
regular (4:00-9:30 ET), asi que un squeeze/ruptura que arranca en premarket
ya se detecta antes de que abra la sesion regular, en vez de perderse.
"""

import pickle
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeoutError
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import yfinance as yf

from paper_trading.nyse_calendar import is_trading_day, market_close_time

MARKET_TZ = ZoneInfo("America/New_York")

# Limite de pared duro sobre las descargas de yfinance -- sin esto, un
# rate-limit sostenido de Yahoo puede dejar la llamada colgada mucho mas de
# lo esperado, congelando watchlist_scan.py / proximity_watch.py /
# position_monitor.py (que reutilizan estas funciones) de forma indefinida.
_NETWORK_TIMEOUT = 15  # segundos
_history_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="yf-hist-timeout")

# Backoff ante rate-limit de Yahoo (2026-09-10, ver misma nota en
# webapp/market_data.py -- mismo fix aplicado aca porque bollinger_strategy
# tiene su propio punto de descarga independiente).
#
# CORRECCION el mismo dia: la primera version reintentaba hasta 3 veces
# con timeout completo de 15s cada vez + backoff 2/4/8s -- eso podia
# tardar ~65s POR TICKER durante un rate-limit sostenido, y con varios
# tickers fallando seguido en la misma corrida (proximity_watch.py con
# max_workers=3) el ciclo entero se estiraba varios minutos y volvia a
# disparar el mismo "colgado" que este sistema ya habia resuelto antes
# (ver seccion "Causa raiz ya corregida" mas arriba). Ahora hay un
# PRESUPUESTO DE TIEMPO TOTAL por ticker, no reintentos sin techo.
_RATE_LIMIT_RETRIES = 1
_RATE_LIMIT_BACKOFF_BASE = 1.5
_FETCH_TOTAL_BUDGET = 20  # segundos MAXIMO por ticker entre todos los intentos juntos


def _looks_like_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "too many requests" in msg or "rate limit" in msg or "429" in msg


# Cache de historial COMPARTIDA ENTRE PROCESOS via archivo, no solo en
# memoria de un proceso (2026-09-10, causa raiz real del bloqueo de
# yfinance de hoy: auto_entry.py, proximity_watch.py, position_monitor.py
# y family_sim.py son 4 procesos SEPARADOS, cada uno con su propia sesion
# de yfinance, pidiendo el historial de los MISMOS tickers una y otra vez
# con minutos de diferencia -- 4x mas llamadas de las necesarias. Un
# cache en memoria de un solo proceso no ayuda ahi porque cada proceso
# tiene su propia memoria; este cache usa el disco para que los 4 lo
# compartan de verdad.
_HIST_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "hist_cache"
_HIST_CACHE_TTL = 45  # segundos -- bastante para cubrir el solape entre procesos, corto para no operar con datos viejos


def _hist_cache_path(symbol: str, period: str, interval: str) -> Path:
    safe = f"{symbol}_{period}_{interval}".replace("/", "_").replace(" ", "_")
    return _HIST_CACHE_DIR / f"{safe}.pkl"


def _stale_cache_or_raise(cache_path: Path, exc: Exception):
    """Ultimo recurso: un dato VIEJO del cache (aunque ya vencido) es
    mejor que dejar el ciclo entero sin señal por un rate-limit sostenido
    de Yahoo. Si no hay nada guardado, se propaga el error original."""
    if cache_path.exists():
        try:
            with open(cache_path, "rb") as f:
                return pickle.load(f)
        except Exception:
            pass
    raise exc


def _fetch_history(symbol: str, period: str, interval: str):
    cache_path = _hist_cache_path(symbol, period, interval)
    try:
        if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < _HIST_CACHE_TTL:
            with open(cache_path, "rb") as f:
                return pickle.load(f)
    except Exception:
        pass  # cache corrupta/en escritura por otro proceso -- no bloquea, sigue a pedir de red

    deadline = time.time() + _FETCH_TOTAL_BUDGET
    last_exc = None
    attempt = 0
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return _stale_cache_or_raise(cache_path, last_exc or TimeoutError(
                f"Descarga de {symbol} ({period}/{interval}) supero el presupuesto de {_FETCH_TOTAL_BUDGET}s"))

        future = _history_executor.submit(
            lambda: yf.Ticker(symbol).history(period=period, interval=interval, prepost=True)
        )
        try:
            hist = future.result(timeout=min(_NETWORK_TIMEOUT, remaining))
            try:
                _HIST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                with open(cache_path, "wb") as f:
                    pickle.dump(hist, f)
            except Exception:
                pass  # guardar el cache es un plus -- si falla no debe tumbar el fetch que si funciono
            return hist
        except _FutureTimeoutError:
            last_exc = TimeoutError(f"Descarga de {symbol} ({period}/{interval}) excedio {_NETWORK_TIMEOUT}s")
            if attempt >= _RATE_LIMIT_RETRIES or time.time() >= deadline:
                return _stale_cache_or_raise(cache_path, last_exc)
        except Exception as e:
            if not _looks_like_rate_limit(e) or attempt >= _RATE_LIMIT_RETRIES:
                return _stale_cache_or_raise(cache_path, e)
            last_exc = e
            sleep_for = min(_RATE_LIMIT_BACKOFF_BASE * (attempt + 1), max(deadline - time.time(), 0))
            if sleep_for > 0:
                time.sleep(sleep_for)
        attempt += 1

_INTERVAL_TIMEDELTAS = {
    "1m": timedelta(minutes=1), "2m": timedelta(minutes=2), "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15), "30m": timedelta(minutes=30), "60m": timedelta(minutes=60),
    "90m": timedelta(minutes=90), "1h": timedelta(hours=1), "1d": timedelta(days=1),
    "1wk": timedelta(weeks=1),
}


def _drop_unclosed_bar(hist, interval: str):
    """Descarta la ultima barra si todavia se esta formando (no ha pasado
    el tiempo completo del intervalo desde su timestamp de apertura).

    Sin esto, el volumen de una vela recien empezada (ej. 2 min dentro de
    una vela de 15m) se compara injustamente contra el promedio de velas ya
    CERRADAS, saliendo siempre artificialmente bajo -- no porque el mercado
    este tranquilo, sino porque la vela todavia no tuvo tiempo de acumular
    volumen. Las señales solo se evaluan sobre velas completas.
    """
    if hist.empty or len(hist) < 2:
        return hist
    delta = _INTERVAL_TIMEDELTAS.get(interval)
    if delta is None:
        return hist
    last_ts = hist.index[-1]
    now = datetime.now(last_ts.tzinfo) if last_ts.tzinfo is not None else datetime.now()
    if now - last_ts < delta:
        return hist.iloc[:-1]
    return hist

BB_PERIOD = 20
BB_STD = 2

# "Apretadas": el ancho de banda debe estar en el percentil <= SQUEEZE_PERCENTILE
# de su propio rango reciente (SQUEEZE_LOOKBACK barras hacia atras).
# Umbral relajado a pedido del usuario (20->30->35) para que la estrategia
# dispare señales con mas frecuencia, aceptando que seran algo menos
# exigentes/confiables que con los valores originales. Diagnostico real
# (2026-09-08, mitad de sesion): de 30 tickers en watchlist, la mayoria
# tenia el ancho de banda en percentil 55-100 (bandas YA abiertas, no
# apretadas) -- subir el umbral no fabrica squeezes que no existen, pero
# ayuda a capturar los casos limite (ej. tickers en 30-35 que antes quedaban
# justo afuera).
#
# Intento de reajuste (2026-09-09) via tune_strategy.py: el barrido standalone
# sugeria percentil<=40 como mejor (36.07% vs 11.86% total_pnl segun ESE
# script). Al verificarlo con backtest.py (motor de produccion real, mismos
# datos del dia) el resultado se invirtio: 35 dio 8.45% total_pnl / 58.8% win
# rate vs 7.09% / 58.0% con 40 -- tune_strategy.py mide P&L de forma mas
# simple/distinta a la del motor real y no es confiable por si solo para
# decidir. Revertido a 35 (el valor que de verdad funciona mejor). Para
# cualquier futuro reajuste de squeeze_breakout: no aplicar en vivo solo con
# el resultado de tune_strategy.py, siempre confirmar con backtest.py primero
# (como si hizo con exito para giro_sma20 el mismo dia, ver arriba).
SQUEEZE_LOOKBACK = 100
SQUEEZE_PERCENTILE = 35
# "Varios periodos consecutivos": minimo de barras seguidas en squeeze
# justo antes de la barra de ruptura.
SQUEEZE_MIN_BARS = 2

# "Mayor al promedio de los ultimos dias": volumen de la barra de senal
# comparado con el promedio de las VOLUME_LOOKBACK barras previas.
VOLUME_LOOKBACK = 20

# Umbral para considerar la ruptura "extrema" (volatilidad de apertura
# extrema) vs una ruptura con volumen "regular".
STRONG_VOLUME_MULT = 2.0
PROFIT_TARGET_REGULAR_PCT = 0.10   # +10% fijo sobre la prima con volatilidad regular

# Con volatilidad EXTREMA (volume_ratio >= STRONG_VOLUME_MULT), el objetivo
# no es fijo: escala entre 15% y 20% segun que tan extremo sea el volumen.
# A partir de STRONG_VOLUME_MULT el objetivo empieza en el minimo (15%) y
# sube linealmente hasta el maximo (20%) cuando el volumen llega a
# EXTREME_VOLUME_MULT_FOR_MAX_TARGET o mas.
PROFIT_TARGET_EXTREME_MIN_PCT = 0.15
PROFIT_TARGET_EXTREME_MAX_PCT = 0.20
EXTREME_VOLUME_MULT_FOR_MAX_TARGET = 4.0


def _extreme_profit_target(volume_ratio: float) -> float:
    span_mult = EXTREME_VOLUME_MULT_FOR_MAX_TARGET - STRONG_VOLUME_MULT
    progress = (volume_ratio - STRONG_VOLUME_MULT) / span_mult if span_mult else 1.0
    progress = max(0.0, min(1.0, progress))
    span_pct = PROFIT_TARGET_EXTREME_MAX_PCT - PROFIT_TARGET_EXTREME_MIN_PCT
    return PROFIT_TARGET_EXTREME_MIN_PCT + progress * span_pct

# Si el mercado se mueve en contra de la posicion, se corta la perdida sin
# esperar mas: -20% sobre la prima pagada, sin importar la fuerza de entrada.
STOP_LOSS_PCT = 0.20

# Dia trading: si a la ultima barra le quedan <= estos minutos para el
# cierre REAL de la sesion (16:00 ET normal, 13:00 ET en dia de cierre
# anticipado -- ver paper_trading.nyse_calendar), se avisa que hay que
# cerrar posiciones sin esperar a otra senal. Antes era una hora fija
# (15:45 ET) que no sabia de cierres anticipados -- corregido 2026-09-08.
EOD_FORCE_CLOSE_MINUTES_BEFORE_CLOSE = 15

# Nueva regla (2026-09-08, a pedido del usuario): no se abren posiciones
# NUEVAS si faltan <= estos minutos para el cierre de la sesion -- entrar
# tan cerca del cierre no deja tiempo real para que la operacion se
# desarrolle antes del cierre forzado de dia trading.
NO_NEW_ENTRY_MINUTES_BEFORE_CLOSE = 30


def _minutes_until_market_close(ts: datetime) -> float | None:
    """Minutos desde `ts` hasta el cierre REAL del NYSE ese dia (considera
    cierres anticipados). None si `ts` no cae en un dia de mercado."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=MARKET_TZ)
    else:
        ts = ts.astimezone(MARKET_TZ)
    day = ts.date()
    if not is_trading_day(day):
        return None
    close_dt = datetime.combine(day, market_close_time(day), tzinfo=MARKET_TZ)
    return (close_dt - ts).total_seconds() / 60


def is_near_session_close(ts: datetime, minutes_before: float = EOD_FORCE_CLOSE_MINUTES_BEFORE_CLOSE) -> bool:
    """True si a `ts` le quedan <= `minutes_before` minutos para el cierre
    real de la sesion (o si `ts` ya no cae en un dia de mercado)."""
    mins = _minutes_until_market_close(ts)
    return mins is None or mins <= minutes_before


def too_close_to_open_new_position(now: datetime | None = None) -> bool:
    """True si faltan <= NO_NEW_ENTRY_MINUTES_BEFORE_CLOSE minutos para el
    cierre de la sesion (o si hoy no es dia de mercado) -- no se deben
    abrir posiciones NUEVAS tan cerca del cierre (day trading, sin overnight)."""
    now = now or datetime.now(MARKET_TZ)
    return is_near_session_close(now, NO_NEW_ENTRY_MINUTES_BEFORE_CLOSE)

DEFAULT_INTERVAL = "15m"
DEFAULT_PERIOD = "1mo"


def _bollinger(closes):
    sma = closes.rolling(BB_PERIOD).mean()
    std = closes.rolling(BB_PERIOD).std(ddof=0)
    upper = sma + BB_STD * std
    lower = sma - BB_STD * std
    width_pct = (upper - lower) / sma * 100
    return sma, upper, lower, width_pct


def _notna(value) -> bool:
    return value == value  # NaN != NaN


# Tolerancia para comparaciones de precio: evita que un redondeo de punto
# flotante (ej. 4.65 * 1.10 = 5.115000000000001 en vez de 5.115 exacto)
# haga que una prima que SI toco el objetivo/stop no dispare el cierre por
# una diferencia de centesimas de centavo que no existe en el mercado real.
_PRICE_EPSILON = 1e-6


def compute_target_price(entry_premium: float, target_pct: float) -> float:
    """Precio de la prima al que se deberia cerrar para tomar la ganancia objetivo."""
    return entry_premium * (1 + target_pct)


def profit_target_hit(entry_premium: float, current_premium: float, target_pct: float) -> bool:
    """True si la prima actual ya alcanzo (o supero) el objetivo de ganancia."""
    return current_premium >= compute_target_price(entry_premium, target_pct) - _PRICE_EPSILON


def compute_stop_loss_price(entry_premium: float, stop_pct: float = STOP_LOSS_PCT) -> float:
    """Precio de la prima al que se debe cortar la perdida."""
    return entry_premium * (1 - stop_pct)


def stop_loss_hit(entry_premium: float, current_premium: float, stop_pct: float = STOP_LOSS_PCT) -> bool:
    """True si la prima actual ya cayo al nivel de stop-loss (o por debajo)."""
    return current_premium <= compute_stop_loss_price(entry_premium, stop_pct) + _PRICE_EPSILON


def analyze(symbol: str, interval: str = DEFAULT_INTERVAL, period: str = DEFAULT_PERIOD,
            raw_hist=None) -> dict:
    """Analiza `symbol` con Bandas de Bollinger (20,2) y devuelve la senal
    actual mas el texto de "razon" listo para el registro del trade.

    raw_hist: DataFrame ya descargado (mismo interval/period) para evitar
    una llamada de red repetida -- ver analyze_with_confirmation(), que
    reutiliza el mismo historial de 15m para el analisis Y para la
    tendencia de 15m en vez de descargarlo dos veces.
    """
    hist = raw_hist if raw_hist is not None else _fetch_history(symbol, period, interval)
    hist = hist.dropna(subset=["Close", "Volume"])
    hist = _drop_unclosed_bar(hist, interval)
    if len(hist) < BB_PERIOD + SQUEEZE_MIN_BARS + 5:
        raise ValueError(
            f"No hay suficiente historial de '{symbol}' en intervalo {interval}/{period} "
            f"para calcular Bandas de Bollinger de {BB_PERIOD} periodos."
        )

    closes = hist["Close"]
    volumes = hist["Volume"]
    sma, upper, lower, width_pct = _bollinger(closes)

    last_close = float(closes.iloc[-1])
    last_sma = float(sma.iloc[-1])
    last_upper = float(upper.iloc[-1])
    last_lower = float(lower.iloc[-1])
    last_width = float(width_pct.iloc[-1])
    last_ts = hist.index[-1]

    lookback_widths = width_pct.dropna().iloc[-(SQUEEZE_LOOKBACK + 1):-1]
    if len(lookback_widths):
        width_percentile_rank = float((lookback_widths < last_width).mean() * 100)
        typical_width = float(lookback_widths.median())
        squeeze_threshold = float(np.percentile(lookback_widths, SQUEEZE_PERCENTILE))
    else:
        width_percentile_rank, typical_width, squeeze_threshold = 50.0, last_width, last_width

    # Cuenta barras consecutivas en squeeze, justo antes de la barra actual.
    consecutive_squeeze = 0
    for w in width_pct.iloc[:-1][::-1]:
        if _notna(w) and w <= squeeze_threshold:
            consecutive_squeeze += 1
        else:
            break
    was_squeezed = consecutive_squeeze >= SQUEEZE_MIN_BARS

    # Yahoo reporta Volume=0 en casi todas las barras de premarket (aunque
    # si trae el precio). Si se incluyeran esas barras en el promedio, el
    # promedio quedaria contaminado con ceros justo despues de la apertura
    # y cualquier volumen real parecería "infinitamente alto" -> señales
    # falsas. Se descartan las barras de volumen 0 antes de promediar.
    historical_volumes = volumes.iloc[:-1]
    nonzero_volumes = historical_volumes[historical_volumes > 0]
    enough_volume_data = len(nonzero_volumes) >= VOLUME_LOOKBACK
    avg_volume = float(nonzero_volumes.iloc[-VOLUME_LOOKBACK:].mean()) if len(nonzero_volumes) > 0 else 0.0
    last_volume = float(volumes.iloc[-1])
    volume_ratio = (last_volume / avg_volume) if avg_volume else 0.0
    high_volume = enough_volume_data and last_volume > avg_volume
    strong_volume = enough_volume_data and volume_ratio >= STRONG_VOLUME_MULT

    # 2026-09-16, a pedido del usuario tras MSFT (rompio la banda por
    # apenas 0.032% y se revirtio en minutos, stop-loss -21.53%): el mismo
    # filtro de margen que ya usa analyze_forming_bar()
    # (EARLY_BREAKOUT_MARGIN_PCT, validado con backtest real -- ver la
    # nota junto a esa constante) nunca se aplico aca, en la señal
    # NORMAL de squeeze_breakout (solo en la variante "temprana"). Datos
    # reales propios: separando las rupturas por margen, las de menos de
    # 0.3% tuvieron 50% de acierto con perdidas grandes (AAPL -23.71%,
    # MSFT -21.53%, MARA -23.73%, TSLA -25.00%), las de 0.3% o mas
    # tuvieron 87.5% de acierto (unica perdida: HOOD -22.52%). Tocar
    # apenas la banda ya no alcanza -- tiene que romperla con margen real.
    breakout_up = last_close > last_upper * (1 + EARLY_BREAKOUT_MARGIN_PCT / 100)
    breakout_down = last_close < last_lower * (1 - EARLY_BREAKOUT_MARGIN_PCT / 100)
    cross_below_mid = last_close < last_sma
    cross_above_mid = last_close > last_sma

    signal = "none"
    if was_squeezed and breakout_up and high_volume:
        signal = "buy_call"
    elif was_squeezed and breakout_down and high_volume:
        signal = "buy_put"

    exit_long_signal = cross_below_mid or (breakout_down and high_volume)
    exit_short_signal = cross_above_mid or (breakout_up and high_volume)

    force_eod_exit = False
    try:
        force_eod_exit = is_near_session_close(last_ts)
    except (AttributeError, TypeError, ValueError):
        pass

    volatility_strength = "extrema" if strong_volume else "regular"
    profit_target_pct = _extreme_profit_target(volume_ratio) if strong_volume else PROFIT_TARGET_REGULAR_PCT

    band_tightness_desc = (
        f"Ancho de banda actual {last_width:.2f}% del precio (percentil {width_percentile_rank:.0f} "
        f"sobre las ultimas {len(lookback_widths)} barras de {interval}; ancho tipico reciente "
        f"~{typical_width:.2f}%). {consecutive_squeeze} barras consecutivas en squeeze antes de esta "
        f"barra ({'SI cumple' if was_squeezed else 'NO cumple'} el minimo de {SQUEEZE_MIN_BARS} "
        f"para considerarse 'apretadas varios periodos')."
    )
    if not enough_volume_data:
        volume_desc = (
            f"Volumen de la barra de senal: {last_volume:,.0f}. AVISO: no hay suficientes barras "
            f"recientes con volumen real reportado (Yahoo marca en 0 casi todo el premarket) para "
            f"calcular un promedio confiable -> no se confirma volumen alto con estos datos."
        )
    else:
        volume_desc = (
            f"Volumen de la barra de senal: {last_volume:,.0f} vs promedio de las ultimas "
            f"{VOLUME_LOOKBACK} barras con volumen real {avg_volume:,.0f} ({volume_ratio:.2f}x). "
            f"{'SI es mayor' if high_volume else 'NO es mayor'} al promedio reciente "
            f"-> volatilidad de apertura {volatility_strength} "
            f"(umbral extremo = {STRONG_VOLUME_MULT:.1f}x)."
        )
    price_desc = (
        f"Precio {last_close:.2f} vs banda superior {last_upper:.2f} / media {last_sma:.2f} / "
        f"banda inferior {last_lower:.2f}."
    )
    exit_plan_desc = (
        f"Plan de salida: tomar ganancia al +{profit_target_pct*100:.0f}% sobre la prima pagada "
        f"(volatilidad {volatility_strength}); cortar perdida al -{STOP_LOSS_PCT*100:.0f}% sobre la "
        f"prima si el mercado va en contra; o cerrar antes si aparece la senal tecnica de salida "
        f"contraria, o al llegar el cierre de sesion (day trading, no overnight). Se cierra por lo "
        f"que ocurra primero."
    )

    reason = (
        f"[Bollinger {BB_PERIOD},{BB_STD} | {interval} | {last_ts:%Y-%m-%d %H:%M}] "
        f"{band_tightness_desc} {volume_desc} {price_desc} {exit_plan_desc}"
    )

    return {
        "symbol": symbol,
        "interval": interval,
        "timestamp": last_ts.isoformat(),
        "signal": signal,
        "reason": reason,
        "band_tightness_explanation": band_tightness_desc,
        "volume_explanation": volume_desc,
        "exit_plan_explanation": exit_plan_desc,
        "last_close": last_close,
        "sma": last_sma,
        "upper_band": last_upper,
        "lower_band": last_lower,
        "band_width_pct": last_width,
        "band_width_percentile": width_percentile_rank,
        "consecutive_squeeze_bars": consecutive_squeeze,
        "was_squeezed": was_squeezed,
        "volume": last_volume,
        "avg_volume": avg_volume,
        "volume_ratio": volume_ratio,
        "high_volume": high_volume,
        "enough_volume_data": enough_volume_data,
        "strong_volume": strong_volume,
        "volatility_strength": volatility_strength,
        "profit_target_pct": profit_target_pct,
        "stop_loss_pct": STOP_LOSS_PCT,
        "exit_long_signal": exit_long_signal,
        "exit_short_signal": exit_short_signal,
        "force_eod_exit": force_eod_exit,
        "strategy": "squeeze_breakout",
    }


# ---------------------------------------------------------------------------
# Entrada TEMPRANA sobre la vela EN FORMACION (a pedido del usuario,
# 2026-09-09): evalua squeeze_breakout sobre la ultima vela ANTES de que
# cierre, para entrar mas rapido y capturar mas movimiento a favor.
#
# RIESGO explicito -- por eso es una funcion SEPARADA que NO reemplaza
# analyze(): operar sobre velas sin cerrar fue identificado como un BUG real
# y corregido el 2026-09-08 (ver _drop_unclosed_bar arriba: una vela que
# recien empieza puede revertir antes de cerrar, y su volumen parcial no es
# comparable al de una vela completa). Esta funcion reintroduce la entrada
# temprana pero con DOS salvaguardas que la version con bug no tenia:
#   1. Margen de ruptura (EARLY_BREAKOUT_MARGIN_PCT): no basta tocar la
#      banda por un tick -- exige que el precio ya este claramente mas alla,
#      para filtrar ruido intrabar que revierte en los minutos siguientes.
#   2. Volumen PROYECTADO (pace): el volumen parcial de una vela a medio
#      formar se proyecta linealmente segun la fraccion de tiempo
#      transcurrido antes de compararlo contra el promedio de velas
#      completas -- comparar volumen crudo parcial vs promedio completo
#      sesgaria siempre hacia abajo (por eso _drop_unclosed_bar existe).
#   3. Ignora el primer EARLY_MIN_BAR_ELAPSED_PCT de la vela -- con muy poco
#      tiempo transcurrido, la proyeccion de volumen es demasiado ruidosa
#      para confiar en ella.
#
# Solo se usa para ENTRADAS nuevas (scan_all_signals, watchlist_scan.py) --
# las SALIDAS (position_monitor.py, evaluate_open_position_exit) siguen
# usando exclusivamente analyze() sobre velas YA CERRADAS, sin cambios:
# cerrar una posicion por una reversion que despues no se confirma es mucho
# menos grave (se puede volver a entrar) que abrir una por ella.
#
# VALIDACION HISTORICA (2026-09-09, a pedido del usuario -- "buscale una
# logica de entradas... estudiala"): se simulo esta funcion minuto a minuto
# con datos REALES de 1m de yfinance (ultimos ~7 dias, limite del
# proveedor -- unos 90-138 eventos segun el umbral, muestra chica) sobre
# toda la watchlist, comparando el precio en el momento del disparo
# temprano contra el precio de cierre REAL de esa misma vela de 15m.
#
# Resultado HONESTO: con los umbrales originales (margen 0.15%, elapsed
# >=20%, volumen proyectado >1.0x) el 74.6% de los disparos tempranos se
# confirmaron al cierre (la ruptura se sostuvo), pero el valor esperado del
# movimiento adicional capturado (temprano -> cierre) fue LIGERAMENTE
# NEGATIVO (-0.052%): lo que se pierde en el 25.4% que revierte (-0.68%
# promedio) supera lo que se gana en las que se confirman (+0.161%
# promedio). Se probaron variantes mas estrictas -- la mejor combinacion
# (margen 0.30% + volumen proyectado >1.3x) sube la confirmacion a 83% y
# deja el valor esperado en -0.006% (practicamente cero, ya no negativo,
# pero tampoco una ganancia comprobada). NINGUNA configuracion probada dio
# ventaja positiva clara sobre simplemente esperar al cierre de la vela.
#
# Conclusion aplicada: se sube el umbral a la mejor combinacion encontrada
# (no a la original) porque reduce los falsos positivos sin costo medible,
# pero esta funcion NO debe presentarse como "gana mas por entrar antes" --
# su valor real es reducir el retraso de reaccion (ver el caso SPY del
# 2026-09-09 que motivo auto_entry.py), no fabricar edge direccional extra
# dentro de la misma vela. Muestra de 7 dias -- revalidar si el
# comportamiento del mercado cambia mucho o cuando yfinance permita mas
# historial de 1m.
EARLY_BREAKOUT_MARGIN_PCT = 0.30   # el precio debe superar la banda por >= 0.30% adicional, no solo tocarla
EARLY_MIN_BAR_ELAPSED_PCT = 0.20   # ignora el primer 20% de la vela -- el volumen proyectado no es confiable todavia
EARLY_VOLUME_MULT = 1.3            # volumen proyectado debe superar 1.3x el promedio, no solo 1.0x


def analyze_forming_bar(symbol: str, interval: str = DEFAULT_INTERVAL, period: str = DEFAULT_PERIOD) -> dict | None:
    """Squeeze_breakout evaluado sobre la vela EN FORMACION (todavia no
    cerrada) en vez de esperar a que cierre -- ver nota de riesgo arriba.

    Devuelve None (sin señal) si: la vela ya cerro (usar analyze() normal),
    es demasiado nueva para proyectar volumen con confianza, no hay squeeze
    previo confirmado con velas cerradas, o el precio parcial no rompe la
    banda con margen suficiente."""
    delta = _INTERVAL_TIMEDELTAS.get(interval)
    if delta is None:
        return None

    hist = _fetch_history(symbol, period, interval)
    hist = hist.dropna(subset=["Close", "Volume"])
    if len(hist) < BB_PERIOD + SQUEEZE_MIN_BARS + 6:
        return None

    last_ts = hist.index[-1]
    now = datetime.now(last_ts.tzinfo) if last_ts.tzinfo is not None else datetime.now()
    elapsed_seconds = (now - last_ts).total_seconds()
    bar_seconds = delta.total_seconds()
    elapsed_frac = elapsed_seconds / bar_seconds if bar_seconds else 1.0
    if elapsed_frac >= 1.0 or elapsed_frac < EARLY_MIN_BAR_ELAPSED_PCT:
        return None

    closed = hist.iloc[:-1]
    forming = hist.iloc[-1]

    closes = closed["Close"]
    sma, upper, lower, width_pct = _bollinger(closes)
    if len(sma.dropna()) == 0:
        return None
    last_sma = float(sma.iloc[-1])
    last_upper = float(upper.iloc[-1])
    last_lower = float(lower.iloc[-1])

    lookback_widths = width_pct.dropna().iloc[-SQUEEZE_LOOKBACK:]
    if len(lookback_widths) == 0:
        return None
    squeeze_threshold = float(np.percentile(lookback_widths, SQUEEZE_PERCENTILE))
    consecutive_squeeze = 0
    for w in width_pct[::-1]:
        if _notna(w) and w <= squeeze_threshold:
            consecutive_squeeze += 1
        else:
            break
    was_squeezed = consecutive_squeeze >= SQUEEZE_MIN_BARS
    if not was_squeezed:
        return None

    forming_price = float(forming["Close"])
    breakout_up = forming_price > last_upper * (1 + EARLY_BREAKOUT_MARGIN_PCT / 100)
    breakout_down = forming_price < last_lower * (1 - EARLY_BREAKOUT_MARGIN_PCT / 100)
    if not (breakout_up or breakout_down):
        return None

    historical_volumes = closed["Volume"]
    nonzero_volumes = historical_volumes[historical_volumes > 0]
    enough_volume_data = len(nonzero_volumes) >= VOLUME_LOOKBACK
    avg_volume = float(nonzero_volumes.iloc[-VOLUME_LOOKBACK:].mean()) if len(nonzero_volumes) > 0 else 0.0
    partial_volume = float(forming["Volume"])
    projected_volume = partial_volume / elapsed_frac
    volume_ratio = (projected_volume / avg_volume) if avg_volume else 0.0
    high_volume = enough_volume_data and volume_ratio > EARLY_VOLUME_MULT
    if not high_volume:
        return None

    signal = "buy_call" if breakout_up else "buy_put"
    strong_volume = enough_volume_data and volume_ratio >= STRONG_VOLUME_MULT
    volatility_strength = "extrema" if strong_volume else "regular"
    profit_target_pct = _extreme_profit_target(volume_ratio) if strong_volume else PROFIT_TARGET_REGULAR_PCT
    closes_at = last_ts + delta

    band_tightness_desc = (
        f"Squeeze previo confirmado con velas YA CERRADAS: {consecutive_squeeze} barras consecutivas "
        f"(cumple minimo {SQUEEZE_MIN_BARS}, umbral percentil {SQUEEZE_PERCENTILE})."
    )
    volume_desc = (
        f"Volumen parcial {partial_volume:,.0f} con {elapsed_frac*100:.0f}% de la vela transcurrida -> "
        f"proyectado a vela completa {projected_volume:,.0f} vs promedio {avg_volume:,.0f} "
        f"({volume_ratio:.2f}x) -> volatilidad {volatility_strength}."
    )
    exit_plan_desc = (
        f"Plan de salida: objetivo +{profit_target_pct*100:.0f}% sobre la prima, stop -{STOP_LOSS_PCT*100:.0f}%, "
        f"señal tecnica contraria, o cierre de sesion -- lo que ocurra primero."
    )
    reason = (
        f"[Bollinger {BB_PERIOD},{BB_STD} | {interval} ENTRADA TEMPRANA sobre vela EN FORMACION "
        f"({elapsed_frac*100:.0f}% transcurrida, cierra ~{closes_at:%H:%M}) | {last_ts:%Y-%m-%d %H:%M}] "
        f"{band_tightness_desc} Precio parcial {forming_price:.2f} ya supera banda "
        f"{'superior' if breakout_up else 'inferior'} ({last_upper if breakout_up else last_lower:.2f}) "
        f"con margen >= {EARLY_BREAKOUT_MARGIN_PCT}% (filtra ruido de ruptura marginal que podria revertir). "
        f"{volume_desc} RIESGO: la vela todavia NO cerro -- puede revertir antes de {closes_at:%H:%M} y "
        f"esta señal deshacerse; por eso NO se usa para evaluar salidas, solo para adelantar entradas. "
        f"{exit_plan_desc}"
    )

    return {
        "symbol": symbol,
        "interval": interval,
        "timestamp": last_ts.isoformat(),
        "signal": signal,
        "reason": reason,
        "band_tightness_explanation": band_tightness_desc,
        "volume_explanation": volume_desc,
        "exit_plan_explanation": exit_plan_desc,
        "last_close": forming_price,
        "sma": last_sma,
        "upper_band": last_upper,
        "lower_band": last_lower,
        "consecutive_squeeze_bars": consecutive_squeeze,
        "was_squeezed": was_squeezed,
        "volume": partial_volume,
        "avg_volume": avg_volume,
        "volume_ratio": volume_ratio,
        "high_volume": high_volume,
        "enough_volume_data": enough_volume_data,
        "strong_volume": strong_volume,
        "volatility_strength": volatility_strength,
        "profit_target_pct": profit_target_pct,
        "stop_loss_pct": STOP_LOSS_PCT,
        "exit_long_signal": False,
        "exit_short_signal": False,
        "force_eod_exit": False,
        "strategy": "squeeze_breakout_temprano",
        "bar_elapsed_pct": round(elapsed_frac * 100, 1),
    }


# ---------------------------------------------------------------------------
# Confirmacion multi-temporalidad: antes de tomar una señal de 15m, se revisa
# si la temporalidad de 1h la respalda o la contradice. Operar a favor de la
# tendencia de 1h reduce falsas rupturas (un error comun: comprar un CALL de
# 15m justo cuando el activo esta en tendencia bajista de fondo en 1h).
#
# Por defecto, para day trading, SOLO se usan 15m (entrada) y 1h
# (confirmacion) -- nada de 30m/dia/semana. Es deliberado: en day trading
# las temporalidades mas largas no son relevantes para la decision de
# entrada/salida del mismo dia, y consultarlas de mas solo hace mas lento
# el analisis sin aportar a la decision.
# ---------------------------------------------------------------------------

MTF_TIMEFRAMES = [
    ("15m", "5d"),
    ("1h", "3mo"),
]
MTF_TREND_FAST = 20
MTF_TREND_SLOW = 50


def get_trend(symbol: str, interval: str, period: str, raw_hist=None) -> str | None:
    """Tendencia simple (alcista/bajista/lateral) de un simbolo en una
    temporalidad, via SMA rapida vs SMA lenta. None si no hay suficiente
    historial en esa temporalidad.

    raw_hist: DataFrame ya descargado para este symbol/interval, para
    evitar una llamada de red repetida (ver analyze_with_confirmation).
    Solo importa el intervalo -- un period mas largo que el usado por
    defecto para esta temporalidad sirve igual, porque la tendencia solo
    depende de las ultimas barras (rolling mean ancladas al final)."""
    try:
        hist = raw_hist if raw_hist is not None else _fetch_history(symbol, period, interval)
        hist = _drop_unclosed_bar(hist, interval)
        closes = hist["Close"].dropna()
    except Exception:
        return None
    if len(closes) < MTF_TREND_SLOW + 5:
        return None

    last = float(closes.iloc[-1])
    sma_fast = float(closes.rolling(MTF_TREND_FAST).mean().iloc[-1])
    sma_slow = float(closes.rolling(MTF_TREND_SLOW).mean().iloc[-1])

    if last > sma_fast > sma_slow:
        return "alcista"
    if last < sma_fast < sma_slow:
        return "bajista"
    return "lateral"


def multi_timeframe_trends(symbol: str, raw_hist_by_interval: dict | None = None) -> dict:
    """Tendencia del simbolo en 15m y 1h (day trading: por defecto no se
    consultan temporalidades mayores).

    raw_hist_by_interval: {interval: DataFrame} ya descargado para
    reutilizar en vez de pedirlo de nuevo por red (ver analyze_with_confirmation)."""
    raw_hist_by_interval = raw_hist_by_interval or {}
    return {
        interval: get_trend(symbol, interval, period, raw_hist=raw_hist_by_interval.get(interval))
        for interval, period in MTF_TIMEFRAMES
    }


def confirmation_for_signal(signal: str, trends: dict) -> dict:
    """Compara la señal de entrada (buy_call/buy_put) contra la tendencia de
    1h (el unico respaldo usado por defecto para day trading -- ver nota en
    MTF_TIMEFRAMES) y devuelve un nivel de confianza:

      alta  -> 1h va en la misma direccion que la señal
      media -> 1h esta lateral o sin dato suficiente
      baja  -> 1h va en contra de la señal

    No bloquea la señal por si solo -- es informacion para decidir con mas
    cuidado (o reducir tamaño) cuando la confianza es baja.
    """
    if signal not in ("buy_call", "buy_put"):
        return {"confidence": "n/a", "aligned": [], "against": [], "neutral": []}

    wanted = "alcista" if signal == "buy_call" else "bajista"
    opposite = "bajista" if signal == "buy_call" else "alcista"

    backing_timeframes = ["1h"]
    aligned, against, neutral = [], [], []
    for tf in backing_timeframes:
        trend = trends.get(tf)
        if trend == wanted:
            aligned.append(tf)
        elif trend == opposite:
            against.append(tf)
        else:
            neutral.append(tf)

    if against:
        confidence = "baja"
    elif aligned:
        confidence = "alta"
    else:
        confidence = "media"

    return {"confidence": confidence, "aligned": aligned, "against": against, "neutral": neutral}


def analyze_with_confirmation(symbol: str, interval: str = DEFAULT_INTERVAL, period: str = DEFAULT_PERIOD) -> dict:
    """analyze() + confirmacion multi-temporalidad. Uso recomendado para
    decidir entradas reales -- analyze() a secas sigue disponible para
    escaneos rapidos o pruebas donde no hace falta el contexto de tendencia.

    Eficiencia: cuando `interval` es la misma temporalidad que la primera
    entrada de MTF_TIMEFRAMES (15m, el caso normal), el historial ya
    descargado para el analisis de Bollinger se reutiliza para calcular
    tambien la tendencia de 15m, evitando pedirlo dos veces por red."""
    raw_hist = _fetch_history(symbol, period, interval)
    result = analyze(symbol, interval=interval, period=period, raw_hist=raw_hist)
    raw_hist_by_interval = {interval: raw_hist} if interval == MTF_TIMEFRAMES[0][0] else {}
    trends = multi_timeframe_trends(symbol, raw_hist_by_interval=raw_hist_by_interval)
    confirmation = confirmation_for_signal(result["signal"], trends)
    result["mtf_trends"] = trends
    result["confidence"] = confirmation["confidence"]
    result["confidence_detail"] = confirmation
    return result


# ---------------------------------------------------------------------------
# Estrategia 2: reversion del gap de apertura (9:30 ET). A diferencia de la
# estrategia de squeeze (que opera A FAVOR de una ruptura tras compresion),
# esta opera EN CONTRA de una vela de apertura que amanece MUY lejos de una
# banda, apostando a que el precio revierte hacia la media en vez de seguir
# extendiendose. Regla del usuario:
#
#   - Si la primera vela de 15m de la sesion regular (9:30-9:45 ET) es
#     alcista ("vela call") y cierra muy por encima de la banda superior
#     -> se compra un PUT (fade a la baja).
#   - Si esa vela es bajista ("vela put") y cierra muy por debajo de la
#     banda inferior -> se compra un CALL (fade al alza).
#   - El plan de salida es EXACTAMENTE el mismo que ya existe por defecto
#     (evaluate_open_position_exit, sin cambios): objetivo/stop segun
#     volumen, proteccion tecnica, toma rapida, cierre de sesion.
#
# Solo puede disparar una vez por dia por ticker, porque solo evalua la
# barra cuyo timestamp es exactamente las 9:30 ET (la barra de apertura ya
# cerrada) -- fuera de esa barra, signal siempre es "none".
# ---------------------------------------------------------------------------

MARKET_OPEN_BAR_TIME = dtime(9, 30)

# "Muy alejada": el cierre debe quedar mas alla de la banda por al menos
# esta fraccion del propio semiancho de banda (upper-sma, o sma-lower).
# Relajado 0.5->0.35 (2026-09-08) a pedido del usuario para tener mas
# aperturas -- un gap de apertura ya no necesita llegar a ~3 desviaciones
# estandar de la media, con ~2.7 (2 de la banda + 0.35 extra) alcanza.
GAP_FADE_MIN_EXTENSION_RATIO = 0.35

# PAUSADA (2026-09-08): backtest de 60 dias / 10 tickers mostro win rate
# 44.9% (peor que el azar) y P&L direccional total -12.38%, mientras
# squeeze_breakout y giro_sma20 dieron resultados positivos -- ver
# backtest.py. La deteccion sigue corriendo (para diagnostico/reportes en
# bollinger_scan.py) pero signal siempre sale "none" mientras este en False,
# asi nunca se auto-ejecuta. Cambiar a True para reactivarla.
GAP_FADE_ENABLED = False


def detect_opening_gap_fade(symbol: str, interval: str = DEFAULT_INTERVAL, period: str = DEFAULT_PERIOD,
                             raw_hist=None) -> dict:
    """Detecta un gap de apertura extremo (vela de 9:30 ET) para tomar una
    entrada CONTRARIA (fade), apostando a reversion hacia la banda en vez
    de continuacion. Devuelve un dict con la misma forma que analyze() para
    poder pasar por el mismo pipeline de confirmacion/seleccion de contrato.

    signal = "none" si la ultima barra cerrada no es la de apertura de hoy
    (9:30 ET), o si no quedo lo bastante lejos de la banda.
    """
    hist = raw_hist if raw_hist is not None else _fetch_history(symbol, period, interval)
    hist = hist.dropna(subset=["Open", "Close", "Volume"])
    hist = _drop_unclosed_bar(hist, interval)
    if len(hist) < BB_PERIOD + 5:
        raise ValueError(
            f"No hay suficiente historial de '{symbol}' en intervalo {interval}/{period} "
            f"para calcular Bandas de Bollinger de {BB_PERIOD} periodos."
        )

    closes = hist["Close"]
    volumes = hist["Volume"]
    sma, upper, lower, width_pct = _bollinger(closes)

    last_ts = hist.index[-1]
    is_opening_bar = last_ts.time() == MARKET_OPEN_BAR_TIME

    last_open = float(hist["Open"].iloc[-1])
    last_close = float(closes.iloc[-1])
    last_sma = float(sma.iloc[-1])
    last_upper = float(upper.iloc[-1])
    last_lower = float(lower.iloc[-1])
    last_width = float(width_pct.iloc[-1])

    band_half_width_up = last_upper - last_sma
    band_half_width_down = last_sma - last_lower
    extension_above = last_close - last_upper
    extension_below = last_lower - last_close
    extension_ratio_up = (extension_above / band_half_width_up) if band_half_width_up > 0 else 0.0
    extension_ratio_down = (extension_below / band_half_width_down) if band_half_width_down > 0 else 0.0

    bullish_candle = last_close > last_open
    bearish_candle = last_close < last_open
    far_above = extension_above > 0 and extension_ratio_up >= GAP_FADE_MIN_EXTENSION_RATIO
    far_below = extension_below > 0 and extension_ratio_down >= GAP_FADE_MIN_EXTENSION_RATIO

    signal = "none"
    if is_opening_bar and bullish_candle and far_above:
        signal = "buy_put"
    elif is_opening_bar and bearish_candle and far_below:
        signal = "buy_call"
    detected_signal = signal
    if not GAP_FADE_ENABLED:
        signal = "none"

    # Mismo tratamiento de volumen que analyze(), para fijar el objetivo de
    # ganancia con el mismo criterio que ya se usa por defecto.
    historical_volumes = volumes.iloc[:-1]
    nonzero_volumes = historical_volumes[historical_volumes > 0]
    enough_volume_data = len(nonzero_volumes) >= VOLUME_LOOKBACK
    avg_volume = float(nonzero_volumes.iloc[-VOLUME_LOOKBACK:].mean()) if len(nonzero_volumes) > 0 else 0.0
    last_volume = float(volumes.iloc[-1])
    volume_ratio = (last_volume / avg_volume) if avg_volume else 0.0
    strong_volume = enough_volume_data and volume_ratio >= STRONG_VOLUME_MULT
    volatility_strength = "extrema" if strong_volume else "regular"
    profit_target_pct = _extreme_profit_target(volume_ratio) if strong_volume else PROFIT_TARGET_REGULAR_PCT

    force_eod_exit = False
    try:
        force_eod_exit = is_near_session_close(last_ts)
    except (AttributeError, TypeError, ValueError):
        pass

    if extension_above > 0:
        extension_desc = f"extension {extension_ratio_up*100:.0f}% del semiancho de banda mas alla de la banda superior"
    elif extension_below > 0:
        extension_desc = f"extension {extension_ratio_down*100:.0f}% del semiancho de banda mas alla de la banda inferior"
    else:
        extension_desc = "sin quedar fuera de ninguna banda"
    gap_desc = (
        f"Vela de apertura (9:30 ET) {'ALCISTA' if bullish_candle else 'BAJISTA' if bearish_candle else 'neutra'} "
        f"({'es' if is_opening_bar else 'NO es'} la barra de apertura de hoy): abrio {last_open:.2f}, "
        f"cerro {last_close:.2f} -- banda superior {last_upper:.2f} / media {last_sma:.2f} / banda inferior "
        f"{last_lower:.2f} ({extension_desc}; umbral para 'muy alejada' = "
        f"{GAP_FADE_MIN_EXTENSION_RATIO*100:.0f}% del semiancho de banda)."
    )
    volume_desc = (
        f"Volumen de la vela de apertura: {last_volume:,.0f} vs promedio de las ultimas {VOLUME_LOOKBACK} "
        f"barras con volumen real {avg_volume:,.0f} ({volume_ratio:.2f}x) -> volatilidad {volatility_strength}."
        if enough_volume_data else
        f"Volumen de la vela de apertura: {last_volume:,.0f}. AVISO: no hay suficiente historial de volumen "
        f"real para comparar -> se usa el objetivo de ganancia regular por defecto."
    )
    exit_plan_desc = (
        f"Plan de salida: tomar ganancia al +{profit_target_pct*100:.0f}% sobre la prima pagada "
        f"(volatilidad {volatility_strength}); cortar perdida al -{STOP_LOSS_PCT*100:.0f}%; o cerrar antes "
        f"por senal tecnica de reversion o cierre de sesion -- mismas reglas de salida que la estrategia de squeeze."
    )
    paused_note = (
        f" [ESTRATEGIA PAUSADA -- hubiera dado {detected_signal.upper()} pero gap_fade_apertura esta "
        f"desactivada (GAP_FADE_ENABLED=False) tras backtest con win rate <50%, ver backtest.py]"
        if not GAP_FADE_ENABLED and detected_signal != "none" else ""
    )
    reason = (
        f"[Reversion de gap de apertura {BB_PERIOD},{BB_STD} | {interval} | {last_ts:%Y-%m-%d %H:%M}] "
        f"Estrategia CONTRARIA (fade), no de continuacion: {gap_desc} {volume_desc} {exit_plan_desc}{paused_note}"
    )

    return {
        "symbol": symbol,
        "strategy": "gap_fade_apertura",
        "interval": interval,
        "timestamp": last_ts.isoformat(),
        "signal": signal,
        "reason": reason,
        "band_tightness_explanation": gap_desc,
        "volume_explanation": volume_desc,
        "exit_plan_explanation": exit_plan_desc,
        "last_close": last_close,
        "sma": last_sma,
        "upper_band": last_upper,
        "lower_band": last_lower,
        "band_width_pct": last_width,
        "band_width_percentile": None,
        "consecutive_squeeze_bars": None,
        "was_squeezed": None,
        "volume": last_volume,
        "avg_volume": avg_volume,
        "volume_ratio": volume_ratio,
        "high_volume": None,
        "enough_volume_data": enough_volume_data,
        "strong_volume": strong_volume,
        "volatility_strength": volatility_strength,
        "profit_target_pct": profit_target_pct,
        "stop_loss_pct": STOP_LOSS_PCT,
        "exit_long_signal": False,
        "exit_short_signal": False,
        "force_eod_exit": force_eod_exit,
        "is_opening_bar": is_opening_bar,
        "extension_ratio_up": extension_ratio_up,
        "extension_ratio_down": extension_ratio_down,
    }


# ---------------------------------------------------------------------------
# Estrategia nueva (en estudio, 2026-09-10, a pedido del usuario): Opening
# Range Breakout (ORB) -- CONTINUACION del movimiento de apertura, al
# reves de gap_fade_apertura (que apuesta a reversion y ya se probo que
# pierde, ver GAP_FADE_ENABLED). Hipotesis: en vez de pelear contra la
# vela de apertura, operar A FAVOR si el precio rompe el rango que dejo
# esa primera vela (9:30-9:45 ET), con volumen que confirme.
#
# PAUSADA por defecto (igual que gap_fade_apertura, rsi_reversal,
# vwap_cross) hasta validar con backtest.py -- NUNCA se auto-ejecuta
# mientras ORB_ENABLED sea False, sin importar lo que reporte scan_all_signals.
#
# RESULTADO REAL (2026-09-10, backtest.py, 60 dias/10 tickers, motor real
# no una reimplementacion aparte -- ver el chat, un script standalone dio
# numeros distintos e inconfiables, se descarto):
#   margen 10% del rango: n=127, win_rate=38.6%, P&L total -26.14%
#   margen 35% del rango: n=97,  win_rate=41.2%, P&L total -20.86%
# Peor que gap_fade_apertura (44.9%/-12.38%) en ambas configuraciones --
# la hipotesis de continuacion (operar A FAVOR de la ruptura de apertura)
# tampoco funciona en este watchlist/ventana de 60 dias, ni relajando el
# margen. NO reactivar sin volver a probar con datos/ventana distintos
# primero -- este resultado ya esta confirmado, no hace falta repetirlo.
# ---------------------------------------------------------------------------

ORB_ENABLED = False  # NO activar -- ver resultado real arriba, perdio en las 2 configuraciones probadas

# El precio debe romper el rango de apertura por al menos este % del propio
# rango (no solo tocarlo) -- mismo principio que EARLY_BREAKOUT_MARGIN_PCT
# en analyze_forming_bar: filtra rupturas marginales que probablemente son
# ruido intrabar, no una continuacion real.
ORB_MIN_BREAKOUT_PCT_OF_RANGE = 0.10


def detect_opening_range_breakout(symbol: str, interval: str = DEFAULT_INTERVAL, period: str = DEFAULT_PERIOD,
                                   raw_hist=None) -> dict:
    """Detecta una ruptura del rango que dejo la vela de apertura (9:30 ET)
    en una vela POSTERIOR del mismo dia, con volumen que confirme -- apuesta
    a CONTINUACION del movimiento de apertura (a favor, no en contra como
    gap_fade_apertura). Devuelve un dict con la misma forma que analyze().

    signal = "none" si la ultima barra cerrada ES la de apertura (necesita
    al menos una vela despues para poder romper su rango), si no se
    encuentra la vela de apertura de hoy dentro del historial descargado, o
    si el precio no rompio el rango con margen y volumen suficientes.
    """
    hist = raw_hist if raw_hist is not None else _fetch_history(symbol, period, interval)
    hist = hist.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    hist = _drop_unclosed_bar(hist, interval)
    if len(hist) < BB_PERIOD + 5:
        raise ValueError(
            f"No hay suficiente historial de '{symbol}' en intervalo {interval}/{period} "
            f"para calcular el rango de apertura."
        )

    last_ts = hist.index[-1]
    last_close = float(hist["Close"].iloc[-1])
    last_volume = float(hist["Volume"].iloc[-1])

    signal = "none"
    detected_signal = "none"
    opening_high = opening_low = None
    breakout_pct_of_range = 0.0
    volume_ratio = 0.0
    enough_volume_data = False
    high_volume = False

    is_opening_bar = last_ts.time() == MARKET_OPEN_BAR_TIME
    today_bars = hist[hist.index.date == last_ts.date()]
    opening_rows = today_bars[today_bars.index.time == MARKET_OPEN_BAR_TIME]

    if not is_opening_bar and len(opening_rows) == 1:
        opening_high = float(opening_rows["High"].iloc[0])
        opening_low = float(opening_rows["Low"].iloc[0])
        opening_range = opening_high - opening_low

        if opening_range > 0:
            breakout_above = (last_close - opening_high) / opening_range
            breakout_below = (opening_low - last_close) / opening_range

            historical_volumes = hist["Volume"].iloc[:-1]
            nonzero_volumes = historical_volumes[historical_volumes > 0]
            enough_volume_data = len(nonzero_volumes) >= VOLUME_LOOKBACK
            avg_volume = float(nonzero_volumes.iloc[-VOLUME_LOOKBACK:].mean()) if len(nonzero_volumes) > 0 else 0.0
            volume_ratio = (last_volume / avg_volume) if avg_volume else 0.0
            high_volume = enough_volume_data and last_volume > avg_volume

            if breakout_above >= ORB_MIN_BREAKOUT_PCT_OF_RANGE and high_volume:
                detected_signal = "buy_call"
                breakout_pct_of_range = breakout_above
            elif breakout_below >= ORB_MIN_BREAKOUT_PCT_OF_RANGE and high_volume:
                detected_signal = "buy_put"
                breakout_pct_of_range = breakout_below

    signal = detected_signal if ORB_ENABLED else "none"

    strong_volume = enough_volume_data and volume_ratio >= STRONG_VOLUME_MULT
    volatility_strength = "extrema" if strong_volume else "regular"
    profit_target_pct = _extreme_profit_target(volume_ratio) if strong_volume else PROFIT_TARGET_REGULAR_PCT

    force_eod_exit = False
    try:
        force_eod_exit = is_near_session_close(last_ts)
    except (AttributeError, TypeError, ValueError):
        pass

    if opening_high is not None:
        range_desc = (
            f"Rango de apertura (9:30-9:45 ET): {opening_low:.2f} - {opening_high:.2f}. "
            f"Precio actual {last_close:.2f} -> ruptura {breakout_pct_of_range*100:.0f}% del rango "
            f"({'arriba' if detected_signal == 'buy_call' else 'abajo' if detected_signal == 'buy_put' else 'sin romper con margen suficiente'}, "
            f"umbral {ORB_MIN_BREAKOUT_PCT_OF_RANGE*100:.0f}%)."
        )
    else:
        range_desc = "No se encontro la vela de apertura de hoy en el historial descargado -- sin dato de rango."
    volume_desc = (
        f"Volumen de la barra: {last_volume:,.0f} vs promedio de las ultimas {VOLUME_LOOKBACK} barras con "
        f"volumen real ({volume_ratio:.2f}x) -> volatilidad {volatility_strength}."
        if enough_volume_data else
        "AVISO: no hay suficiente historial de volumen real para confirmar la ruptura."
    )
    exit_plan_desc = (
        f"Plan de salida: tomar ganancia al +{profit_target_pct*100:.0f}% sobre la prima pagada "
        f"(volatilidad {volatility_strength}); cortar perdida al -{STOP_LOSS_PCT*100:.0f}%; o cerrar antes "
        f"por senal tecnica de reversion o cierre de sesion -- mismas reglas de salida que las otras estrategias."
    )
    paused_note = (
        f" [ESTRATEGIA EN ESTUDIO -- hubiera dado {detected_signal.upper()} pero opening_range_breakout "
        f"esta desactivada (ORB_ENABLED=False) hasta validar con backtest.py, ver bollinger_strategy.py]"
        if not ORB_ENABLED and detected_signal != "none" else ""
    )
    reason = (
        f"[Opening Range Breakout {interval} | {last_ts:%Y-%m-%d %H:%M}] "
        f"Estrategia de CONTINUACION (a favor de la ruptura, no en contra): {range_desc} {volume_desc} "
        f"{exit_plan_desc}{paused_note}"
    )

    return {
        "symbol": symbol,
        "strategy": "opening_range_breakout",
        "interval": interval,
        "timestamp": last_ts.isoformat(),
        "signal": signal,
        "reason": reason,
        "band_tightness_explanation": range_desc,
        "volume_explanation": volume_desc,
        "exit_plan_explanation": exit_plan_desc,
        "last_close": last_close,
        "sma": None,
        "upper_band": opening_high,
        "lower_band": opening_low,
        "band_width_pct": None,
        "band_width_percentile": None,
        "consecutive_squeeze_bars": None,
        "was_squeezed": None,
        "volume": last_volume,
        "avg_volume": avg_volume if enough_volume_data else 0.0,
        "volume_ratio": volume_ratio,
        "high_volume": high_volume,
        "enough_volume_data": enough_volume_data,
        "strong_volume": strong_volume,
        "volatility_strength": volatility_strength,
        "profit_target_pct": profit_target_pct,
        "stop_loss_pct": STOP_LOSS_PCT,
        "exit_long_signal": False,
        "exit_short_signal": False,
        "force_eod_exit": force_eod_exit,
        "is_opening_bar": is_opening_bar,
        "breakout_pct_of_range": breakout_pct_of_range,
    }


# ---------------------------------------------------------------------------
# Estrategia nueva (en estudio, 2026-09-10, a pedido del usuario -- "estudia
# a los grandes traders"): Volatility Breakout de Larry Williams. Distinta
# de opening_range_breakout (que ya se probo y fallo): el nivel de entrada
# no se calcula con el rango de la PROPIA vela de apertura de hoy, sino con
# el rango del DIA ANTERIOR completo -- formula clasica:
#
#   nivel_largo  = apertura_de_hoy + k * (maximo_de_ayer - minimo_de_ayer)
#   nivel_corto  = apertura_de_hoy - k * (maximo_de_ayer - minimo_de_ayer)
#
# Con esta con la que Larry Williams gano el World Cup Trading
# Championship de 1987 (convirtio $10,000 en $1.1M). k tipicamente 0.25-1.0;
# se prueban varios valores en el backtest antes de fijar uno.
#
# PAUSADA por defecto hasta validar con backtest.py -- mismo patron que
# opening_range_breakout / gap_fade_apertura / rsi_reversal / vwap_cross.
# ---------------------------------------------------------------------------

VOLATILITY_BREAKOUT_ENABLED = False  # NO activar sin validar con backtest.py primero
VOLATILITY_BREAKOUT_K = 0.5  # multiplicador clasico de Larry Williams


def detect_volatility_breakout(symbol: str, interval: str = DEFAULT_INTERVAL, period: str = DEFAULT_PERIOD,
                                raw_hist=None) -> dict:
    """Detecta una ruptura del nivel de Larry Williams (apertura de hoy +-
    k * rango del dia anterior) en una vela POSTERIOR a la de apertura, con
    volumen que confirme. Devuelve un dict con la misma forma que analyze().

    signal = "none" si la ultima barra cerrada ES la de apertura, si no se
    encuentra la apertura de hoy o el dia anterior dentro del historial, o
    si el precio no rompio el nivel con volumen suficiente.
    """
    hist = raw_hist if raw_hist is not None else _fetch_history(symbol, period, interval)
    hist = hist.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    hist = _drop_unclosed_bar(hist, interval)
    if len(hist) < BB_PERIOD + 5:
        raise ValueError(
            f"No hay suficiente historial de '{symbol}' en intervalo {interval}/{period} "
            f"para calcular el nivel de Larry Williams."
        )

    last_ts = hist.index[-1]
    last_close = float(hist["Close"].iloc[-1])
    last_volume = float(hist["Volume"].iloc[-1])
    today = last_ts.date()

    signal = "none"
    detected_signal = "none"
    today_open = long_level = short_level = prev_range = None
    volume_ratio = 0.0
    enough_volume_data = False
    high_volume = False
    avg_volume = 0.0

    is_opening_bar = last_ts.time() == MARKET_OPEN_BAR_TIME
    today_bars = hist[hist.index.date == today]
    opening_rows = today_bars[today_bars.index.time == MARKET_OPEN_BAR_TIME]

    prior_days = sorted({d for d in hist.index.date if d < today})
    prev_day = prior_days[-1] if prior_days else None

    if not is_opening_bar and len(opening_rows) == 1 and prev_day is not None:
        today_open = float(opening_rows["Open"].iloc[0])

        prev_day_bars = hist[hist.index.date == prev_day]
        prev_day_regular = prev_day_bars[
            (prev_day_bars.index.time >= MARKET_OPEN_BAR_TIME) & (prev_day_bars.index.time < dtime(16, 0))
        ]
        if prev_day_regular.empty:
            prev_day_regular = prev_day_bars
        prev_range = float(prev_day_regular["High"].max() - prev_day_regular["Low"].min())

        if prev_range > 0:
            long_level = today_open + VOLATILITY_BREAKOUT_K * prev_range
            short_level = today_open - VOLATILITY_BREAKOUT_K * prev_range

            historical_volumes = hist["Volume"].iloc[:-1]
            nonzero_volumes = historical_volumes[historical_volumes > 0]
            enough_volume_data = len(nonzero_volumes) >= VOLUME_LOOKBACK
            avg_volume = float(nonzero_volumes.iloc[-VOLUME_LOOKBACK:].mean()) if len(nonzero_volumes) > 0 else 0.0
            volume_ratio = (last_volume / avg_volume) if avg_volume else 0.0
            high_volume = enough_volume_data and last_volume > avg_volume

            if last_close >= long_level and high_volume:
                detected_signal = "buy_call"
            elif last_close <= short_level and high_volume:
                detected_signal = "buy_put"

    signal = detected_signal if VOLATILITY_BREAKOUT_ENABLED else "none"

    strong_volume = enough_volume_data and volume_ratio >= STRONG_VOLUME_MULT
    volatility_strength = "extrema" if strong_volume else "regular"
    profit_target_pct = _extreme_profit_target(volume_ratio) if strong_volume else PROFIT_TARGET_REGULAR_PCT

    force_eod_exit = False
    try:
        force_eod_exit = is_near_session_close(last_ts)
    except (AttributeError, TypeError, ValueError):
        pass

    if today_open is not None and prev_range is not None and prev_range > 0:
        range_desc = (
            f"Apertura de hoy {today_open:.2f} +- {VOLATILITY_BREAKOUT_K} x rango de ayer ({prev_range:.2f}) -> "
            f"nivel largo {long_level:.2f} / nivel corto {short_level:.2f}. Precio actual {last_close:.2f} "
            f"({'rompio arriba' if detected_signal == 'buy_call' else 'rompio abajo' if detected_signal == 'buy_put' else 'dentro de los niveles'})."
        )
    else:
        range_desc = "No se pudo calcular el nivel (falta la apertura de hoy o el rango del dia anterior)."
    volume_desc = (
        f"Volumen de la barra: {last_volume:,.0f} vs promedio de las ultimas {VOLUME_LOOKBACK} barras con "
        f"volumen real ({volume_ratio:.2f}x) -> volatilidad {volatility_strength}."
        if enough_volume_data else
        "AVISO: no hay suficiente historial de volumen real para confirmar la ruptura."
    )
    exit_plan_desc = (
        f"Plan de salida: tomar ganancia al +{profit_target_pct*100:.0f}% sobre la prima pagada "
        f"(volatilidad {volatility_strength}); cortar perdida al -{STOP_LOSS_PCT*100:.0f}%; o cerrar antes "
        f"por senal tecnica de reversion o cierre de sesion -- mismas reglas de salida que las otras estrategias."
    )
    paused_note = (
        f" [ESTRATEGIA EN ESTUDIO -- hubiera dado {detected_signal.upper()} pero volatility_breakout "
        f"esta desactivada (VOLATILITY_BREAKOUT_ENABLED=False) hasta validar con backtest.py]"
        if not VOLATILITY_BREAKOUT_ENABLED and detected_signal != "none" else ""
    )
    reason = (
        f"[Volatility Breakout (Larry Williams) {interval} | {last_ts:%Y-%m-%d %H:%M}] "
        f"{range_desc} {volume_desc} {exit_plan_desc}{paused_note}"
    )

    return {
        "symbol": symbol,
        "strategy": "volatility_breakout",
        "interval": interval,
        "timestamp": last_ts.isoformat(),
        "signal": signal,
        "reason": reason,
        "band_tightness_explanation": range_desc,
        "volume_explanation": volume_desc,
        "exit_plan_explanation": exit_plan_desc,
        "last_close": last_close,
        "sma": None,
        "upper_band": long_level,
        "lower_band": short_level,
        "band_width_pct": None,
        "band_width_percentile": None,
        "consecutive_squeeze_bars": None,
        "was_squeezed": None,
        "volume": last_volume,
        "avg_volume": avg_volume,
        "volume_ratio": volume_ratio,
        "high_volume": high_volume,
        "enough_volume_data": enough_volume_data,
        "strong_volume": strong_volume,
        "volatility_strength": volatility_strength,
        "profit_target_pct": profit_target_pct,
        "stop_loss_pct": STOP_LOSS_PCT,
        "exit_long_signal": False,
        "exit_short_signal": False,
        "force_eod_exit": force_eod_exit,
        "is_opening_bar": is_opening_bar,
    }


# ---------------------------------------------------------------------------
# Estrategia 3: giro pronunciado de la SMA20 (la linea del medio de las
# bandas). A diferencia de gap_fade_apertura (opera EN CONTRA de un gap
# extremo) y de squeeze_breakout (opera A FAVOR de una ruptura de PRECIO),
# esta opera A FAVOR de un cambio de direccion BRUSCO en la propia SMA20:
# si la media venia subiendo y de golpe gira a bajar con fuerza (o al
# reves), se entra siguiendo la NUEVA direccion del giro.
#
# No depende de donde este el precio respecto a las bandas -- solo de la
# pendiente de la SMA20 barra a barra, comparada contra su propia pendiente
# tipica reciente (mismo patron que volume_ratio: comparar contra un
# promedio propio en vez de un umbral fijo, para que se adapte a cada
# ticker/temporalidad).
# ---------------------------------------------------------------------------

# Umbrales relajados (2026-09-08) a pedido del usuario para tener mas
# aperturas: 4->3 barras consistentes (un giro mas corto ya cuenta como
# tendencia previa) y 1.5x->1.3x de magnitud (un giro moderado ya cuenta
# como "pronunciado", no solo uno extremo). Diagnostico real mostro varios
# tickers con magnitude_ratio > 1.5 que NO disparaban porque la pendiente
# seguia en la MISMA direccion que la tendencia previa (continuacion, no
# giro) -- bajar el umbral de magnitud no fabrica giros que no existen, pero
# reduce la exigencia para los que si ocurran.
#
# Reajustado (2026-09-09) via tune_strategy.py: barrido de 12 combinaciones
# sobre los mismos 10 tickers/60 dias reales. barras=3 se mantiene (mejor
# en todo el barrido); magnitud 1.3x->1.1x subio total_pnl de 65.8% a 88.33%
# (n=370->408, win_rate 52.4%->53.7%) -- mas señales reales capturadas sin
# perder calidad. Verificado despues con backtest.py (motor de produccion,
# no solo el script de ajuste) antes de aplicarlo en vivo.
SMA_TURN_PRIOR_TREND_BARS = 5       # barras previas para establecer la tendencia anterior de la SMA
SMA_TURN_MIN_CONSISTENT_BARS = 3    # de esas barras previas, cuantas deben ir en la misma direccion
SMA_TURN_MAGNITUDE_LOOKBACK = 20    # barras para el promedio de pendiente "tipica" de la SMA
SMA_TURN_MIN_MAGNITUDE_RATIO = 1.1  # el giro debe ser al menos esto veces mas fuerte que lo tipico


def detect_sma_turn(symbol: str, interval: str = DEFAULT_INTERVAL, period: str = DEFAULT_PERIOD,
                     raw_hist=None) -> dict:
    """Detecta un giro pronunciado en la SMA20: si venia con una tendencia
    clara y de golpe invierte direccion con fuerza (comparado con su propio
    movimiento tipico reciente), se entra siguiendo la NUEVA direccion (a
    favor del giro, no en contra)."""
    hist = raw_hist if raw_hist is not None else _fetch_history(symbol, period, interval)
    hist = hist.dropna(subset=["Close", "Volume"])
    hist = _drop_unclosed_bar(hist, interval)
    min_bars = BB_PERIOD + SMA_TURN_PRIOR_TREND_BARS + SMA_TURN_MAGNITUDE_LOOKBACK + 5
    if len(hist) < min_bars:
        raise ValueError(
            f"No hay suficiente historial de '{symbol}' en intervalo {interval}/{period} "
            f"para evaluar el giro de la SMA de {BB_PERIOD} periodos."
        )

    closes = hist["Close"]
    volumes = hist["Volume"]
    sma, upper, lower, width_pct = _bollinger(closes)

    last_close = float(closes.iloc[-1])
    last_sma = float(sma.iloc[-1])
    last_upper = float(upper.iloc[-1])
    last_lower = float(lower.iloc[-1])
    last_width = float(width_pct.iloc[-1])
    last_ts = hist.index[-1]

    # Pendiente de la SMA barra a barra, en % de su propio valor (comparable
    # entre tickers de precios muy distintos).
    sma_diff_pct = (sma.diff() / sma * 100).dropna()

    current_slope = float(sma_diff_pct.iloc[-1])
    prior_slopes = sma_diff_pct.iloc[-(SMA_TURN_PRIOR_TREND_BARS + 1):-1]
    magnitude_lookback = sma_diff_pct.iloc[-(SMA_TURN_MAGNITUDE_LOOKBACK + 1):-1]

    rising_prior_bars = int((prior_slopes > 0).sum())
    falling_prior_bars = int((prior_slopes < 0).sum())
    prior_trend = None
    if rising_prior_bars >= SMA_TURN_MIN_CONSISTENT_BARS:
        prior_trend = "alcista"
    elif falling_prior_bars >= SMA_TURN_MIN_CONSISTENT_BARS:
        prior_trend = "bajista"

    avg_abs_slope = float(magnitude_lookback.abs().mean()) if len(magnitude_lookback) else 0.0
    magnitude_ratio = (abs(current_slope) / avg_abs_slope) if avg_abs_slope else 0.0
    is_pronounced = magnitude_ratio >= SMA_TURN_MIN_MAGNITUDE_RATIO

    signal = "none"
    if prior_trend == "alcista" and current_slope < 0 and is_pronounced:
        signal = "buy_put"
    elif prior_trend == "bajista" and current_slope > 0 and is_pronounced:
        signal = "buy_call"

    # Mismo tratamiento de volumen que las otras estrategias, para fijar el
    # objetivo de ganancia con el mismo criterio que ya se usa por defecto.
    historical_volumes = volumes.iloc[:-1]
    nonzero_volumes = historical_volumes[historical_volumes > 0]
    enough_volume_data = len(nonzero_volumes) >= VOLUME_LOOKBACK
    avg_volume = float(nonzero_volumes.iloc[-VOLUME_LOOKBACK:].mean()) if len(nonzero_volumes) > 0 else 0.0
    last_volume = float(volumes.iloc[-1])
    volume_ratio = (last_volume / avg_volume) if avg_volume else 0.0
    strong_volume = enough_volume_data and volume_ratio >= STRONG_VOLUME_MULT
    volatility_strength = "extrema" if strong_volume else "regular"
    profit_target_pct = _extreme_profit_target(volume_ratio) if strong_volume else PROFIT_TARGET_REGULAR_PCT

    force_eod_exit = False
    try:
        force_eod_exit = is_near_session_close(last_ts)
    except (AttributeError, TypeError, ValueError):
        pass

    turn_desc = (
        f"Tendencia previa de la SMA20 (ultimas {SMA_TURN_PRIOR_TREND_BARS} barras): "
        f"{prior_trend or 'sin tendencia clara'} ({rising_prior_bars} barras subiendo, "
        f"{falling_prior_bars} bajando -- se requieren {SMA_TURN_MIN_CONSISTENT_BARS} para contar como "
        f"tendencia). Pendiente actual: {current_slope:+.3f}% por barra vs pendiente tipica reciente de "
        f"{avg_abs_slope:.3f}% ({magnitude_ratio:.2f}x -- {'SI' if is_pronounced else 'NO'} es un giro "
        f"pronunciado, umbral {SMA_TURN_MIN_MAGNITUDE_RATIO:.1f}x)."
    )
    volume_desc = (
        f"Volumen de la barra: {last_volume:,.0f} vs promedio de las ultimas {VOLUME_LOOKBACK} barras "
        f"con volumen real {avg_volume:,.0f} ({volume_ratio:.2f}x) -> volatilidad {volatility_strength}."
        if enough_volume_data else
        f"Volumen de la barra: {last_volume:,.0f}. AVISO: no hay suficiente historial de volumen real "
        f"para comparar -> se usa el objetivo de ganancia regular por defecto."
    )
    exit_plan_desc = (
        f"Plan de salida: tomar ganancia al +{profit_target_pct*100:.0f}% sobre la prima pagada "
        f"(volatilidad {volatility_strength}); cortar perdida al -{STOP_LOSS_PCT*100:.0f}%; o cerrar antes "
        f"por senal tecnica de reversion o cierre de sesion -- mismas reglas de salida que las otras estrategias."
    )
    reason = (
        f"[Giro de SMA20 {BB_PERIOD},{BB_STD} | {interval} | {last_ts:%Y-%m-%d %H:%M}] "
        f"Estrategia A FAVOR del nuevo giro (no en contra): {turn_desc} {volume_desc} {exit_plan_desc}"
    )

    return {
        "symbol": symbol,
        "strategy": "giro_sma20",
        "interval": interval,
        "timestamp": last_ts.isoformat(),
        "signal": signal,
        "reason": reason,
        "band_tightness_explanation": turn_desc,
        "volume_explanation": volume_desc,
        "exit_plan_explanation": exit_plan_desc,
        "last_close": last_close,
        "sma": last_sma,
        "upper_band": last_upper,
        "lower_band": last_lower,
        "band_width_pct": last_width,
        "band_width_percentile": None,
        "consecutive_squeeze_bars": None,
        "was_squeezed": None,
        "volume": last_volume,
        "avg_volume": avg_volume,
        "volume_ratio": volume_ratio,
        "high_volume": None,
        "enough_volume_data": enough_volume_data,
        "strong_volume": strong_volume,
        "volatility_strength": volatility_strength,
        "profit_target_pct": profit_target_pct,
        "stop_loss_pct": STOP_LOSS_PCT,
        "exit_long_signal": False,
        "exit_short_signal": False,
        "force_eod_exit": force_eod_exit,
        "prior_trend": prior_trend,
        "current_slope_pct": current_slope,
        "magnitude_ratio": magnitude_ratio,
    }


# ---------------------------------------------------------------------------
# Estrategia (añadida 2026-09-08): RSI en reversa desde extremo. Distinta
# de las demas: usa el oscilador RSI(14) en vez de las bandas de Bollinger.
# Se opera A FAVOR de la reversion: RSI que sale de sobreventa (<30) hacia
# arriba -> CALL (se espera que el precio rebote); RSI que sale de
# sobrecompra (>70) hacia abajo -> PUT. Con confirmacion de volumen (mismo
# criterio que las otras estrategias). Comparte naturaleza de "reversion en
# extremo" con giro_sma20, que fue la estrategia mas solida en el backtest
# -- se prueba con la hipotesis de que ese tipo de señal funciona bien en
# este watchlist/temporalidad.
# ---------------------------------------------------------------------------

RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

# PAUSADA (2026-09-08, el mismo dia que se agrego): backtest de 60 dias /
# 10 tickers dio win rate 40.7% y P&L direccional total -13.87% -- tercera
# estrategia candidata seguida que falla en este watchlist/temporalidad
# (gap_fade_apertura y vwap_cross tambien fallaron el mismo dia). Ver
# backtest.py.
RSI_REVERSAL_ENABLED = False


def _compute_rsi(closes, period: int = RSI_PERIOD):
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(100)


def detect_rsi_reversal(symbol: str, interval: str = DEFAULT_INTERVAL, period: str = DEFAULT_PERIOD,
                         raw_hist=None) -> dict:
    """Detecta un cruce de RSI(14) saliendo de un extremo (sobreventa <30 o
    sobrecompra >70) hacia el centro, con confirmacion de volumen. Se opera
    A FAVOR del rebote esperado."""
    hist = raw_hist if raw_hist is not None else _fetch_history(symbol, period, interval)
    hist = hist.dropna(subset=["Close", "Volume"])
    hist = _drop_unclosed_bar(hist, interval)
    if len(hist) < RSI_PERIOD + VOLUME_LOOKBACK + 5:
        raise ValueError(
            f"No hay suficiente historial de '{symbol}' en intervalo {interval}/{period} "
            f"para calcular el RSI de {RSI_PERIOD} periodos."
        )

    closes = hist["Close"]
    volumes = hist["Volume"]
    rsi = _compute_rsi(closes)
    last_ts = hist.index[-1]

    last_close = float(closes.iloc[-1])
    last_rsi = float(rsi.iloc[-1])
    prev_rsi = float(rsi.iloc[-2]) if len(rsi) >= 2 and _notna(rsi.iloc[-2]) else None

    crossed_up_from_oversold = prev_rsi is not None and prev_rsi < RSI_OVERSOLD <= last_rsi
    crossed_down_from_overbought = prev_rsi is not None and prev_rsi > RSI_OVERBOUGHT >= last_rsi

    historical_volumes = volumes.iloc[:-1]
    nonzero_volumes = historical_volumes[historical_volumes > 0]
    enough_volume_data = len(nonzero_volumes) >= VOLUME_LOOKBACK
    avg_volume = float(nonzero_volumes.iloc[-VOLUME_LOOKBACK:].mean()) if len(nonzero_volumes) > 0 else 0.0
    last_volume = float(volumes.iloc[-1])
    volume_ratio = (last_volume / avg_volume) if avg_volume else 0.0
    high_volume = enough_volume_data and last_volume > avg_volume
    strong_volume = enough_volume_data and volume_ratio >= STRONG_VOLUME_MULT

    signal = "none"
    if crossed_up_from_oversold and high_volume:
        signal = "buy_call"
    elif crossed_down_from_overbought and high_volume:
        signal = "buy_put"
    detected_signal = signal
    if not RSI_REVERSAL_ENABLED:
        signal = "none"

    volatility_strength = "extrema" if strong_volume else "regular"
    profit_target_pct = _extreme_profit_target(volume_ratio) if strong_volume else PROFIT_TARGET_REGULAR_PCT

    force_eod_exit = False
    try:
        force_eod_exit = is_near_session_close(last_ts)
    except (AttributeError, TypeError, ValueError):
        pass

    cross_desc = (
        "salio de SOBREVENTA hacia arriba" if crossed_up_from_oversold else
        "salio de SOBRECOMPRA hacia abajo" if crossed_down_from_overbought else
        "sin cruce de extremo nuevo"
    )
    if prev_rsi is not None:
        rsi_desc = (
            f"RSI({RSI_PERIOD}): {last_rsi:.1f} vs {prev_rsi:.1f} en la barra previa -> {cross_desc} "
            f"(umbrales: sobreventa <{RSI_OVERSOLD}, sobrecompra >{RSI_OVERBOUGHT}). Precio {last_close:.2f}."
        )
    else:
        rsi_desc = (
            f"RSI({RSI_PERIOD}): {last_rsi:.1f}, sin dato de la barra previa para evaluar cruce. "
            f"Precio {last_close:.2f}."
        )
    volume_desc = (
        f"Volumen de la barra: {last_volume:,.0f} vs promedio de las ultimas {VOLUME_LOOKBACK} barras "
        f"con volumen real {avg_volume:,.0f} ({volume_ratio:.2f}x) -> {'SI' if high_volume else 'NO'} confirma "
        f"con volumen alto -> volatilidad {volatility_strength}."
        if enough_volume_data else
        f"Volumen de la barra: {last_volume:,.0f}. AVISO: no hay suficiente historial de volumen real "
        f"para comparar -> no se confirma volumen alto con estos datos."
    )
    exit_plan_desc = (
        f"Plan de salida: tomar ganancia al +{profit_target_pct*100:.0f}% sobre la prima pagada "
        f"(volatilidad {volatility_strength}); cortar perdida al -{STOP_LOSS_PCT*100:.0f}%; o cerrar antes "
        f"por senal tecnica de reversion o cierre de sesion -- mismas reglas de salida que las otras estrategias."
    )
    paused_note = (
        f" [ESTRATEGIA PAUSADA -- hubiera dado {detected_signal.upper()} pero rsi_reversal esta desactivada "
        f"(RSI_REVERSAL_ENABLED=False) tras backtest con win rate 40.7%, ver backtest.py]"
        if not RSI_REVERSAL_ENABLED and detected_signal != "none" else ""
    )
    reason = (
        f"[RSI reversa | {interval} | {last_ts:%Y-%m-%d %H:%M}] Estrategia A FAVOR del rebote desde el "
        f"extremo: {rsi_desc} {volume_desc} {exit_plan_desc}{paused_note}"
    )

    return {
        "symbol": symbol,
        "strategy": "rsi_reversal",
        "interval": interval,
        "timestamp": last_ts.isoformat(),
        "signal": signal,
        "reason": reason,
        "band_tightness_explanation": rsi_desc,
        "volume_explanation": volume_desc,
        "exit_plan_explanation": exit_plan_desc,
        "last_close": last_close,
        "sma": last_close,
        "upper_band": last_close,
        "lower_band": last_close,
        "band_width_pct": 0.0,
        "band_width_percentile": None,
        "consecutive_squeeze_bars": None,
        "was_squeezed": None,
        "volume": last_volume,
        "avg_volume": avg_volume,
        "volume_ratio": volume_ratio,
        "high_volume": high_volume,
        "enough_volume_data": enough_volume_data,
        "strong_volume": strong_volume,
        "volatility_strength": volatility_strength,
        "profit_target_pct": profit_target_pct,
        "stop_loss_pct": STOP_LOSS_PCT,
        "exit_long_signal": False,
        "exit_short_signal": False,
        "force_eod_exit": force_eod_exit,
        "rsi": last_rsi,
        "prev_rsi": prev_rsi,
    }


# ---------------------------------------------------------------------------
# Estrategia 4 (añadida 2026-09-08): cruce con VWAP (precio promedio
# ponderado por volumen del DIA -- se reinicia cada sesion, no es
# acumulado entre dias). Distinta de las otras 3: no usa las bandas de
# Bollinger para la señal en si -- es un nivel de referencia intradia
# muy usado por traders institucionales. Se opera A FAVOR del cruce:
# precio que cruza por ENCIMA del VWAP -> CALL; por DEBAJO -> PUT, con
# confirmacion de volumen (mismo criterio que las otras estrategias).
# ---------------------------------------------------------------------------

VWAP_MIN_BARS_INTO_SESSION = 3  # ignorar los primeros bares del dia -- VWAP con muy pocos datos es ruidoso

# PAUSADA (2026-09-08, el mismo dia que se agrego): backtest de 60 dias /
# 10 tickers dio win rate 47.6% (peor que el azar) y P&L direccional total
# -19.67%, peor incluso que gap_fade_apertura -- ver backtest.py. Queda el
# codigo por si se quiere refinar (ej. exigir volumen mas fuerte, o una
# distancia minima al VWAP tras el cruce para evitar whipsaws) y volver a
# probar, pero no se auto-ejecuta mientras esto sea False.
VWAP_CROSS_ENABLED = False


def detect_vwap_cross(symbol: str, interval: str = DEFAULT_INTERVAL, period: str = DEFAULT_PERIOD,
                       raw_hist=None) -> dict:
    """Detecta un cruce de precio con el VWAP del dia actual, con
    confirmacion de volumen. VWAP = suma(precio tipico * volumen) /
    suma(volumen), acumulado SOLO desde la apertura del dia de la ultima
    barra (se reinicia cada dia de mercado)."""
    hist = raw_hist if raw_hist is not None else _fetch_history(symbol, period, interval)
    hist = hist.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    hist = _drop_unclosed_bar(hist, interval)
    if len(hist) < VOLUME_LOOKBACK + VWAP_MIN_BARS_INTO_SESSION + 2:
        raise ValueError(
            f"No hay suficiente historial de '{symbol}' en intervalo {interval}/{period} "
            f"para calcular el cruce con VWAP."
        )

    last_ts = hist.index[-1]
    current_day = last_ts.date()
    today_bars = hist.loc[hist.index.date == current_day]
    bars_into_session = len(today_bars)

    typical_price = (today_bars["High"] + today_bars["Low"] + today_bars["Close"]) / 3
    cum_pv = (typical_price * today_bars["Volume"]).cumsum()
    cum_vol = today_bars["Volume"].cumsum().replace(0, np.nan)
    vwap = cum_pv / cum_vol

    last_close = float(today_bars["Close"].iloc[-1])
    last_vwap = float(vwap.iloc[-1]) if _notna(vwap.iloc[-1]) else None

    crossed_above = crossed_below = False
    if bars_into_session >= VWAP_MIN_BARS_INTO_SESSION + 1 and last_vwap is not None and _notna(vwap.iloc[-2]):
        prev_close = float(today_bars["Close"].iloc[-2])
        prev_vwap = float(vwap.iloc[-2])
        crossed_above = prev_close <= prev_vwap and last_close > last_vwap
        crossed_below = prev_close >= prev_vwap and last_close < last_vwap

    volumes = hist["Volume"]
    historical_volumes = volumes.iloc[:-1]
    nonzero_volumes = historical_volumes[historical_volumes > 0]
    enough_volume_data = len(nonzero_volumes) >= VOLUME_LOOKBACK
    avg_volume = float(nonzero_volumes.iloc[-VOLUME_LOOKBACK:].mean()) if len(nonzero_volumes) > 0 else 0.0
    last_volume = float(volumes.iloc[-1])
    volume_ratio = (last_volume / avg_volume) if avg_volume else 0.0
    high_volume = enough_volume_data and last_volume > avg_volume
    strong_volume = enough_volume_data and volume_ratio >= STRONG_VOLUME_MULT

    signal = "none"
    if crossed_above and high_volume:
        signal = "buy_call"
    elif crossed_below and high_volume:
        signal = "buy_put"
    detected_signal = signal
    if not VWAP_CROSS_ENABLED:
        signal = "none"

    volatility_strength = "extrema" if strong_volume else "regular"
    profit_target_pct = _extreme_profit_target(volume_ratio) if strong_volume else PROFIT_TARGET_REGULAR_PCT

    force_eod_exit = False
    try:
        force_eod_exit = is_near_session_close(last_ts)
    except (AttributeError, TypeError, ValueError):
        pass

    vwap_str = f"{last_vwap:.2f}" if last_vwap is not None else "sin dato"
    cross_desc = "cruzo ARRIBA" if crossed_above else "cruzo ABAJO" if crossed_below else "sin cruce nuevo"
    vwap_desc = (
        f"Barra {bars_into_session} del dia. VWAP intradia: {vwap_str} vs cierre {last_close:.2f} "
        f"-> precio {cross_desc} del VWAP en esta barra "
        f"(minimo {VWAP_MIN_BARS_INTO_SESSION} barras dentro de la sesion para evaluar)."
    )
    volume_desc = (
        f"Volumen de la barra: {last_volume:,.0f} vs promedio de las ultimas {VOLUME_LOOKBACK} barras "
        f"con volumen real {avg_volume:,.0f} ({volume_ratio:.2f}x) -> {'SI' if high_volume else 'NO'} confirma "
        f"con volumen alto -> volatilidad {volatility_strength}."
        if enough_volume_data else
        f"Volumen de la barra: {last_volume:,.0f}. AVISO: no hay suficiente historial de volumen real "
        f"para comparar -> no se confirma volumen alto con estos datos."
    )
    exit_plan_desc = (
        f"Plan de salida: tomar ganancia al +{profit_target_pct*100:.0f}% sobre la prima pagada "
        f"(volatilidad {volatility_strength}); cortar perdida al -{STOP_LOSS_PCT*100:.0f}%; o cerrar antes "
        f"por senal tecnica de reversion o cierre de sesion -- mismas reglas de salida que las otras estrategias."
    )
    paused_note = (
        f" [ESTRATEGIA PAUSADA -- hubiera dado {detected_signal.upper()} pero vwap_cross esta desactivada "
        f"(VWAP_CROSS_ENABLED=False) tras backtest con win rate 47.6%, ver backtest.py]"
        if not VWAP_CROSS_ENABLED and detected_signal != "none" else ""
    )
    reason = (
        f"[Cruce VWAP | {interval} | {last_ts:%Y-%m-%d %H:%M}] Estrategia A FAVOR del cruce (continuacion "
        f"tras romper el nivel institucional del dia): {vwap_desc} {volume_desc} {exit_plan_desc}{paused_note}"
    )

    return {
        "symbol": symbol,
        "strategy": "vwap_cross",
        "interval": interval,
        "timestamp": last_ts.isoformat(),
        "signal": signal,
        "reason": reason,
        "band_tightness_explanation": vwap_desc,
        "volume_explanation": volume_desc,
        "exit_plan_explanation": exit_plan_desc,
        "last_close": last_close,
        "sma": last_vwap if last_vwap is not None else last_close,
        "upper_band": last_vwap if last_vwap is not None else last_close,
        "lower_band": last_vwap if last_vwap is not None else last_close,
        "band_width_pct": 0.0,
        "band_width_percentile": None,
        "consecutive_squeeze_bars": None,
        "was_squeezed": None,
        "volume": last_volume,
        "avg_volume": avg_volume,
        "volume_ratio": volume_ratio,
        "high_volume": high_volume,
        "enough_volume_data": enough_volume_data,
        "strong_volume": strong_volume,
        "volatility_strength": volatility_strength,
        "profit_target_pct": profit_target_pct,
        "stop_loss_pct": STOP_LOSS_PCT,
        "exit_long_signal": False,
        "exit_short_signal": False,
        "force_eod_exit": force_eod_exit,
        "vwap": last_vwap,
        "crossed_above": crossed_above,
        "crossed_below": crossed_below,
    }


def scan_all_signals(symbol: str, interval: str = DEFAULT_INTERVAL, period: str = DEFAULT_PERIOD) -> list[dict]:
    """Corre TODAS las estrategias disponibles (squeeze_breakout,
    gap_fade_apertura, giro_sma20, vwap_cross) sobre `symbol`, reutilizando
    una sola descarga de 15m y una sola de 1h para todas (en vez de que
    cada estrategia pida su propio historial por separado) -- ver leccion
    de eficiencia en memoria. Devuelve una lista con un dict por estrategia,
    cada uno ya con confirmacion multi-temporalidad aplicada (mismas claves
    que analyze_with_confirmation())."""
    raw_hist = _fetch_history(symbol, period, interval)
    raw_hist_by_interval = {interval: raw_hist} if interval == MTF_TIMEFRAMES[0][0] else {}
    trends = multi_timeframe_trends(symbol, raw_hist_by_interval=raw_hist_by_interval)

    results = []
    for detector in (analyze, detect_opening_gap_fade, detect_sma_turn, detect_vwap_cross, detect_rsi_reversal):
        result = detector(symbol, interval=interval, period=period, raw_hist=raw_hist)
        confirmation = confirmation_for_signal(result["signal"], trends)
        result["mtf_trends"] = trends
        result["confidence"] = confirmation["confidence"]
        result["confidence_detail"] = confirmation
        results.append(result)

    # Entrada temprana (vela en formacion, ver analyze_forming_bar arriba)
    # -- solo se evalua si squeeze_breakout normal (vela cerrada, results[0]
    # porque analyze() es el primer detector) NO disparo ya una señal, para
    # no duplicar la misma entrada dos veces en el mismo ciclo.
    if results[0]["signal"] == "none":
        early = analyze_forming_bar(symbol, interval=interval, period=period)
        if early is not None:
            confirmation = confirmation_for_signal(early["signal"], trends)
            early["mtf_trends"] = trends
            early["confidence"] = confirmation["confidence"]
            early["confidence_detail"] = confirmation
            results.append(early)

    return results


# ---------------------------------------------------------------------------
# Proteccion de ganancia: si una posicion abierta esta en ganancia pero no ha
# llegado al objetivo por defecto, y aparece una señal tecnica de reversion,
# se cierra AHORA para asegurar la ganancia disponible en vez de esperar y
# arriesgarse a que el mercado se de vuelta y termine en perdida.
#
# Ademas: dejar correr una posicion hacia el objetivo completo solo tiene
# sentido si hay motivo para esperar que la volatilidad continue durante la
# proxima hora. Esa señal es la tendencia de 1h: si confirma la direccion
# de la entrada de 15m, hay respaldo para continuar; si no, lo mas probable
# es que el impulso de 15m sea corto y se desvanezca antes de llegar al
# objetivo -- en ese caso conviene asegurar la ganancia disponible temprano
# en vez de arriesgarse a esperar.
# ---------------------------------------------------------------------------

EARLY_LOCK_WITHOUT_1H_SUPPORT_PCT = 0.05  # asegurar ganancia desde +5% si 1h no respalda la entrada

# El usuario pidio priorizar velocidad: en day trading de 15m, una ganancia
# solida en mano vale mas que esperar horas por el objetivo completo (10-20%)
# que podria no llegar. Tras al menos una vela de 15m completa desde la
# entrada, si ya hay una ganancia razonable, se toma YA -- sin depender de
# si hay respaldo de 1h ni de que aparezca una señal tecnica de reversion.
QUICK_PROFIT_PCT = 0.08          # +8% ya se considera una ganancia rapida solida
QUICK_PROFIT_MIN_MINUTES = 15    # esperar al menos 1 vela de 15m completa antes de aplicar esta regla

# 2026-09-15, a pedido del usuario tras una sesion con varias perdidas
# ("no podemos entrar en las falsas volatilidades y si eso pasa hay que
# tomar ganancias antes de que haya perdidas"): las reglas de arriba solo
# protegen ganancia cuando la posicion YA esta en positivo en el momento
# del chequeo (regla 4) o llego a +5% (regla 5) -- ninguna mira el
# CAMINO. Caso real: HOOD hoy llego a estar unos minutos en ganancia (el
# quiebre bajista funciono al principio) y despues se revirtio del todo
# hasta el stop-loss (-22.52%), sin que ninguna regla existente actuara
# en el medio porque el chequeo de entonces (cada 120s) no coincidio con
# la ventana breve de ganancia. Con el nuevo motor de salidas de 30s esto
# ya deberia pasar menos, pero igual hace falta una regla que mire el
# PICO de ganancia visto durante toda la posicion, no solo el momento
# actual: una vez que se vio una ganancia real de PEAK_PROFIT_LOCK_PCT o
# mas, no se deja que la posicion vuelva a cero o negativo -- se asegura
# lo que se pueda apenas cruce de vuelta por debajo de PEAK_PROFIT_LOCK_PCT.
PEAK_PROFIT_LOCK_PCT = 0.03      # a partir de +3% de pico visto, se protege esa ganancia

# 2026-09-16, a pedido del usuario ("chequear cualquier posible rechazo y
# tomar ganancias antes de que se vaya en contra"), root-causado con MSFT:
# perdio -21.53% sin que NINGUNA regla de arriba actuara, porque
# technical_exit_signal (cross_above_mid/cross_below_mid) exige que el
# precio cruce toda la banda MEDIA -- un viaje mucho mas largo. MSFT nunca
# cruzo la media (495.54), pero SI volvio a cruzar por ENCIMA de la banda
# inferior que habia roto al entrar (494.01) -- eso ya es un rechazo real
# de la ruptura, mucho mas rapido de detectar. rejection_signal (calculado
# por el llamador comparando el precio actual contra la banda que se
# rompio al entrar, no la banda media) dispara este cierre temprano SOLO
# si la posicion todavia no esta en ganancia (pnl_pct <= 0) -- si ya esta
# en ganancia, no hace falta: la regla de pico de ganancia (arriba) ya
# protege eso. Ventana acotada a los primeros
# EARLY_REJECTION_WINDOW_MINUTES para no disparar en posiciones viejas
# donde la banda ya se movio con la tendencia por otras razones.
EARLY_REJECTION_WINDOW_MINUTES = 30


def evaluate_open_position_exit(
    entry_premium: float,
    current_premium: float,
    profit_target_pct: float,
    technical_exit_signal: bool,
    hourly_aligned: bool = True,
    force_eod_exit: bool = False,
    rejection_signal: bool = False,
    stop_loss_pct: float = STOP_LOSS_PCT,
    minutes_since_entry: float = 0.0,
    quick_profit_pct: float = QUICK_PROFIT_PCT,
    peak_pnl_pct: float | None = None,
) -> dict:
    """Decide si cerrar una posicion abierta ahora mismo, y por que. Orden
    de prioridad (lo que ocurra primero):

      1) Objetivo de ganancia alcanzado -> cerrar, ganancia completa.
      2) Stop-loss alcanzado -> cerrar, cortar la perdida.
      3) force_eod_exit=True -> cerrar. CAMBIO (2026-09-09, a pedido del
         usuario): esto YA NO significa "cerro la sesion de hoy" -- ahora
         el llamador (position_monitor.py, via _is_near_expiration()) lo
         activa solo cuando el contrato esta a <= NEAR_EXPIRATION_DAYS de
         su vencimiento REAL. Antes de este cambio, toda posicion se
         forzaba a cerrar al fin de CADA sesion (day trading puro, nunca
         overnight) -- ahora las posiciones SI quedan abiertas overnight,
         con el riesgo que eso agrega (gaps fuera de horario, caida de la
         prima por el paso de los dias) a cambio de mas tiempo para
         alcanzar el objetivo. Ver INCIDENTS.md y CLAUDE.md.
      4) rejection_signal=True (el precio volvio a cruzar hacia ADENTRO de
         la banda que rompio al entrar -- no hace falta que cruce toda la
         banda media, eso es mucho mas lento) Y la posicion todavia NO esta
         en ganancia (pnl_pct <= 0) Y sigue dentro de los primeros
         EARLY_REJECTION_WINDOW_MINUTES desde que entro -> cerrar YA. 2026-09-16,
         a pedido del usuario ("chequear cualquier posible rechazo... antes
         de que se vaya en contra"), caso real: MSFT perdio -21.53% sin que
         ninguna otra regla actuara, porque nunca cruzo la banda media.
      5) Se vio un PICO de ganancia de al menos PEAK_PROFIT_LOCK_PCT en
         algun momento de la posicion (peak_pnl_pct) Y la ganancia actual
         ya volvio a 0 o menos -> cerrar YA. 2026-09-15, a pedido del
         usuario: una posicion que llego a estar en ganancia real no debe
         terminar dando vuelta a perdida sin que se intente asegurar algo
         en el camino -- ver la nota junto a PEAK_PROFIT_LOCK_PCT arriba
         (caso real: HOOD).
      6) Señal tecnica de reversion Y la posicion esta en ganancia (aunque
         no haya llegado al objetivo) -> cerrar YA para proteger esa
         ganancia, en vez de esperar a que el mercado no llegue al objetivo
         por defecto y termine dando vuelta a perdida.
      7) La tendencia de 1h NO respalda la direccion de la entrada
         (hourly_aligned=False) Y ya hay una ganancia razonable
         (>= EARLY_LOCK_WITHOUT_1H_SUPPORT_PCT) -> asegurarla ahora.
      8) Ya paso al menos una vela de 15m completa (minutes_since_entry >=
         QUICK_PROFIT_MIN_MINUTES) Y la ganancia ya es solida
         (>= quick_profit_pct) -> tomar la ganancia YA, sin esperar el
         objetivo completo. Prioriza velocidad: en 15m, una ganancia en
         mano vale mas que esperar horas por un objetivo mayor.
      9) Si nada de lo anterior aplica -> mantener y dejar correr hacia el
         objetivo completo (paso 1).
      10) Señal tecnica de reversion pero la posicion sigue en perdida (no
         llego al stop) -> NO se fuerza el cierre solo por la señal; el
         stop-loss sigue siendo el limite (evita salidas prematuras por
         ruido cuando ya se acepto ese riesgo al entrar).
    """
    pnl_pct = (current_premium / entry_premium - 1) if entry_premium else 0.0
    target_price = compute_target_price(entry_premium, profit_target_pct)
    stop_price = compute_stop_loss_price(entry_premium, stop_loss_pct)

    if current_premium >= target_price - _PRICE_EPSILON:
        return {"should_close": True, "reason": "objetivo_alcanzado", "pnl_pct": pnl_pct}
    if current_premium <= stop_price + _PRICE_EPSILON:
        return {"should_close": True, "reason": "stop_loss", "pnl_pct": pnl_pct}
    if force_eod_exit:
        return {"should_close": True, "reason": "cierre_por_vencimiento", "pnl_pct": pnl_pct}
    if rejection_signal and pnl_pct <= 0 and minutes_since_entry <= EARLY_REJECTION_WINDOW_MINUTES:
        return {"should_close": True, "reason": "rechazo_de_ruptura", "pnl_pct": pnl_pct}
    if peak_pnl_pct is not None and peak_pnl_pct >= PEAK_PROFIT_LOCK_PCT and pnl_pct <= 0:
        return {"should_close": True, "reason": "proteger_pico_de_ganancia", "pnl_pct": pnl_pct}
    if technical_exit_signal and pnl_pct > 0:
        return {"should_close": True, "reason": "proteger_ganancia_parcial", "pnl_pct": pnl_pct}
    if not hourly_aligned and pnl_pct >= EARLY_LOCK_WITHOUT_1H_SUPPORT_PCT:
        return {"should_close": True, "reason": "asegurar_ganancia_sin_respaldo_1h", "pnl_pct": pnl_pct}
    if minutes_since_entry >= QUICK_PROFIT_MIN_MINUTES and pnl_pct >= quick_profit_pct:
        return {"should_close": True, "reason": "toma_rapida_15m", "pnl_pct": pnl_pct}
    return {"should_close": False, "reason": "mantener", "pnl_pct": pnl_pct}
