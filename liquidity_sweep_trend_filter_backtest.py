#!/usr/bin/env python3
"""
Backtest OOS del filtro propuesto para liquidity_sweep (2026-10-05, a pedido
del usuario tras el caso real NCLH: entro BUY_CALL con confianza alta de 1h
pero el SMA20 de 15m ya venia roto a la baja un par de barras antes de la
señal -- perdio -22%).

Hipotesis: aunque la tendencia de 1h (ultima vela CERRADA) de confirmation_for_signal
diga "alta", el 15m puede ya estar mostrando el giro contrario en las barras
justo antes de la señal. Se prueban dos filtros adicionales, evaluados SOLO
sobre señales que ya pasan el filtro real en vivo (confianza alta de 1h):

  F1_mid_pos   bloquear si el CIERRE de la barra anterior a la señal ya esta
               del lado contrario de la SMA20 de 15m (ej. buy_call bloqueado
               si close[i-1] < sma20[i-1])
  F2_mid_slope bloquear si la SMA20 de 15m viene cayendo (buy_call) o subiendo
               (buy_put) en las ultimas 4 barras antes de la señal

Reutiliza detect_liquidity_sweep / confirmation_for_signal / get_trend REALES
(misma logica que vivo). Mide direccion del SUBYACENTE a 30/60 min (no P&L de
opcion real, mismas limitaciones que backtest.py / backtest_confirmacion.py).

Validacion OOS pedida por el usuario: separa tickers en dos mitades (alfabetico,
"in-sample" / "out-of-sample") y el periodo de 60 dias en dos mitades de fecha,
reporta las 4 combinaciones por separado -- una regla solo se adopta si el
efecto se repite en las 4, no solo en el set que la "descubrio".

Uso:
  python liquidity_sweep_trend_filter_backtest.py
"""
import json
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import time as dtime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from paper_trading.bollinger_strategy import (  # noqa: E402
    BB_PERIOD, confirmation_for_signal, detect_liquidity_sweep, get_trend,
)

WATCHLIST = (
    "GOOGL,META,NVDA,COIN,V,QQQ,HOOD,SPY,TSLA,GLD,SOXL,SOXS,MARA,INTC,SMCI,NIO,AMC,PLTR,SOFI,F,SNAP,TQQQ,"
    "RIOT,MSTR,SQQQ,AAPL,AVGO,NFLX,LCID,AMD,MSFT,AMZN,JPM,XLF,XLE,XLK,UBER,SHOP,DIS,WMT,COST,CRM,QCOM,MU,IWM,NCLH"
).split(",")

MIN_BARS = 80  # suficiente para BB_PERIOD(20) + lookback del sweep(30) + swing k(3) + margen
SESSION_FIRST_BAR = dtime(9, 30)
SESSION_LAST_BAR = dtime(15, 30)
SMA_SLOPE_LOOKBACK = 4


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
        h1h = _hist(sym, "3mo", "1h", True)
        if len(h15) < MIN_BARS + 50:
            return sym, [], "sin datos suficientes"

        closes = h15["Close"]
        sma20 = closes.rolling(BB_PERIOD).mean()

        out = []
        seen = set()
        idx = h15.index
        for i in range(MIN_BARS, len(h15) - 4):  # -4: deja margen para medir forward
            ts = idx[i]
            if not (SESSION_FIRST_BAR <= ts.time() <= SESSION_LAST_BAR):
                continue
            key = ts.date()
            if key in seen:
                continue  # 1 señal por ticker/dia, igual que backtest_confirmacion
            window = h15.iloc[: i + 1]
            try:
                r = detect_liquidity_sweep(sym, interval="15m", period="60d", raw_hist=window)
            except Exception:
                continue
            if r["signal"] not in ("buy_call", "buy_put"):
                continue

            T = ts
            closed_1h = h1h[h1h.index + timedelta(hours=1) <= T] if len(h1h) else h1h
            trend = get_trend(sym, "1h", "3mo", raw_hist=closed_1h) if len(closed_1h) else None
            conf = confirmation_for_signal(r["signal"], {"1h": trend})["confidence"]
            if conf != "alta":
                continue  # misma regla que ya corre en vivo: solo 'alta' llega a operarse

            seen.add(key)
            d = 1 if r["signal"] == "buy_call" else -1

            mid_prev = sma20.iloc[i - 1]
            close_prev = closes.iloc[i - 1]
            mid_slope_prev = sma20.iloc[i - 1] - sma20.iloc[i - 1 - SMA_SLOPE_LOOKBACK]
            if pd.isna(mid_prev) or pd.isna(mid_slope_prev):
                continue

            f1_blocked = (d == 1 and close_prev < mid_prev) or (d == -1 and close_prev > mid_prev)
            f2_blocked = (d == 1 and mid_slope_prev < 0) or (d == -1 and mid_slope_prev > 0)

            entry_price = float(closes.iloc[i])
            r30 = d * (float(closes.iloc[i + 2]) / entry_price - 1) * 100 if i + 2 < len(h15) else None
            r60 = d * (float(closes.iloc[i + 4]) / entry_price - 1) * 100 if i + 4 < len(h15) else None

            out.append({
                "symbol": sym, "date": str(ts.date()), "signal": r["signal"],
                "f1_blocked": bool(f1_blocked), "f2_blocked": bool(f2_blocked),
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
    print(f"\n{'='*78}\n{label}  (total señales confianza-alta: {len(sigs)})\n{'='*78}")
    for fkey, fname in (("f1_blocked", "F1 posicion vs SMA20 (barra anterior)"),
                         ("f2_blocked", "F2 pendiente de SMA20 (4 barras)")):
        kept = [s for s in sigs if not s[fkey]]
        blocked = [s for s in sigs if s[fkey]]
        print(f"\n{fname}")
        print(f"  MANTENIDAS (filtro de acuerdo con la señal): r30 {_stat([s['r30'] for s in kept])} | r60 {_stat([s['r60'] for s in kept])}")
        print(f"  BLOQUEADAS (filtro en contra, se descartarian): r30 {_stat([s['r30'] for s in blocked])} | r60 {_stat([s['r60'] for s in blocked])}")


def main():
    print(f"Backtest filtro de tendencia 15m para liquidity_sweep sobre {len(WATCHLIST)} tickers...", flush=True)
    t0 = time.time()
    all_sigs = []
    with ProcessPoolExecutor(max_workers=6) as pool:
        for sym, sigs, err in pool.map(run_ticker, WATCHLIST):
            print(f"[{sym}] {len(sigs)} señales confianza-alta" + (f" ERROR: {err}" if err else "") + f"  t={time.time()-t0:.0f}s", flush=True)
            all_sigs.extend(sigs)

    Path("liquidity_sweep_trend_filter_resultados.json").write_text(json.dumps(all_sigs, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(all_sigs)} señales totales -> liquidity_sweep_trend_filter_resultados.json")

    report(all_sigs, "TODAS LAS SEÑALES (muestra completa)")

    tickers_sorted = sorted(set(s["symbol"] for s in all_sigs))
    half = len(tickers_sorted) // 2
    in_sample_tickers = set(tickers_sorted[:half])
    oos_tickers = set(tickers_sorted[half:])

    dates_sorted = sorted(set(s["date"] for s in all_sigs))
    if dates_sorted:
        mid_date = dates_sorted[len(dates_sorted) // 2]
    else:
        mid_date = None

    report([s for s in all_sigs if s["symbol"] in in_sample_tickers], "IN-SAMPLE TICKERS (mitad A, descubrimiento)")
    report([s for s in all_sigs if s["symbol"] in oos_tickers], "OUT-OF-SAMPLE TICKERS (mitad B, nunca vista)")
    if mid_date:
        report([s for s in all_sigs if s["date"] < mid_date], f"PRIMERA MITAD DEL PERIODO (antes de {mid_date})")
        report([s for s in all_sigs if s["date"] >= mid_date], f"SEGUNDA MITAD DEL PERIODO (desde {mid_date})")


if __name__ == "__main__":
    main()
