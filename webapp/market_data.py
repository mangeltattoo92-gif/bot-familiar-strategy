"""
Datos de mercado en vivo para el panel local, via yfinance (gratuito, sin
API key). Independiente del MCP de Robinhood: este panel debe poder
refrescarse aunque no haya una sesion de Claude activa.

Todo con cache en memoria de corta duracion para no saturar yfinance.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import yfinance as yf

# Backoff ante rate-limit de Yahoo (2026-09-10 -- la ampliacion de
# watchlist a 101 tickers con 3 procesos escaneando en paralelo (cada uno
# con su propia sesion/crumb de yfinance) disparo "Too Many Requests" en
# CASI TODAS las llamadas durante mas de una hora, dejando el bot sin
# operar aunque los procesos seguian vivos. Antes, un solo fallo de
# rate-limit se propagaba de una y se saltaba ese ticker ese ciclo nada
# mas -- ahora se reintenta con backoff, porque el limite de Yahoo suele
# ser de corta duracion (segundos) y la mayoria de los rate-limits se
# resuelven solos en el segundo o tercer intento.
#
# CORRECCION el mismo dia: la primera version (3 reintentos, timeout
# completo cada vez) podia tardar ~65s por llamada durante un rate-limit
# sostenido, y con varias llamadas fallando seguido eso volvia a colgar
# proximity_watch.py -- el mismo problema que se intentaba resolver. Ahora
# hay un PRESUPUESTO DE TIEMPO TOTAL por llamada.
_RATE_LIMIT_RETRIES = 1
_RATE_LIMIT_BACKOFF_BASE = 1.5
_CALL_TOTAL_BUDGET = 20  # segundos MAXIMO por llamada entre todos los intentos juntos


def _looks_like_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "too many requests" in msg or "rate limit" in msg or "429" in msg

# yfinance no expone `timeout=` en fast_info/option_chain, y aunque lo
# expusiera, un rate-limit sostenido de Yahoo puede dejar la llamada
# colgada mucho mas de lo esperado -- eso es lo que paralizaba
# position_monitor.py / proximity_watch.py / el panel Flask (servidor de
# desarrollo, un solo hilo) indefinidamente. _with_timeout() fuerza un
# limite de pared duro sobre CUALQUIER llamada de red aqui: si no vuelve a
# tiempo, se lanza TimeoutError (que los try/except ya existentes en cada
# funcion capturan igual que cualquier otro fallo de red).
_NETWORK_TIMEOUT = 15  # segundos
_timeout_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="yf-timeout")


def _with_timeout(fn, timeout: float = _NETWORK_TIMEOUT):
    deadline = time.time() + min(_CALL_TOTAL_BUDGET, timeout * (_RATE_LIMIT_RETRIES + 1) + 10)
    last_exc = None
    attempt = 0
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise last_exc or TimeoutError(f"Llamada a yfinance supero el presupuesto de reintentos")
        future = _timeout_executor.submit(fn)
        try:
            return future.result(timeout=min(timeout, remaining))
        except _FutureTimeoutError:
            last_exc = TimeoutError(f"Llamada a yfinance excedio {timeout}s (posible rate-limit / red lenta)")
            if attempt >= _RATE_LIMIT_RETRIES or time.time() >= deadline:
                raise last_exc
        except Exception as e:
            if not _looks_like_rate_limit(e) or attempt >= _RATE_LIMIT_RETRIES:
                raise
            last_exc = e
            sleep_for = min(_RATE_LIMIT_BACKOFF_BASE * (attempt + 1), max(deadline - time.time(), 0))
            if sleep_for > 0:
                time.sleep(sleep_for)
        attempt += 1


_QUOTE_TTL = 30          # segundos, precios de posiciones abiertas
_MARKET_TTL = 20 * 60    # segundos, resumen semanal / ETFs (cambia poco)

_quote_cache: dict[str, tuple[float, float | None]] = {}
_market_cache: dict[str, tuple[float, object]] = {}
_option_chain_cache: dict[str, tuple[float, object]] = {}
_OPTION_CHAIN_TTL = 15  # segundos -- option_chain() es lento (~1-2s), cachear evita repetirlo en cada refresco

# 2026-09-16, a pedido del usuario ("que no tenga problemas" al escalar a
# mas usuarios): el cache de arriba es SOLO en memoria del proceso -- pero
# multi_user_entry_loop.py y exit_watch_loop.py arrancan un PROCESO NUEVO
# en cada ciclo (a proposito, ver la nota en multi_user_entry_loop.py
# sobre threads colgados), asi que ese cache se borra cada vez y nunca se
# comparte entre el motor de entradas (cada 120s) y el de salidas rapidas
# (cada 30s) aunque esten chequeando la MISMA posicion segundos despues.
# Este cache en disco (mismo TTL, mismo criterio de "vencido") persiste
# entre procesos y se comparte entre todos -- reduce las llamadas de red a
# Yahoo Finance a medida que se suman mas usuarios/posiciones, sin cambiar
# ninguna decision de trading (mismos datos, solo se piden menos veces).
_DISK_CACHE_DIR = Path(__file__).resolve().parent.parent / "data"
_QUOTE_DISK_CACHE = _DISK_CACHE_DIR / "quote_cache.json"
_OPTION_CHAIN_DISK_CACHE = _DISK_CACHE_DIR / "option_chain_cache.json"


def _load_disk_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_disk_cache(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)  # atomico -- nunca deja el archivo a medio escribir
    except Exception:
        pass  # el cache es una optimizacion -- si falla, se sigue pidiendo la red normal


def _get_option_chain_cached(underlying: str, expiration: str):
    """Cadena de opciones (calls/puts) con cache de 2 niveles: memoria del
    proceso actual (mas rapido) y disco (compartido entre procesos/ciclos
    distintos dentro del mismo TTL). Devuelve un objeto con .calls/.puts
    (DataFrames), igual que yf.Ticker(...).option_chain(...)."""
    cache_key = f"{underlying}_{expiration}"
    now = time.time()

    cached = _option_chain_cache.get(cache_key)
    if cached and now - cached[0] < _OPTION_CHAIN_TTL:
        return cached[1]

    disk_cache = _load_disk_cache(_OPTION_CHAIN_DISK_CACHE)
    disk_entry = disk_cache.get(cache_key)
    if disk_entry and now - disk_entry[0] < _OPTION_CHAIN_TTL:
        chain = SimpleNamespace(
            calls=pd.DataFrame(disk_entry[1]["calls"]),
            puts=pd.DataFrame(disk_entry[1]["puts"]),
        )
        _option_chain_cache[cache_key] = (disk_entry[0], chain)
        return chain

    chain = _with_timeout(lambda: yf.Ticker(underlying).option_chain(expiration))
    _option_chain_cache[cache_key] = (now, chain)
    disk_cache[cache_key] = (now, {
        "calls": chain.calls.to_dict(orient="records"),
        "puts": chain.puts.to_dict(orient="records"),
    })
    _save_disk_cache(_OPTION_CHAIN_DISK_CACHE, disk_cache)
    return chain

MARKET_INDEXES = {
    "S&P 500": "^GSPC",
    "Nasdaq 100": "^NDX",
    "Dow Jones": "^DJI",
    "Russell 2000": "^RUT",
}

# ETFs liquidos y populares para day trading; se rankean por volumen/volatilidad reales.
CANDIDATE_ETFS = [
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "SMH",
    "ARKK", "TQQQ", "SQQQ", "GDX", "EEM", "HYG", "TLT", "UVXY",
]

# Los ETFs mas conocidos/mencionados por el publico general. Los 4 principales
# indices tienen un futuro asociado que cotiza en fin de semana/fuera de horario,
# util como indicador (no garantia) de como podria abrir el lunes.
KNOWN_ETFS = [
    {"symbol": "SPY", "name": "S&P 500", "future": "ES=F"},
    {"symbol": "QQQ", "name": "Nasdaq 100", "future": "NQ=F"},
    {"symbol": "DIA", "name": "Dow Jones", "future": "YM=F"},
    {"symbol": "IWM", "name": "Russell 2000", "future": "RTY=F"},
    {"symbol": "VOO", "name": "S&P 500 (Vanguard)", "future": None},
    {"symbol": "VTI", "name": "Mercado total EEUU", "future": None},
    {"symbol": "GLD", "name": "Oro", "future": "GC=F"},
    {"symbol": "XLF", "name": "Sector financiero", "future": None},
    {"symbol": "ARKK", "name": "Innovacion (ARK)", "future": None},
    {"symbol": "TLT", "name": "Bonos Tesoro 20+ anos", "future": "ZN=F"},
]


def get_quote(symbol: str) -> float | None:
    """Precio actual (o ultimo cierre) de un simbolo de yfinance."""
    now = time.time()
    cached = _quote_cache.get(symbol)
    if cached and now - cached[0] < _QUOTE_TTL:
        return cached[1]

    disk_cache = _load_disk_cache(_QUOTE_DISK_CACHE)
    disk_entry = disk_cache.get(symbol)
    if disk_entry and now - disk_entry[0] < _QUOTE_TTL:
        _quote_cache[symbol] = tuple(disk_entry)
        return disk_entry[1]

    price = None
    try:
        info = _with_timeout(lambda: yf.Ticker(symbol).fast_info)
        price = float(info["lastPrice"])
    except Exception:
        try:
            hist = _with_timeout(lambda: yf.Ticker(symbol).history(period="1d"))
            if hist is not None and not hist.empty:
                price = float(hist["Close"].iloc[-1])
        except Exception:
            price = None
    _quote_cache[symbol] = (now, price)
    disk_cache[symbol] = (now, price)
    _save_disk_cache(_QUOTE_DISK_CACHE, disk_cache)
    return price


def _option_row_price(row) -> float | None:
    """Punto medio bid/ask de una fila de la cadena de opciones, o
    'lastPrice' si no hay bid/ask en vivo. Se prefiere el punto medio sobre
    lastPrice: lastPrice es el precio de la ULTIMA OPERACION real de ese
    contrato especifico, que en strikes poco liquidos puede ser de dias
    atras (yfinance no lo marca como stale) y desviar mucho el P&L
    mostrado. bid/ask, en cambio, se cotiza en vivo aunque no haya habido
    una operacion reciente."""
    bid = float(row.get("bid", 0) or 0)
    ask = float(row.get("ask", 0) or 0)
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 4)
    last_price = row.get("lastPrice")
    return float(last_price) if last_price is not None else None


def get_option_price(underlying: str, expiration: str, strike: float, option_type: str) -> float | None:
    """Precio actual de un contrato de opcion via la cadena de opciones de
    yfinance.

    Si el strike exacto no aparece en yfinance -- puede pasar: Robinhood
    lista un strike (ej. GLD 403) que yfinance simplemente no trae para esa
    cadena, aunque los strikes vecinos (402, 404) si esten -- se interpola
    linealmente el PRECIO (bid/ask mid) entre los dos strikes mas cercanos.

    NOTA (probado 2026-09-08): se intento interpolar via volatilidad
    implicita + Black-Scholes en vez de precio crudo, esperando mayor
    precision -- resulto PEOR: para GLD (opciones de estilo americano, con
    prima de ejercicio anticipado que Black-Scholes europeo no captura) el
    precio teorico salio sistematicamente ~$0.50 por debajo del precio real
    de mercado (verificado contra Robinhood), peor que la simple
    interpolacion lineal de precio (que quedo ~$0.32 por debajo). Revertido
    a interpolacion lineal de precio. Sigue siendo una aproximacion -- en
    strikes con muy poco volumen/open interest puede desviarse unos
    centavos del precio real; para el strike exacto (caso normal) no aplica
    ninguna interpolacion y el precio es el bid/ask real."""
    try:
        chain = _get_option_chain_cached(underlying, expiration)
        df = chain.calls if option_type.lower() == "call" else chain.puts
        strike = float(strike)
        row = df[df["strike"] == strike]
        if not row.empty:
            return _option_row_price(row.iloc[0])

        lower = df[df["strike"] < strike].sort_values("strike").tail(1)
        upper = df[df["strike"] > strike].sort_values("strike").head(1)
        if lower.empty or upper.empty:
            return None
        lower_price = _option_row_price(lower.iloc[0])
        upper_price = _option_row_price(upper.iloc[0])
        if lower_price is None or upper_price is None:
            return None
        lower_strike = float(lower.iloc[0]["strike"])
        upper_strike = float(upper.iloc[0]["strike"])
        weight = (strike - lower_strike) / (upper_strike - lower_strike)
        return round(lower_price + weight * (upper_price - lower_price), 4)
    except Exception:
        pass
    return None


def get_option_bid_ask(underlying: str, expiration: str, strike: float, option_type: str) -> tuple[float | None, float | None]:
    """Bid y ask REALES (no el punto medio) de un contrato, para simular
    ejecucion realista: comprar al ASK, vender al BID -- el costo real del
    spread que _option_row_price/get_option_price (punto medio) no
    capturan. Creado 2026-09-10 para family_sim.py (10 cuentas simuladas),
    a pedido explicito del usuario ('lo mas real posible'). A diferencia
    de get_option_price(), NO interpola entre strikes vecinos si el strike
    exacto no esta en la cadena -- un bid/ask interpolado seria un numero
    inventado, no un precio real de mercado -- devuelve (None, None) en
    ese caso (raro en la practica: el strike de salida es el mismo que se
    eligio al entrar, que si estaba en la cadena en ese momento)."""
    try:
        chain = _get_option_chain_cached(underlying, expiration)
        df = chain.calls if option_type.lower() == "call" else chain.puts
        row = df[df["strike"] == float(strike)]
        if row.empty:
            return None, None
        bid = float(row.iloc[0].get("bid") or 0)
        ask = float(row.iloc[0].get("ask") or 0)
        return (bid or None), (ask or None)
    except Exception:
        return None, None


def _cached(key: str, ttl: float, fn):
    now = time.time()
    cached = _market_cache.get(key)
    if cached and now - cached[0] < ttl:
        return cached[1]
    value = fn()
    _market_cache[key] = (now, value)
    return value


def get_weekly_market_summary() -> dict:
    def _compute():
        summary = {}
        for name, symbol in MARKET_INDEXES.items():
            try:
                hist = _with_timeout(lambda symbol=symbol: yf.Ticker(symbol).history(period="5d"))
                if len(hist) >= 2:
                    change_pct = (hist["Close"].iloc[-1] / hist["Close"].iloc[0] - 1) * 100
                    summary[name] = {
                        "symbol": symbol,
                        "week_change_pct": round(float(change_pct), 2),
                        "last_close": round(float(hist["Close"].iloc[-1]), 2),
                    }
            except Exception:
                continue
        changes = [v["week_change_pct"] for v in summary.values()]
        avg_change = sum(changes) / len(changes) if changes else 0.0
        if avg_change > 1.0:
            tone = "alcista"
        elif avg_change < -1.0:
            tone = "bajista"
        else:
            tone = "lateral / mixto"
        return {
            "indexes": summary,
            "avg_change_pct": round(avg_change, 2),
            "tone": tone,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    return _cached("weekly_summary", _MARKET_TTL, _compute)


def get_top_etfs_for_day_trading(limit: int = 8) -> list[dict]:
    def _compute():
        results = []
        for symbol in CANDIDATE_ETFS:
            try:
                hist = _with_timeout(lambda symbol=symbol: yf.Ticker(symbol).history(period="5d"))
                if len(hist) < 2:
                    continue
                avg_volume = float(hist["Volume"].mean())
                week_change_pct = float((hist["Close"].iloc[-1] / hist["Close"].iloc[0] - 1) * 100)
                avg_range_pct = float(((hist["High"] - hist["Low"]) / hist["Close"]).mean() * 100)
                results.append({
                    "symbol": symbol,
                    "avg_volume": int(avg_volume),
                    "week_change_pct": round(week_change_pct, 2),
                    "avg_daily_range_pct": round(avg_range_pct, 2),
                    "last_close": round(float(hist["Close"].iloc[-1]), 2),
                })
            except Exception:
                continue
        # Rankeo simple: mas volumen y mas rango diario = mas interesante para day trading.
        results.sort(key=lambda r: (r["avg_volume"], r["avg_daily_range_pct"]), reverse=True)
        return results[:limit]

    return _cached(f"top_etfs_{limit}", _MARKET_TTL, _compute)


_WATCHLIST_TTL = 60  # segundos


def get_watchlist_overview(symbols: list[str]) -> list[dict]:
    """Vista en vivo (precio, cambio, volumen, volatilidad) de una lista
    arbitraria de tickers -- pensada para el watchlist configurado por el
    usuario en Configuracion de operativa. Descarga en lote (una sola
    peticion para todos los simbolos) para que sea rapido con 30+ tickers.
    """
    def _compute():
        try:
            data = _with_timeout(lambda: yf.download(symbols, period="5d", group_by="ticker", threads=True, progress=False))
        except Exception:
            return []

        results = []
        for symbol in symbols:
            try:
                hist = data[symbol].dropna() if len(symbols) > 1 else data.dropna()
                if len(hist) < 2:
                    continue
                last_close = float(hist["Close"].iloc[-1])
                prev_close = float(hist["Close"].iloc[-2])
                day_change_pct = (last_close / prev_close - 1) * 100
                week_change_pct = (last_close / float(hist["Close"].iloc[0]) - 1) * 100
                avg_volume = float(hist["Volume"].mean())
                avg_range_pct = float(((hist["High"] - hist["Low"]) / hist["Close"]).mean() * 100)
                results.append({
                    "symbol": symbol,
                    "last_close": round(last_close, 2),
                    "day_change_pct": round(day_change_pct, 2),
                    "week_change_pct": round(week_change_pct, 2),
                    "avg_volume": int(avg_volume),
                    "avg_daily_range_pct": round(avg_range_pct, 2),
                })
            except Exception:
                continue
        return results

    cache_key = f"watchlist_{'_'.join(symbols)}"
    return _cached(cache_key, _WATCHLIST_TTL, _compute)


def get_known_etfs_recap() -> list[dict]:
    """Cierre de la semana de los ETFs mas conocidos + indicador de futuros
    para la posible apertura del lunes (solo referencial, no una prediccion).
    """
    def _compute():
        results = []
        for etf in KNOWN_ETFS:
            try:
                hist = _with_timeout(lambda etf=etf: yf.Ticker(etf["symbol"]).history(period="5d"))
                if hist is None or hist.empty:
                    continue
                week_open = float(hist["Open"].iloc[0])
                week_close = float(hist["Close"].iloc[-1])
                week_change_pct = (week_close / week_open - 1) * 100

                entry = {
                    "symbol": etf["symbol"],
                    "name": etf["name"],
                    "week_open": round(week_open, 2),
                    "week_close": round(week_close, 2),
                    "week_change_pct": round(week_change_pct, 2),
                    "next_week_change_pct": None,
                }

                if etf["future"]:
                    try:
                        fhist = _with_timeout(lambda etf=etf: yf.Ticker(etf["future"]).history(period="5d"))
                        if fhist is not None and len(fhist) >= 2:
                            last_price = float(fhist["Close"].iloc[-1])
                            prev_settle = float(fhist["Close"].iloc[-2])
                            entry["next_week_change_pct"] = round((last_price / prev_settle - 1) * 100, 2)
                    except Exception:
                        pass

                results.append(entry)
            except Exception:
                continue
        return results

    return _cached("known_etfs_recap", _MARKET_TTL, _compute)


def _rsi(closes, period: int = 14):
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _macd(closes, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


# (etiqueta, intervalo yfinance, periodo historico, ventana SMA rapida, ventana SMA lenta)
TECHNICAL_TIMEFRAMES = [
    ("hourly", "1h", "5d", 10, 30),
    ("daily", "1d", "6mo", 20, 50),
    ("weekly", "1wk", "2y", 10, 30),
]


def get_technical_outlook(symbol: str = "SPY") -> dict:
    """Analisis tecnico (tendencia, RSI, MACD) del simbolo en 3 plazos:
    horario (momentum a horas/1-2 dias vista), diario (tendencia de la
    semana) y semanal (tendencia de fondo). Se usa para estimar un sesgo
    probable de como podria moverse el mercado en la proxima semana.

    No es una prediccion garantizada: es lectura de indicadores tecnicos
    estandar sobre datos historicos de Yahoo Finance.
    """
    def _compute():
        timeframes = {}
        chart_series = []

        for label, interval, period, fast_win, slow_win in TECHNICAL_TIMEFRAMES:
            try:
                hist = _with_timeout(lambda period=period, interval=interval: yf.Ticker(symbol).history(period=period, interval=interval))
                closes = hist["Close"].dropna()
                if len(closes) < slow_win + 5:
                    continue

                sma_fast = closes.rolling(fast_win).mean()
                sma_slow = closes.rolling(slow_win).mean()
                rsi = _rsi(closes)
                macd_line, signal_line = _macd(closes)

                last_close = float(closes.iloc[-1])
                last_sma_fast = float(sma_fast.iloc[-1])
                last_sma_slow = float(sma_slow.iloc[-1])
                last_rsi = float(rsi.iloc[-1])
                last_macd = float(macd_line.iloc[-1])
                last_signal = float(signal_line.iloc[-1])

                if last_close > last_sma_fast > last_sma_slow:
                    trend = "alcista"
                elif last_close < last_sma_fast < last_sma_slow:
                    trend = "bajista"
                else:
                    trend = "lateral"

                if last_rsi >= 70:
                    momentum = "sobrecomprado"
                elif last_rsi <= 30:
                    momentum = "sobrevendido"
                else:
                    momentum = "neutral"

                timeframes[label] = {
                    "last_close": round(last_close, 2),
                    "sma_fast": round(last_sma_fast, 2),
                    "sma_slow": round(last_sma_slow, 2),
                    "sma_fast_window": fast_win,
                    "sma_slow_window": slow_win,
                    "rsi": round(last_rsi, 1),
                    "trend": trend,
                    "momentum": momentum,
                    "macd_signal": "alcista" if last_macd > last_signal else "bajista",
                }

                if label == "daily":
                    tail = hist.iloc[-60:]
                    sma_fast_tail = sma_fast.iloc[-60:]
                    sma_slow_tail = sma_slow.iloc[-60:]
                    for ts, close_val in tail["Close"].items():
                        chart_series.append({
                            "date": ts.strftime("%Y-%m-%d"),
                            "close": round(float(close_val), 2),
                            "sma_fast": round(float(sma_fast_tail.loc[ts]), 2) if not pd_isna(sma_fast_tail.loc[ts]) else None,
                            "sma_slow": round(float(sma_slow_tail.loc[ts]), 2) if not pd_isna(sma_slow_tail.loc[ts]) else None,
                        })
            except Exception:
                continue

        trends = [t["trend"] for t in timeframes.values()]
        bullish = trends.count("alcista")
        bearish = trends.count("bajista")
        if bullish > bearish:
            bias = "alcista"
        elif bearish > bullish:
            bias = "bajista"
        else:
            bias = "mixto / lateral"

        return {
            "symbol": symbol,
            "timeframes": timeframes,
            "chart_series": chart_series,
            "bias": bias,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    return _cached(f"technical_outlook_{symbol}", _MARKET_TTL, _compute)


def pd_isna(value) -> bool:
    return value != value  # NaN != NaN es True; evita importar pandas solo para esto
