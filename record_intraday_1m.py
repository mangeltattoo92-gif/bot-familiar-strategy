#!/usr/bin/env python3
"""
Guarda velas de 1m de la watchlist en disco -- 2026-09-18, a pedido del
usuario, para poder repetir el backtest de confirmacion intra-vela
(backtest_confirmacion.py) con mas de los ~7 dias que Yahoo entrega de 1m.

Corre UNA vez por dia despues del cierre (systemd timer, 4:35pm ET), no
durante el mercado, para no competir con el bot por el rate-limit de Yahoo.
Pide 5 dias hacia atras por ticker (asi un dia perdido se recupera solo) y
guarda con INSERT OR IGNORE (correrlo dos veces nunca duplica). Solo sesion
regular (prepost=False). No toca ninguna cuenta ni configuracion del bot.

Uso:
  python record_intraday_1m.py                 # watchlists de las cuentas activas
  python record_intraday_1m.py --tickers MU,CRM --period 1d
"""
import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "intraday_1m.db"
LOG_PATH = ROOT / "data" / "record_intraday_1m.log"
BASE_SYMBOLS = ["SPY", "QQQ"]
PAUSE_BETWEEN_SYMBOLS_SECONDS = 1.0
MAX_ATTEMPTS = 3


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bars (
            symbol TEXT NOT NULL,
            ts TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume INTEGER NOT NULL,
            PRIMARY KEY (symbol, ts)
        )
        """
    )
    return conn


def symbols_to_record() -> list[str]:
    symbols = list(BASE_SYMBOLS)
    try:
        from multi_user_entry import active_accounts
        from paper_trading.engine import get_settings

        for _username, db_path in active_accounts():
            settings = get_settings(db_path)
            for t in settings["watchlist"] + settings["fast_watchlist"]:
                if t not in symbols:
                    symbols.append(t)
    except Exception as e:
        _log(f"No se pudieron leer las watchlists de las cuentas ({e!r}) -- se graban solo {BASE_SYMBOLS}.")
    return symbols


def record_symbol(conn: sqlite3.Connection, symbol: str, period: str) -> int:
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            hist = yf.Ticker(symbol).history(period=period, interval="1m", prepost=False)
            hist = hist.dropna(subset=["Open", "High", "Low", "Close"])
            if hist.empty:
                last_error = "sin datos"
                time.sleep(5 * (attempt + 1))
                continue
            rows = [
                (symbol, ts.isoformat(), float(r.Open), float(r.High), float(r.Low), float(r.Close), int(r.Volume))
                for ts, r in hist.iterrows()
            ]
            before = conn.total_changes
            conn.executemany("INSERT OR IGNORE INTO bars VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            conn.commit()
            return conn.total_changes - before
        except Exception as e:
            last_error = repr(e)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(last_error)


def main() -> int:
    ap = argparse.ArgumentParser(description="Graba velas de 1m de la watchlist")
    ap.add_argument("--tickers", default=None)
    ap.add_argument("--period", default="5d")
    args = ap.parse_args()

    symbols = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] if args.tickers else symbols_to_record()
    conn = _connect()
    inserted, failed = 0, []
    for sym in symbols:
        try:
            inserted += record_symbol(conn, sym, args.period)
        except Exception as e:
            failed.append(sym)
            _log(f"[{sym}] ERROR: {e}")
        time.sleep(PAUSE_BETWEEN_SYMBOLS_SECONDS)
    total = conn.execute("SELECT COUNT(*), COUNT(DISTINCT substr(ts, 1, 10)) FROM bars").fetchone()
    conn.close()
    _log(f"{len(symbols) - len(failed)}/{len(symbols)} tickers grabados, {inserted} velas nuevas. "
         f"Total en disco: {total[0]} velas, {total[1]} dias distintos."
         + (f" Fallaron: {', '.join(failed)}" if failed else ""))
    return 1 if len(failed) > len(symbols) / 2 else 0


if __name__ == "__main__":
    sys.exit(main())
