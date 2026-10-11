"""Adaptador: hace que los datos de Tradier se vean exactamente como los
de yfinance para el motor de estrategias existente (paper_trading/
bollinger_strategy.py, paper_trading/option_selection.py, webapp/
market_data.py). Ninguna logica de estrategia cambia -- solo de donde
sale el DataFrame de velas o la cadena de opciones.

Creado 2026-10-07 a pedido del usuario ("mi bot simulado desde ahora va
ser solo para tradier"), para una cuenta a la vez (ver tradier_cycle.py)
mientras el resto de las cuentas sigue con yfinance sin cambios.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from paper_trading.option_selection import _nearest_business_day_expiration
from webapp.brokers.bars import bars_from_timesales
from webapp.brokers.pricing import effective_window
from webapp.brokers.tradier import TradierClient

# Tradier solo da velas intradia (1/5/15 min) hasta ~40 dias calendario
# atras con session_filter=open (documentado + confirmado en vivo
# 2026-10-07). Un periodo de yfinance mas largo que esto (ej. "3mo" para
# la tendencia de 1h) se recorta a este tope -- de sobra para lo que la
# estrategia realmente necesita (MTF_TREND_SLOW=50 barras de 1h, ver
# bollinger_strategy.py), nunca a los 90 dias calendario literales de "3mo".
_MAX_INTRADAY_LOOKBACK_DAYS = 38

_PERIOD_TO_DAYS = {
    "1d": 1, "5d": 5, "1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730,
}


def _period_to_days(period: str) -> int:
    return _PERIOD_TO_DAYS.get(period, 30)


def fetch_history(client: TradierClient, symbol: str, period: str, interval: str) -> pd.DataFrame:
    """Reemplazo de yf.Ticker(symbol).history(period=period, interval=interval)
    via Tradier. interval "15m"/"5m"/"1m" van directo a timesales; "1h" se
    arma re-muestreando velas de 15m a 1h (Tradier no da 1h nativo)."""
    lookback_days = min(_period_to_days(period), _MAX_INTRADAY_LOOKBACK_DAYS)
    lookback_minutes = lookback_days * 24 * 60

    base_interval = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "15min"}.get(interval)
    if base_interval is None:
        raise ValueError(f"fetch_history: interval '{interval}' no soportado via Tradier")

    start, end = effective_window(client.data_delay_minutes, lookback_minutes)
    payload = client.timesales(symbol, start=start, end=end, interval=base_interval)
    bars = bars_from_timesales(payload)

    if interval != "1h" or bars.empty:
        return bars

    hourly = bars.resample("1h", label="left", closed="left").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
    })
    return hourly.dropna(subset=["Open"])


def _rows_to_frame(options: list[dict], option_type: str) -> pd.DataFrame:
    """Filas crudas de /v1/markets/options/chains -> DataFrame con las
    mismas columnas que yf.Ticker().option_chain().calls/.puts usa
    paper_trading/option_selection.py y webapp/market_data.py."""
    rows = []
    for row in options:
        if row.get("option_type") != option_type:
            continue
        greeks = row.get("greeks") or {}
        # smv_vol (volatilidad de la superficie, suavizada) en vez de
        # mid_iv (implicita cruda del bid/ask de ESTE contrato puntual):
        # confirmado en vivo 2026-10-07 que mid_iv puede salir disparado
        # (ej. 175%) en contratos con bid=0/poco liquidos, mientras smv_vol
        # se mantiene razonable -- select_contract() ya descarta IV < 0.03
        # como invalida, pero no filtra IV absurdamente ALTA, asi que usar
        # la version ruidosa inflaria el delta Black-Scholes calculado.
        iv = greeks.get("smv_vol") or greeks.get("mid_iv") or 0
        rows.append({
            "strike": row.get("strike"),
            "bid": row.get("bid") or 0,
            "ask": row.get("ask") or 0,
            "lastPrice": row.get("last"),
            "openInterest": row.get("open_interest") or 0,
            "volume": row.get("volume") or 0,
            "impliedVolatility": iv,
        })
    return pd.DataFrame(rows)


def _chain_from_payload(payload: dict) -> SimpleNamespace:
    options = ((payload or {}).get("options") or {}).get("option") or []
    if isinstance(options, dict):  # Tradier devuelve un dict pelado (no lista) si hay un solo contrato
        options = [options]
    return SimpleNamespace(calls=_rows_to_frame(options, "call"), puts=_rows_to_frame(options, "put"))


def get_expiration_and_chain(client: TradierClient, symbol: str) -> tuple[str | None, SimpleNamespace | None]:
    """Reemplazo de option_selection.get_expiration_and_chain(): elige el
    vencimiento mas cercano a TARGET_BUSINESS_DAYS_OUT (misma regla, misma
    funcion reutilizada) y trae esa cadena con greeks de Tradier."""
    raw = client.expirations(symbol)
    dates = ((raw or {}).get("expirations") or {}).get("date")
    if dates is None:
        return None, None
    if isinstance(dates, str):
        dates = [dates]
    expiration = _nearest_business_day_expiration(list(dates), date.today())
    if expiration is None:
        return None, None
    payload = client.option_chain(symbol, expiration, greeks=True)
    return expiration, _chain_from_payload(payload)


def get_chain_for_expiration(client: TradierClient, symbol: str, expiration: str) -> SimpleNamespace | None:
    """Reemplazo de market_data._get_option_chain_cached(): cadena de un
    vencimiento YA CONOCIDO (la posicion abierta la trae guardada), para
    calcular el precio de salida -- no elige vencimiento, a diferencia de
    get_expiration_and_chain() de arriba."""
    try:
        payload = client.option_chain(symbol, expiration, greeks=True)
    except Exception:
        return None
    return _chain_from_payload(payload)
