"""Convierte las velas de Tradier (timesales) al formato que usa el motor de senales.

El motor trabaja con un DataFrame indexado por fecha-hora, con columnas
Open, High, Low, Close y Volume, igual que devuelve yfinance.
"""
import pandas as pd


def bars_from_timesales(payload: dict) -> pd.DataFrame:
    """payload: respuesta de /v1/markets/timesales con series.data[] de time, open, high, low, close, volume."""
    series = (payload or {}).get("series") or {}
    data = series.get("data") or []
    if not data:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    df = pd.DataFrame(data)
    # Tradier devuelve hora LOCAL de mercado (America/New_York) sin offset
    # (ej. "2026-10-07 10:15:00"), a diferencia de yfinance que ya trae el
    # indice tz-aware en esa misma zona. Sin localizar, cualquier logica
    # downstream que compare contra datetime.now() -- como
    # bollinger_strategy._drop_unclosed_bar(), que decide si la ULTIMA vela
    # ya cerro -- compararia una hora de NY contra la hora UTC del
    # servidor, pudiendo quedarse una vela de mas o de menos sin avisar.
    # Confirmado en vivo 2026-10-07 (yf.Ticker(...).history() trae
    # dtype=datetime64[s, America/New_York]; Tradier venia naive).
    df["time"] = pd.to_datetime(df["time"]).dt.tz_localize("America/New_York")
    df = df.set_index("time").sort_index()
    out = pd.DataFrame({
        "Open": df["open"].astype(float),
        "High": df["high"].astype(float),
        "Low": df["low"].astype(float),
        "Close": df["close"].astype(float),
        "Volume": df["volume"].astype(float),
    })
    return out[~out.index.duplicated(keep="last")]
