#!/usr/bin/env python3
"""
Backtest OOS: reemplazar la confirmacion de tendencia de 1h (pausada a
pedido del usuario, 2026-10-05) por "apertura de bandas de Bollinger" en
15m como filtro de liquidity_sweep.

Definicion operacional de "apertura de bandas" (se prueban dos variantes):
  F3_width_expanding  el ancho de banda (band_width_pct) de la barra anterior
                       a la señal es mayor que 4 barras atras -- las bandas
                       se estan abriendo (saliendo de squeeze), no contrayendo.
  F3b_width_and_side   ademas de abrirse, el cierre de la barra anterior esta
                       del lado de la banda a favor de la señal (igual que F1
                       del backtest de tendencia, pero combinado con apertura).

Mide TODAS las señales de liquidity_sweep (ya no se filtra por confianza de
1h -- esa confirmacion queda pausada). Reutiliza detect_liquidity_sweep REAL.
Compara direccion del subyacente a 30/60 min, igual metodologia y mismas
limitaciones que liquidity_sweep_trend_filter_backtest.py (hermano de este
script). Valida OOS: tickers en dos mitades + periodo en dos mitades.

Uso:
  python liquidity_sweep_band_opening_backtest.py
"""
import json
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import time as dtime
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from paper_trading.bollinger_strategy import BB_PERIOD, BB_STD, detect_liquidity_sweep  # noqa: E402

WATCHLIST = (
    "GOOGL,META,NVDA,COIN,V,QQQ,HOOD,SPY,TSLA,GLD,SOXL,SOXS,MARA,INTC,SMCI,NIO,AMC,PLTR,SOFI,F,SNAP,TQQQ,"
    "RIOT,MSTR,SQQQ,AAPL,AVGO,NFLX,LCID,AMD,MSFT,AMZN,JPM,XLF,XLE,XLK,UBER,SHOP,DIS,WMT,COST,CRM,QCOM,MU,IWM,NCLH"
).split(",")

MIN_BARS = 80
SESSION_FIRST_BAR = dtime(9, 30)
SESSION_LAST_BAR = dtime(15, 30)
WIDTH_LOOKBACK = 4


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
        mid = closes.rolling(BB_PERIOD).mean()
        sd = closes.rolling(BB_PERIOD).std(ddof=0)
        upper, lower = mid + BB_STD * sd, mid - BB_STD * sd
        width_pct = (upper - lower) / mid * 100

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
            window = h15.iloc[: i + 1]
            try:
                r = detect_liquidity_sweep(sym, interval="15m", period="60d", raw_hist=window)
            except Exception:
                continue
            if r["signal"] not in ("buy_call", "buy_put"):
                continue
            seen.add(key)
            d = 1 if r["signal"] == "buy_call" else -1

            w_prev = width_pct.iloc[i - 1]
            w_ref = width_pct.iloc[i - 1 - WIDTH_LOOKBACK]
            mid_prev, close_prev = mid.iloc[i - 1], closes.iloc[i - 1]
            if pd.isna(w_prev) or pd.isna(w_ref) or pd.isna(mid_prev):
                continue

            opening = w_prev > w_ref  # las bandas se estan abriendo (no en squeeze/contrayendo)
            side_ok = (d == 1 and close_prev >= mid_prev) or (d == -1 and close_prev <= mid_prev)
            f3_width_expanding_pass = opening
            f3b_width_and_side_pass = opening and side_ok

            entry_price = float(closes.iloc[i])
            r30 = d * (float(closes.iloc[i + 2]) / entry_price - 1) * 100 if i + 2 < len(h15) else None
            r60 = d * (float(closes.iloc[i + 4]) / entry_price - 1) * 100 if i + 4 < len(h15) else None

            out.append({
                "symbol": sym, "date": str(ts.date()), "signal": r["signal"],
                "f3_pass": bool(f3_width_expanding_pass), "f3b_pass": bool(f3b_width_and_side_pass),
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
    print(f"\n{'='*78}\n{label}  (total señales SIN filtro de 1h: {len(sigs)})\n{'='*78}")
    base = sigs
    print(f"  BASE (toda señal de liquidity_sweep, sin ningun filtro): r30 {_stat([s['r30'] for s in base])} | r60 {_stat([s['r60'] for s in base])}")
    for fkey, fname in (("f3_pass", "F3 bandas abriendose (ancho > hace 4 barras)"),
                         ("f3b_pass", "F3b bandas abriendose + lado correcto de la media")):
        kept = [s for s in sigs if s[fkey]]
        blocked = [s for s in sigs if not s[fkey]]
        print(f"\n{fname}")
        print(f"  PASA el filtro (se operaria): r30 {_stat([s['r30'] for s in kept])} | r60 {_stat([s['r60'] for s in kept])}")
        print(f"  NO pasa (se descarta):        r30 {_stat([s['r30'] for s in blocked])} | r60 {_stat([s['r60'] for s in blocked])}")


def main():
    print(f"Backtest 'apertura de bandas' como filtro de liquidity_sweep (sin confirmacion de 1h) sobre {len(WATCHLIST)} tickers...", flush=True)
    t0 = time.time()
    all_sigs = []
    with ProcessPoolExecutor(max_workers=6) as pool:
        for sym, sigs, err in pool.map(run_ticker, WATCHLIST):
            print(f"[{sym}] {len(sigs)} señales" + (f" ERROR: {err}" if err else "") + f"  t={time.time()-t0:.0f}s", flush=True)
            all_sigs.extend(sigs)

    Path("liquidity_sweep_band_opening_resultados.json").write_text(json.dumps(all_sigs, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(all_sigs)} señales totales -> liquidity_sweep_band_opening_resultados.json")

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
