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
    df["time"] = pd.to_datetime(df["time"])
    df = df.set_index("time").sort_index()
    out = pd.DataFrame({
        "Open": df["open"].astype(float),
        "High": df["high"].astype(float),
        "Low": df["low"].astype(float),
        "Close": df["close"].astype(float),
        "Volume": df["volume"].astype(float),
    })
    return out[~out.index.duplicated(keep="last")]
