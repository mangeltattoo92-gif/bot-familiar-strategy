#!/usr/bin/env python3
"""
Backtest historico de las 3 estrategias sobre datos reales de yfinance.

LIMITACIONES IMPORTANTES (leer antes de confiar en los numeros):
  1. yfinance solo da ~60 dias de historial en velas de 15m (limite del
     proveedor gratuito, no de este script) -- la muestra es de semanas,
     no de meses/años.
  2. No hay datos historicos de OPCIONES via yfinance (solo la cadena
     ACTUAL) -- asi que NO se puede simular el P&L real de la prima
     (que depende de delta/theta/IV en el momento historico, datos que no
     existen para fechas pasadas). En vez de eso, este backtest mide la
     CALIDAD DIRECCIONAL de la señal: cuanto se movio el SUBYACENTE (la
     accion/ETF) a favor o en contra de la direccion que la señal indico,
     desde la entrada hasta el cierre de sesion de ese mismo dia (day
     trading, sin overnight). Un resultado positivo aqui significa "la
     señal acerto la direccion"; NO es directamente el % de ganancia que
     hubiera dado la opcion (que esta apalancada por delta y decae por
     theta, efectos que esto no modela).
  3. Reutiliza las funciones REALES de deteccion (analyze/detect_sma_turn/
     detect_opening_gap_fade via raw_hist) para que la logica de señal sea
     identica a la que corre en vivo -- no hay reimplementacion aparte que
     se pueda desincronizar.

Uso:
  python backtest.py                       # watchlist principal, 60 dias
  python backtest.py --tickers AAPL,GLD    # tickers especificos
"""

import argparse
from datetime import date
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf

from paper_trading.bollinger_strategy import (
    BB_PERIOD,
    MARKET_OPEN_BAR_TIME,
    SQUEEZE_LOOKBACK,
    analyze,
    detect_opening_gap_fade,
    detect_rsi_reversal,
    detect_sma_turn,
    detect_vwap_cross,
)
from paper_trading.engine import get_settings

MIN_LOOKBACK_BARS = BB_PERIOD + SQUEEZE_LOOKBACK + 10
DEFAULT_PERIOD = "60d"
DEFAULT_INTERVAL = "15m"


def backtest_ticker(symbol: str, period: str = DEFAULT_PERIOD, interval: str = DEFAULT_INTERVAL) -> list[dict]:
    hist = yf.Ticker(symbol).history(period=period, interval=interval, prepost=True)
    hist = hist.dropna(subset=["Open", "Close", "Volume"])
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
            bar_high = float(window["High"].iloc[-1])
            bar_low = float(window["Low"].iloc[-1])
            new_day = current_day != entry["entry_day"]

            if new_day:
                exit_price = entry["last_price_same_day"]
                pnl_pct = direction * (exit_price / entry["entry_price"] - 1) * 100
                trades.append({
                    "symbol": symbol, "strategy": entry["strategy"], "signal": entry["signal"],
                    "entry_time": entry["entry_time"], "exit_time": entry["last_bar_time_same_day"],
                    "entry_price": entry["entry_price"], "exit_price": exit_price,
                    "pnl_pct": pnl_pct, "win": pnl_pct > 0,
                    "mfe_pct": entry["mfe_pct"], "mae_pct": entry["mae_pct"],
                })
                open_trade = None
            else:
                favorable = direction * (bar_high / entry["entry_price"] - 1) * 100 if direction == 1 else \
                    direction * (bar_low / entry["entry_price"] - 1) * 100
                adverse = direction * (bar_low / entry["entry_price"] - 1) * 100 if direction == 1 else \
                    direction * (bar_high / entry["entry_price"] - 1) * 100
                entry["mfe_pct"] = max(entry["mfe_pct"], favorable)
                entry["mae_pct"] = min(entry["mae_pct"], adverse)
                entry["last_price_same_day"] = current_price
                entry["last_bar_time_same_day"] = current_bar_time
            continue

        try:
            sq = analyze(symbol, interval=interval, period=period, raw_hist=window)
        except Exception:
            sq = None
        try:
            turn = detect_sma_turn(symbol, interval=interval, period=period, raw_hist=window)
        except Exception:
            turn = None
        gap = None
        if current_bar_time.time() == MARKET_OPEN_BAR_TIME:
            try:
                gap = detect_opening_gap_fade(symbol, interval=interval, period=period, raw_hist=window)
            except Exception:
                gap = None
        try:
            vwap = detect_vwap_cross(symbol, interval=interval, period=period, raw_hist=window)
        except Exception:
            vwap = None
        try:
            rsi = detect_rsi_reversal(symbol, interval=interval, period=period, raw_hist=window)
        except Exception:
            rsi = None

        for candidate, strat_name in ((sq, "squeeze_breakout"), (turn, "giro_sma20"), (gap, "gap_fade_apertura"),
                                       (vwap, "vwap_cross"), (rsi, "rsi_reversal")):
            if candidate and candidate["signal"] != "none":
                open_trade = {
                    "strategy": strat_name, "signal": candidate["signal"],
                    "entry_time": current_bar_time, "entry_price": current_price,
                    "entry_day": current_day, "last_price_same_day": current_price,
                    "last_bar_time_same_day": current_bar_time,
                    "mfe_pct": 0.0, "mae_pct": 0.0,
                }
                break

    if open_trade is not None:
        entry = open_trade
        direction = 1 if entry["signal"] == "buy_call" else -1
        exit_price = entry["last_price_same_day"]
        pnl_pct = direction * (exit_price / entry["entry_price"] - 1) * 100
        trades.append({
            "symbol": symbol, "strategy": entry["strategy"], "signal": entry["signal"],
            "entry_time": entry["entry_time"], "exit_time": entry["last_bar_time_same_day"],
            "entry_price": entry["entry_price"], "exit_price": exit_price,
            "pnl_pct": pnl_pct, "win": pnl_pct > 0,
            "mfe_pct": entry["mfe_pct"], "mae_pct": entry["mae_pct"],
        })

    return trades


def summarize(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate": None, "avg_pnl_pct": None, "total_pnl_pct": None,
                "avg_mfe_pct": None, "avg_mae_pct": None}
    wins = sum(1 for t in trades if t["win"])
    total = sum(t["pnl_pct"] for t in trades)
    avg_mfe = sum(t.get("mfe_pct", 0) for t in trades) / n
    avg_mae = sum(t.get("mae_pct", 0) for t in trades) / n
    return {
        "n": n,
        "win_rate": round(wins / n * 100, 1),
        "avg_pnl_pct": round(total / n, 2),
        "total_pnl_pct": round(total, 2),
        "avg_mfe_pct": round(avg_mfe, 2),
        "avg_mae_pct": round(avg_mae, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="Backtest historico de las 3 estrategias")
    parser.add_argument("--tickers", default=None, help="Lista separada por comas (default: watchlist principal)")
    parser.add_argument("--period", default=DEFAULT_PERIOD)
    parser.add_argument("--interval", default=DEFAULT_INTERVAL)
    args = parser.parse_args()

    if args.tickers:
        symbols = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        symbols = get_settings()["watchlist"]

    print(f"Backtesteando {len(symbols)} tickers sobre {args.period} de velas {args.interval}: {', '.join(symbols)}\n")

    def _run(sym):
        try:
            return sym, backtest_ticker(sym, args.period, args.interval), None
        except Exception as e:
            return sym, [], e

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_run, symbols))

    all_trades = []
    for sym, trades, error in results:
        if error:
            print(f"[{sym}] ERROR: {error}")
            continue
        print(f"[{sym}] {len(trades)} operaciones simuladas")
        all_trades.extend(trades)

    print(f"\n{'='*70}\nRESULTADOS POR ESTRATEGIA (proxy sobre subyacente, no P&L real de opciones)\n{'='*70}")
    by_strategy: dict[str, list[dict]] = {}
    for t in all_trades:
        by_strategy.setdefault(t["strategy"], []).append(t)

    for strat in ("squeeze_breakout", "gap_fade_apertura", "giro_sma20", "vwap_cross", "rsi_reversal"):
        trades = by_strategy.get(strat, [])
        s = summarize(trades)
        print(f"\n{strat}:")
        print(f"  n={s['n']}  win_rate={s['win_rate']}%  avg_pnl_pct={s['avg_pnl_pct']}%  total_pnl_pct={s['total_pnl_pct']}%")
        print(f"  favorable maximo promedio: +{s['avg_mfe_pct']}%  |  adverso maximo promedio: {s['avg_mae_pct']}%")

    print(f"\n{'='*70}\nTOTAL COMBINADO: {summarize(all_trades)}")


if __name__ == "__main__":
    main()
