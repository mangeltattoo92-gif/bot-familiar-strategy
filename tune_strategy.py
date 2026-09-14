#!/usr/bin/env python3
"""
Ajuste de parametros de las 2 estrategias activas (squeeze_breakout,
giro_sma20) contra datos historicos reales -- descarga cada ticker UNA sola
vez y prueba varias combinaciones de umbrales sobre los mismos datos
cacheados, en vez de re-descargar por cada combinacion (mucho mas rapido
que correr backtest.py una vez por parametro).

Mismas limitaciones que backtest.py: mide direccion del SUBYACENTE (no P&L
real de opciones), 60 dias de yfinance, sin las reglas de salida
adicionales del bot en vivo (solo objetivo/stop/cierre de sesion vendria
implicito -- aqui, como en backtest.py, se mide hasta el cierre de sesion).

Uso:
  python tune_strategy.py giro_sma20
  python tune_strategy.py squeeze_breakout
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf

import paper_trading.bollinger_strategy as bs
from paper_trading.engine import get_settings

DEFAULT_PERIOD = "60d"
DEFAULT_INTERVAL = "15m"
MIN_LOOKBACK_BARS = bs.BB_PERIOD + bs.SQUEEZE_LOOKBACK + 10


def fetch_all(symbols: list[str], period: str = DEFAULT_PERIOD, interval: str = DEFAULT_INTERVAL) -> dict:
    def _fetch(sym):
        hist = yf.Ticker(sym).history(period=period, interval=interval, prepost=True)
        hist = hist.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
        return sym, hist

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = dict(pool.map(_fetch, symbols))
    return results


def simulate(symbol: str, hist, detector_fn, interval: str = DEFAULT_INTERVAL, period: str = DEFAULT_PERIOD) -> list[dict]:
    """Igual que backtest.backtest_ticker() pero para UN solo detector
    (recibe la funcion ya con los parametros del modulo aplicados)."""
    if len(hist) < MIN_LOOKBACK_BARS + 50:
        return []

    trades = []
    open_trade = None

    for i in range(MIN_LOOKBACK_BARS, len(hist)):
        window = hist.iloc[: i + 1]
        current_bar_time = window.index[-1]
        current_price = float(window["Close"].iloc[-1])
        current_day = current_bar_time.date()

        if open_trade is not None:
            entry = open_trade
            direction = 1 if entry["signal"] == "buy_call" else -1
            new_day = current_day != entry["entry_day"]
            if new_day:
                exit_price = entry["last_price_same_day"]
                pnl_pct = direction * (exit_price / entry["entry_price"] - 1) * 100
                trades.append({"pnl_pct": pnl_pct, "win": pnl_pct > 0})
                open_trade = None
            else:
                entry["last_price_same_day"] = current_price
            continue

        try:
            candidate = detector_fn(symbol, interval=interval, period=period, raw_hist=window)
        except Exception:
            candidate = None

        if candidate and candidate["signal"] != "none":
            open_trade = {
                "strategy": candidate["strategy"], "signal": candidate["signal"],
                "entry_time": current_bar_time, "entry_price": current_price,
                "entry_day": current_day, "last_price_same_day": current_price,
            }

    if open_trade is not None:
        entry = open_trade
        direction = 1 if entry["signal"] == "buy_call" else -1
        exit_price = entry["last_price_same_day"]
        pnl_pct = direction * (exit_price / entry["entry_price"] - 1) * 100
        trades.append({"pnl_pct": pnl_pct, "win": pnl_pct > 0})

    return trades


def summarize(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate": None, "avg_pnl_pct": None, "total_pnl_pct": None}
    wins = sum(1 for t in trades if t["win"])
    total = sum(t["pnl_pct"] for t in trades)
    return {"n": n, "win_rate": round(wins / n * 100, 1), "avg_pnl_pct": round(total / n, 2), "total_pnl_pct": round(total, 2)}


def tune_giro_sma20(hist_by_symbol: dict):
    print("Ajustando giro_sma20 (barras consistentes x magnitud minima)\n")
    combos = [
        (bars, mag)
        for bars in (3, 4)
        for mag in (1.1, 1.2, 1.3, 1.5, 1.8, 2.0)
    ]
    original_bars = bs.SMA_TURN_MIN_CONSISTENT_BARS
    original_mag = bs.SMA_TURN_MIN_MAGNITUDE_RATIO
    try:
        for bars, mag in combos:
            bs.SMA_TURN_MIN_CONSISTENT_BARS = bars
            bs.SMA_TURN_MIN_MAGNITUDE_RATIO = mag
            all_trades = []
            for symbol, hist in hist_by_symbol.items():
                all_trades.extend(simulate(symbol, hist, bs.detect_sma_turn))
            s = summarize(all_trades)
            print(f"  barras>={bars}, magnitud>={mag}x  ->  n={s['n']:<4} win_rate={s['win_rate']}%  "
                  f"avg_pnl={s['avg_pnl_pct']}%  total_pnl={s['total_pnl_pct']}%")
    finally:
        bs.SMA_TURN_MIN_CONSISTENT_BARS = original_bars
        bs.SMA_TURN_MIN_MAGNITUDE_RATIO = original_mag


def tune_squeeze_breakout(hist_by_symbol: dict):
    print("Ajustando squeeze_breakout (percentil de squeeze x minimo de barras x volumen extremo)\n")
    combos = [
        (pct, min_bars)
        for pct in (20, 25, 30, 35, 40)
        for min_bars in (2, 3)
    ]
    original_pct = bs.SQUEEZE_PERCENTILE
    original_bars = bs.SQUEEZE_MIN_BARS
    try:
        for pct, min_bars in combos:
            bs.SQUEEZE_PERCENTILE = pct
            bs.SQUEEZE_MIN_BARS = min_bars
            all_trades = []
            for symbol, hist in hist_by_symbol.items():
                all_trades.extend(simulate(symbol, hist, bs.analyze))
            s = summarize(all_trades)
            print(f"  percentil<={pct}, barras>={min_bars}  ->  n={s['n']:<4} win_rate={s['win_rate']}%  "
                  f"avg_pnl={s['avg_pnl_pct']}%  total_pnl={s['total_pnl_pct']}%")
    finally:
        bs.SQUEEZE_PERCENTILE = original_pct
        bs.SQUEEZE_MIN_BARS = original_bars


def main():
    parser = argparse.ArgumentParser(description="Ajuste de parametros contra datos historicos reales")
    parser.add_argument("strategy", choices=["giro_sma20", "squeeze_breakout"])
    parser.add_argument("--tickers", default=None, help="Lista separada por comas (default: watchlist principal)")
    args = parser.parse_args()

    symbols = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else get_settings()["watchlist"]
    print(f"Descargando {len(symbols)} tickers ({DEFAULT_PERIOD} / {DEFAULT_INTERVAL}) una sola vez...\n")
    hist_by_symbol = fetch_all(symbols)

    if args.strategy == "giro_sma20":
        tune_giro_sma20(hist_by_symbol)
    else:
        tune_squeeze_breakout(hist_by_symbol)


if __name__ == "__main__":
    sys.exit(main() or 0)
