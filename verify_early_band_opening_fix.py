#!/usr/bin/env python3
"""
Verificacion real (2026-10-05, a pedido explicito: "prueba varias veces...
que sea 100% como quedo dicho") de la correccion agregada a
analyze_forming_bar(): EARLY_BAND_OPENING_MIN_RISE_PCT.

Para cada ticker, encuentra TODOS los momentos historicos reales donde
habia un squeeze confirmado (misma logica exacta que el motor: SQUEEZE_
LOOKBACK, SQUEEZE_PERCENTILE, SQUEEZE_MIN_BARS) y la barra siguiente toca
la banda -- el mismo punto donde "entrada temprana" evaluaria. Compara:

  REGLA VIEJA (antes de hoy): dejaba pasar CUALQUIER toque, sin mirar si
  la banda ya estaba abriendo.
  REGLA NUEVA (hoy): exige que el ancho de la ULTIMA vela del squeeze ya
  haya subido >= 5% sobre el minimo del squeeze (igual logica que se
  subio al motor real).

Mide el retorno real del SUBYACENTE a 30/60 min para los casos que la
regla nueva BLOQUEA vs los que SIGUE DEJANDO PASAR -- si la regla nueva
esta bien, los bloqueados deberian rendir peor (son los falsos quiebres
tipo NVDA) y los que pasan deberian rendir mejor o igual que antes.

Uso:
  python verify_early_band_opening_fix.py
"""
import json
import statistics
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

BB_PERIOD = 20
SQUEEZE_LOOKBACK = 100
SQUEEZE_PERCENTILE = 35
SQUEEZE_MIN_BARS = 2
EARLY_BAND_OPENING_MIN_RISE_PCT = 5.0

WATCHLIST = (
    "GOOGL,META,NVDA,COIN,V,QQQ,HOOD,SPY,TSLA,GLD,SOXL,SOXS,MARA,INTC,SMCI,NIO,AMC,PLTR,SOFI,F,SNAP,TQQQ,"
    "RIOT,MSTR,SQQQ,AAPL,AVGO,NFLX,LCID,AMD,MSFT,AMZN,JPM,XLF,XLE,XLK,UBER,SHOP,DIS,WMT,COST,CRM,QCOM,MU,IWM,NCLH"
).split(",")

MIN_BARS = BB_PERIOD + SQUEEZE_LOOKBACK
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
        if len(h15) < MIN_BARS + 20:
            return sym, [], "sin datos suficientes"

        closes = h15["Close"]; highs = h15["High"]; lows = h15["Low"]
        sma = closes.rolling(BB_PERIOD).mean()
        sd = closes.rolling(BB_PERIOD).std(ddof=0)
        upper = sma + 2 * sd
        lower = sma - 2 * sd
        width = (upper - lower) / sma * 100

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

            # "ultima vela cerrada" = bar i-1 (igual que closed.iloc[-1] en
            # analyze_forming_bar); "vela de ruptura/toque" = bar i (analogo
            # a la vela en formacion que toca la banda).
            prev = i - 1
            if prev < BB_PERIOD - 1 or pd.isna(width.iloc[prev]):
                continue
            lookback_widths = width.iloc[max(0, prev - SQUEEZE_LOOKBACK + 1):prev + 1].dropna()
            if len(lookback_widths) == 0:
                continue
            threshold = float(np.percentile(lookback_widths, SQUEEZE_PERCENTILE))

            consecutive = 0
            for j in range(prev, -1, -1):
                w = width.iloc[j]
                if pd.notna(w) and w <= threshold:
                    consecutive += 1
                else:
                    break
            if consecutive < SQUEEZE_MIN_BARS:
                continue

            upper_prev, lower_prev = float(upper.iloc[prev]), float(lower.iloc[prev])
            touch_up = float(highs.iloc[i]) >= upper_prev
            touch_down = float(lows.iloc[i]) <= lower_prev
            if not (touch_up or touch_down):
                continue
            if touch_up and touch_down:
                continue

            squeeze_run_widths = [float(width.iloc[j]) for j in range(prev, prev - consecutive, -1) if pd.notna(width.iloc[j])]
            squeeze_min = min(squeeze_run_widths)
            last_closed_width = float(width.iloc[prev])
            band_already_opening = last_closed_width > squeeze_min * (1 + EARLY_BAND_OPENING_MIN_RISE_PCT / 100)

            seen.add(key)
            d = 1 if touch_up else -1
            entry_price = float(closes.iloc[i])
            r30 = d * (float(closes.iloc[i + 2]) / entry_price - 1) * 100 if i + 2 < len(h15) else None
            r60 = d * (float(closes.iloc[i + 4]) / entry_price - 1) * 100 if i + 4 < len(h15) else None

            out.append({
                "symbol": sym, "date": str(ts.date()), "allowed_by_new_rule": band_already_opening,
                "squeeze_min_width": squeeze_min, "last_closed_width": last_closed_width,
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


def main():
    print(f"Verificacion de la correccion de apertura de banda sobre {len(WATCHLIST)} tickers...", flush=True)
    t0 = time.time()
    all_sigs = []
    with ProcessPoolExecutor(max_workers=6) as pool:
        for sym, sigs, err in pool.map(run_ticker, WATCHLIST):
            print(f"[{sym}] {len(sigs)} momentos de squeeze+toque" + (f" ERROR: {err}" if err else "") + f"  t={time.time()-t0:.0f}s", flush=True)
            all_sigs.extend(sigs)

    Path("verify_early_band_opening_resultados.json").write_text(json.dumps(all_sigs, ensure_ascii=False), encoding="utf-8")

    allowed = [s for s in all_sigs if s["allowed_by_new_rule"]]
    blocked = [s for s in all_sigs if not s["allowed_by_new_rule"]]
    print(f"\n{'='*80}\nTOTAL: {len(all_sigs)} momentos reales de squeeze+toque encontrados\n{'='*80}")
    print(f"\nANTES (regla vieja, dejaba pasar TODOS): r30 {_stat([s['r30'] for s in all_sigs])} | r60 {_stat([s['r60'] for s in all_sigs])}")
    print(f"\nAHORA -- PASA la regla nueva (banda ya abriendo, SI dispara): n={len(allowed)}")
    print(f"  r30 {_stat([s['r30'] for s in allowed])} | r60 {_stat([s['r60'] for s in allowed])}")
    print(f"\nAHORA -- BLOQUEADO por la regla nueva (como NVDA, NO dispara): n={len(blocked)}")
    print(f"  r30 {_stat([s['r30'] for s in blocked])} | r60 {_stat([s['r60'] for s in blocked])}")

    # 3 ejemplos reales bloqueados, para inspeccion humana (deberian parecerse a NVDA)
    print(f"\n{'='*80}\n3 EJEMPLOS REALES BLOQUEADOS (deberian parecerse al caso NVDA)\n{'='*80}")
    for s in blocked[:3]:
        print(f"  {s['symbol']} {s['date']}  ancho_min_squeeze={s['squeeze_min_width']:.3f}  "
              f"ancho_ultima_vela={s['last_closed_width']:.3f}  r30={s['r30']}  r60={s['r60']}")


if __name__ == "__main__":
    main()
