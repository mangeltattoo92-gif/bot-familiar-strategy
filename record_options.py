#!/usr/bin/env python3
"""Guarda historia propia de opciones: una foto cada 10 minutos en horario de mercado.

Por que existe: no hay historial de precios de opciones disponible para
backtests. Cada foto guarda bid, ask, ultimo precio, volumen e interes
abierto de los contratos cercanos al precio (+-10%) con vencimiento en 21
dias o menos. Con semanas de fotos se puede medir la ganancia real de los
contratos.

Fuente: yfinance (provisional). Cuando haya token de Tradier, la fuente
cambia a su cadena de opciones.

Uso: python record_options.py   (lo levanta systemd)
"""
import datetime as dt
import sqlite3
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import yfinance as yf

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "option_history.db"
INTERVAL_SECONDS = 600
MAX_DAYS = 21
STRIKE_BAND = 0.10


def _connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS option_snapshots (
            ts TEXT NOT NULL, underlying TEXT NOT NULL, spot REAL NOT NULL,
            expiration TEXT NOT NULL, option_type TEXT NOT NULL, strike REAL NOT NULL,
            bid REAL, ask REAL, last REAL, volume INTEGER, open_interest INTEGER
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snap_contract ON option_snapshots (underlying, expiration, option_type, strike, ts)"
    )
    conn.commit()
    return conn


def _tickers() -> list[str]:
    import sys
    sys.path.insert(0, str(ROOT))
    from paper_trading.engine import DEFAULT_FAST_WATCHLIST, DEFAULT_WATCHLIST
    return sorted({t.strip() for t in (DEFAULT_WATCHLIST + "," + DEFAULT_FAST_WATCHLIST).split(",") if t.strip()})


def record_once(tickers: list[str], conn: sqlite3.Connection) -> int:
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    today = dt.date.today()
    rows = []
    for t in tickers:
        try:
            tk = yf.Ticker(t)
            hist = tk.history(period="1d")
            if hist.empty:
                continue
            spot = float(hist["Close"].iloc[-1])
            for exp in tk.options:
                days = (dt.date.fromisoformat(exp) - today).days
                if days < 0 or days > MAX_DAYS:
                    continue
                chain = tk.option_chain(exp)
                for kind, df in (("call", chain.calls), ("put", chain.puts)):
                    near = df[(df["strike"] - spot).abs() / spot <= STRIKE_BAND]
                    for _, r in near.iterrows():
                        rows.append((
                            ts, t, spot, exp, kind, float(r["strike"]),
                            float(r["bid"] or 0), float(r["ask"] or 0), float(r["lastPrice"] or 0),
                            int(r["volume"] or 0), int(r["openInterest"] or 0),
                        ))
        except Exception:
            continue
    conn.executemany("INSERT INTO option_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def _market_hours() -> bool:
    now = dt.datetime.now(dt.timezone.utc)
    return now.weekday() < 5 and 13 <= now.hour <= 19


def main() -> None:
    conn = _connect()
    tickers = _tickers()
    while True:
        if _market_hours():
            started = time.time()
            n = record_once(tickers, conn)
            print(f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} -- {n} contratos guardados en {int(time.time()-started)}s", flush=True)
            time.sleep(INTERVAL_SECONDS)
        else:
            time.sleep(300)


if __name__ == "__main__":
    main()
