#!/usr/bin/env python3
"""
Backtest OOS de la regla "rebote de banda de Bollinger" (2026-10-05, a
pedido explicito del usuario, mostrada con lineas dibujadas a mano sobre
graficas reales: "ahi te estoy mostrando con una linea azul donde era que
tenias que entrar... a partir de ahora haz todas las entradas de esa
manera"): comprar CALL cuando el precio toca/perfora la banda inferior y
la vela cierra de vuelta adentro (rebote confirmado); espejo con la banda
superior para PUT.

Reutiliza SOLO la banda de Bollinger real (BB_PERIOD=20, BB_STD=2, igual
que paper_trading.bollinger_strategy) -- no reimplementa el indicador,
solo la regla de entrada nueva, para que el numero sea comparable.

Mide direccion del SUBYACENTE a 30/60 min (misma limitacion que el resto
de los backtests de este proyecto: no hay historico de opciones via
yfinance). Valida OOS: tickers en dos mitades + periodo en dos mitades,
exactamente igual metodologia que los backtests anteriores de esta sesion
-- una regla solo se adopta si el efecto se repite en las 4 particiones,
no solo en la que la "descubrio".

Uso:
  python bb_bounce_backtest.py
"""
import json
import statistics
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import time as dtime
from pathlib import Path

import pandas as pd
import yfinance as yf

BB_PERIOD = 20
BB_STD = 2

WATCHLIST = (
    "GOOGL,META,NVDA,COIN,V,QQQ,HOOD,SPY,TSLA,GLD,SOXL,SOXS,MARA,INTC,SMCI,NIO,AMC,PLTR,SOFI,F,SNAP,TQQQ,"
    "RIOT,MSTR,SQQQ,AAPL,AVGO,NFLX,LCID,AMD,MSFT,AMZN,JPM,XLF,XLE,XLK,UBER,SHOP,DIS,WMT,COST,CRM,QCOM,MU,IWM,NCLH"
).split(",")

MIN_BARS = 40
SESSION_FIRST_BAR = dtime(9, 30)
SESSION_LAST_BAR = dtime(15, 30)


def _hist(sym, period, interval, prepost):
    for attempt in range(3):
        try:
            h = yf.Ticker(sym).history(period=period, interval=interval, prepost=prepost)
            return h.dropna(subset=["Open", "High", "Low", "Close"])
        except Exception:
            time.sleep(2 * (attempt + 1))
    return pd.DataFrame()


def run_ticker(sym):
    try:
        h15 = _hist(sym, "60d", "15m", True)
        if len(h15) < MIN_BARS + 50:
            return sym, [], "sin datos suficientes"

        closes = h15["Close"]
        highs = h15["High"]
        lows = h15["Low"]
        mid = closes.rolling(BB_PERIOD).mean()
        sd = closes.rolling(BB_PERIOD).std(ddof=0)
        upper = mid + BB_STD * sd
        lower = mid - BB_STD * sd

        out = []
        seen = set()
        idx = h15.index
        for i in range(MIN_BARS, len(h15) - 4):
            ts = idx[i]
            if not (SESSION_FIRST_BAR <= ts.time() <= SESSION_LAST_BAR):
                continue
            key = ts.date()
            if key in seen:
                continue
            if pd.isna(lower.iloc[i]) or pd.isna(upper.iloc[i]):
                continue

            low_i, high_i, close_i = float(lows.iloc[i]), float(highs.iloc[i]), float(closes.iloc[i])
            lower_i, upper_i = float(lower.iloc[i]), float(upper.iloc[i])

            bounce_up = low_i <= lower_i and close_i > lower_i
            bounce_down = high_i >= upper_i and close_i < upper_i
            if not (bounce_up or bounce_down):
                continue
            # si pasan las dos a la vez (vela muy ancha) no es señal limpia -- se descarta
            if bounce_up and bounce_down:
                continue

            seen.add(key)
            d = 1 if bounce_up else -1
            entry_price = close_i
            r30 = d * (float(closes.iloc[i + 2]) / entry_price - 1) * 100 if i + 2 < len(h15) else None
            r60 = d * (float(closes.iloc[i + 4]) / entry_price - 1) * 100 if i + 4 < len(h15) else None

            out.append({
                "symbol": sym, "date": str(ts.date()), "signal": "buy_call" if bounce_up else "buy_put",
                "r30": r30, "r60": r60,
            })
        return sym, out, None
    except Exception as e:
        return sym, [], repr(e)


def _stat(vals):
    vals = [v for v in vals if v is not None]
    n = len(vals)
    if n == 0:
        return "n=0"
    m = statistics.mean(vals)
    win = sum(1 for v in vals if v > 0) / n * 100
    return f"n={n:<4} avg={m:+.3f}% win={win:.0f}%"


def report(sigs, label):
    calls = [s for s in sigs if s["signal"] == "buy_call"]
    puts = [s for s in sigs if s["signal"] == "buy_put"]
    print(f"\n{'='*78}\n{label}  (total señales: {len(sigs)})\n{'='*78}")
    print(f"  TODAS:        r30 {_stat([s['r30'] for s in sigs])} | r60 {_stat([s['r60'] for s in sigs])}")
    print(f"  CALL (banda inferior): r30 {_stat([s['r30'] for s in calls])} | r60 {_stat([s['r60'] for s in calls])}")
    print(f"  PUT  (banda superior): r30 {_stat([s['r30'] for s in puts])} | r60 {_stat([s['r60'] for s in puts])}")


def main():
    print(f"Backtest 'rebote de banda de Bollinger' sobre {len(WATCHLIST)} tickers...", flush=True)
    t0 = time.time()
    all_sigs = []
    with ProcessPoolExecutor(max_workers=6) as pool:
        for sym, sigs, err in pool.map(run_ticker, WATCHLIST):
            print(f"[{sym}] {len(sigs)} señales" + (f" ERROR: {err}" if err else "") + f"  t={time.time()-t0:.0f}s", flush=True)
            all_sigs.extend(sigs)

    Path("bb_bounce_resultados.json").write_text(json.dumps(all_sigs, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(all_sigs)} señales totales -> bb_bounce_resultados.json")

    report(all_sigs, "TODAS LAS SEÑALES (muestra completa)")

    tickers_sorted = sorted(set(s["symbol"] for s in all_sigs))
    half = len(tickers_sorted) // 2
    in_sample_tickers = set(tickers_sorted[:half])
    oos_tickers = set(tickers_sorted[half:])

    dates_sorted = sorted(set(s["date"] for s in all_sigs))
    mid_date = dates_sorted[len(dates_sorted) // 2] if dates_sorted else None

    report([s for s in all_sigs if s["symbol"] in in_sample_tickers], "IN-SAMPLE TICKERS (mitad A)")
    report([s for s in all_sigs if s["symbol"] in oos_tickers], "OUT-OF-SAMPLE TICKERS (mitad B, nunca vista)")
    if mid_date:
        report([s for s in all_sigs if s["date"] < mid_date], f"PRIMERA MITAD DEL PERIODO (antes de {mid_date})")
        report([s for s in all_sigs if s["date"] >= mid_date], f"SEGUNDA MITAD DEL PERIODO (desde {mid_date})")


if __name__ == "__main__":
    main()
