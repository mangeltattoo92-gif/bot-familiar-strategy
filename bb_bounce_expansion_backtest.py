#!/usr/bin/env python3
"""
Backtest OOS de la regla REFINADA (2026-10-05, aclaracion explicita del
usuario tras el primer backtest: "no es un punto exacto es el momento
donde las bandas bolinger estan abriendo con amplitud y es el momento en
el que la vela toca la banda... ese es el momento correcto y exacto"):

  señal = la vela TOCA (perfora) una banda  Y  las bandas se estan
  ABRIENDO (ancho actual > ancho de hace K barras, no apretadas/quietas)

A diferencia del primer backtest (bb_bounce_backtest.py, que asumia
"toca banda inferior -> rebote hacia arriba" y no tuvo edge), este NO
asume la direccion -- mide las DOS lecturas posibles para cada toque:
  - CONTINUACION: tocar banda inferior con bandas abriendose = la banda
    se esta yendo para abajo con el precio -> apostar a que SIGUE bajando
    (PUT), y simetrico arriba (CALL si toca banda superior abriendose).
  - REBOTE: tocar banda inferior con bandas abriendose = apostar a que
    rebota para arriba (CALL), y simetrico (PUT si toca banda superior).
Se reportan las dos por separado, en las 4 particiones OOS de siempre,
para que los datos digan cual direccion (si alguna) tiene edge real.

Uso:
  python bb_bounce_expansion_backtest.py
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
WIDTH_LOOKBACK = 4  # misma ventana ya usada en el test de "apertura de bandas" de liquidity_sweep

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

        closes = h15["Close"]; highs = h15["High"]; lows = h15["Low"]
        mid = closes.rolling(BB_PERIOD).mean()
        sd = closes.rolling(BB_PERIOD).std(ddof=0)
        upper = mid + BB_STD * sd
        lower = mid - BB_STD * sd
        width = (upper - lower) / mid * 100

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
            if pd.isna(lower.iloc[i]) or pd.isna(width.iloc[i - WIDTH_LOOKBACK]):
                continue

            low_i, high_i, close_i = float(lows.iloc[i]), float(highs.iloc[i]), float(closes.iloc[i])
            lower_i, upper_i = float(lower.iloc[i]), float(upper.iloc[i])
            opening = width.iloc[i] > width.iloc[i - WIDTH_LOOKBACK]

            touch_lower = low_i <= lower_i
            touch_upper = high_i >= upper_i
            if not opening or not (touch_lower or touch_upper):
                continue
            if touch_lower and touch_upper:
                continue

            seen.add(key)
            entry_price = close_i
            touch = "lower" if touch_lower else "upper"
            r30 = None if i + 2 >= len(h15) else float(closes.iloc[i + 2]) / entry_price - 1
            r60 = None if i + 4 >= len(h15) else float(closes.iloc[i + 4]) / entry_price - 1
            r30 = r30 * 100 if r30 is not None else None
            r60 = r60 * 100 if r60 is not None else None

            out.append({"symbol": sym, "date": str(ts.date()), "touch": touch, "r30_raw": r30, "r60_raw": r60})
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
    print(f"\n{'='*86}\n{label}  (total toques con bandas abriendose: {len(sigs)})\n{'='*86}")
    for touch, touch_name in (("lower", "TOCA BANDA INFERIOR (abriendose)"), ("upper", "TOCA BANDA SUPERIOR (abriendose)")):
        pool = [s for s in sigs if s["touch"] == touch]
        if not pool:
            print(f"\n{touch_name}: sin señales")
            continue
        cont_sign = 1 if touch == "lower" else -1   # continuacion = seguir en la direccion de la apertura
        bounce_sign = -cont_sign                     # rebote = direccion contraria
        cont30 = [s["r30_raw"] * cont_sign if s["r30_raw"] is not None else None for s in pool]
        cont60 = [s["r60_raw"] * cont_sign if s["r60_raw"] is not None else None for s in pool]
        bnc30 = [s["r30_raw"] * bounce_sign if s["r30_raw"] is not None else None for s in pool]
        bnc60 = [s["r60_raw"] * bounce_sign if s["r60_raw"] is not None else None for s in pool]
        print(f"\n{touch_name}  n={len(pool)}")
        print(f"  CONTINUACION (seguir cayendo/subiendo con la apertura): r30 {_stat(cont30)} | r60 {_stat(cont60)}")
        print(f"  REBOTE       (volver para el otro lado):                r30 {_stat(bnc30)} | r60 {_stat(bnc60)}")


def main():
    print(f"Backtest 'toque de banda CON apertura/expansion' sobre {len(WATCHLIST)} tickers...", flush=True)
    t0 = time.time()
    all_sigs = []
    with ProcessPoolExecutor(max_workers=6) as pool:
        for sym, sigs, err in pool.map(run_ticker, WATCHLIST):
            print(f"[{sym}] {len(sigs)} señales" + (f" ERROR: {err}" if err else "") + f"  t={time.time()-t0:.0f}s", flush=True)
            all_sigs.extend(sigs)

    Path("bb_bounce_expansion_resultados.json").write_text(json.dumps(all_sigs, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(all_sigs)} señales totales -> bb_bounce_expansion_resultados.json")

    report(all_sigs, "TODAS LAS SEÑALES (muestra completa)")

    tickers_sorted = sorted(set(s["symbol"] for s in all_sigs))
    half = len(tickers_sorted) // 2
    in_sample = set(tickers_sorted[:half])
    oos = set(tickers_sorted[half:])
    dates_sorted = sorted(set(s["date"] for s in all_sigs))
    mid_date = dates_sorted[len(dates_sorted) // 2] if dates_sorted else None

    report([s for s in all_sigs if s["symbol"] in in_sample], "IN-SAMPLE TICKERS (mitad A)")
    report([s for s in all_sigs if s["symbol"] in oos], "OUT-OF-SAMPLE TICKERS (mitad B, nunca vista)")
    if mid_date:
        report([s for s in all_sigs if s["date"] < mid_date], f"PRIMERA MITAD DEL PERIODO (antes de {mid_date})")
        report([s for s in all_sigs if s["date"] >= mid_date], f"SEGUNDA MITAD DEL PERIODO (desde {mid_date})")


if __name__ == "__main__":
    main()
