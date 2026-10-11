#!/usr/bin/env python3
"""
Backtest de CONFIRMACION intra-vela -- 2026-09-18, a pedido del usuario tras
el caso MU: la señal salio al cierre de la vela de 15m y se entro justo
cuando arrancaba una vela que se fue en contra. Pregunta: si en vez de
entrar al cierre de la vela de señal se espera una confirmacion dentro de la
vela SIGUIENTE (usando velas de 5m y de 1m), ¿mejora el resultado?

Reutiliza las funciones REALES de deteccion (analyze / detect_sma_turn) y la
misma confianza por tendencia de 1h que usa el bot en vivo, solo para
squeeze_breakout y giro_sma20 (las dos estrategias activas). Se aproxima el
filtro de horario de giro_sma20 (solo hasta 10:30 ET). NO reproduce el resto
de filtros en vivo (sesgo diario, noticias, cooldown, contrato/spread).

Variantes de entrada (todas miden desde SU PROPIO precio de entrada):
  base                  entrar al cierre de la vela de señal (como hoy)
  v1_primer_sub         esperar la 1ra sub-vela de la vela siguiente; entrar
                        solo si cierra a favor del cierre de la vela de señal
  v2_sostenido_10m      entrar tras 10 min solo si NINGUNA sub-vela cerro en
                        contra del cierre de la vela de señal
  v3_ruptura_extremo    entrar solo si dentro de los 15 min siguientes el
                        precio rompe el maximo (call) / minimo (put) de la
                        vela de señal, al precio de esa ruptura

LIMITACIONES: mide el SUBYACENTE (no la prima; no hay historico de opciones),
sin los stops/objetivos reales; 5m = ultimos ~60 dias, 1m = solo ~7 dias
(muestra chica). Se toma solo la primera señal por ticker/dia/estrategia.

Uso:
  python backtest_confirmacion.py
  python backtest_confirmacion.py --tickers MU,CRM --skip-1m
"""
import argparse
import json
import math
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
    BB_PERIOD, SQUEEZE_LOOKBACK, analyze, confirmation_for_signal, detect_sma_turn, get_trend,
)

WATCHLIST = (
    "GOOGL,META,NVDA,COIN,V,QQQ,HOOD,SPY,TSLA,GLD,SOXL,SOXS,MARA,INTC,SMCI,NIO,AMC,PLTR,SOFI,F,SNAP,TQQQ,"
    "RIOT,MSTR,SQQQ,AAPL,AVGO,NFLX,LCID,AMD,MSFT,AMZN,JPM,XLF,XLE,XLK,UBER,SHOP,DIS,WMT,COST,CRM,QCOM,MU,IWM"
)
MIN_LOOKBACK_BARS = BB_PERIOD + SQUEEZE_LOOKBACK + 10
WINDOW_CAP_BARS = 1400  # ~1 mes de velas de 15m con premarket, como el period="1mo" en vivo
SESSION_FIRST_BAR = dtime(9, 30)
SESSION_LAST_BAR = dtime(15, 30)
GIRO_WINDOW_END = dtime(10, 30)
VARIANTS = ("base", "v1_primer_sub", "v2_sostenido_10m", "v3_ruptura_extremo")
HORIZONS = (30, 60)


def _hist(sym, period, interval, prepost):
    for attempt in range(3):
        try:
            h = yf.Ticker(sym).history(period=period, interval=interval, prepost=prepost)
            return h.dropna(subset=["Open", "High", "Low", "Close"])
        except Exception:
            time.sleep(2 * (attempt + 1))
    return pd.DataFrame()


def _gen_signals(sym, h15, h1h):
    out = []
    seen = set()
    idx = h15.index
    for i in range(MIN_LOOKBACK_BARS, len(h15) - 1):
        ts = idx[i]
        if not (SESSION_FIRST_BAR <= ts.time() <= SESSION_LAST_BAR):
            continue
        T = ts + timedelta(minutes=15)
        window = h15.iloc[max(0, i - WINDOW_CAP_BARS + 1): i + 1]
        for fn, strat in ((analyze, "squeeze_breakout"), (detect_sma_turn, "giro_sma20")):
            if strat == "giro_sma20" and T.time() > GIRO_WINDOW_END:
                continue
            key = (ts.date(), strat)
            if key in seen:
                continue
            try:
                r = fn(sym, interval="15m", period="1mo", raw_hist=window)
            except Exception:
                continue
            if r.get("signal") not in ("buy_call", "buy_put"):
                continue
            seen.add(key)
            closed_1h = h1h[h1h.index + timedelta(hours=1) <= T] if len(h1h) else h1h
            trend = get_trend(sym, "1h", "3mo", raw_hist=closed_1h) if len(closed_1h) else None
            conf = confirmation_for_signal(r["signal"], {"1h": trend})["confidence"]
            bar = h15.iloc[i]
            nxt = h15.iloc[i + 1] if idx[i + 1] == T else None
            out.append({
                "symbol": sym, "strategy": strat, "signal": r["signal"], "confidence": conf,
                "T": T, "sig_close": float(bar["Close"]), "sig_high": float(bar["High"]),
                "sig_low": float(bar["Low"]),
                "next15_against": None if nxt is None else (
                    (float(nxt["Close"]) < float(nxt["Open"])) if r["signal"] == "buy_call"
                    else (float(nxt["Close"]) > float(nxt["Open"]))),
            })
    return out


def _outcome(day, te, pe, d, sub_min):
    after = day[day.index >= te]
    if after.empty:
        return None
    res = {"pe": pe}
    for h in HORIZONS:
        end = te + timedelta(minutes=h)
        w = after[after.index + timedelta(minutes=sub_min) <= end]
        used = w if not w.empty else after
        px = float(used["Close"].iloc[-1])
        res[f"r{h}"] = d * (px / pe - 1) * 100
        if h == 30:
            if d == 1:
                res["mae30"] = (float(used["Low"].min()) / pe - 1) * 100
                res["mfe30"] = (float(used["High"].max()) / pe - 1) * 100
            else:
                res["mae30"] = -(float(used["High"].max()) / pe - 1) * 100
                res["mfe30"] = -(float(used["Low"].min()) / pe - 1) * 100
    res["reod"] = d * (float(after["Close"].iloc[-1]) / pe - 1) * 100
    return res


def _evaluate(sig, days, sub_min):
    d = 1 if sig["signal"] == "buy_call" else -1
    T = sig["T"]
    day = days.get(T.date())
    if day is None:
        return None
    nxt = day[(day.index >= T) & (day.index < T + timedelta(minutes=15))]
    if len(nxt) < 15 // sub_min:
        return None
    sc = sig["sig_close"]
    ev = {"base": _outcome(day, T, sc, d, sub_min)}

    first = nxt.iloc[0]
    if d * (float(first["Close"]) - sc) > 0:
        ev["v1_primer_sub"] = _outcome(day, first.name + timedelta(minutes=sub_min), float(first["Close"]), d, sub_min)
    else:
        ev["v1_primer_sub"] = None

    n10 = nxt[nxt.index < T + timedelta(minutes=10)]
    if len(n10) and all(d * (float(c) - sc) > 0 for c in n10["Close"]):
        last = n10.iloc[-1]
        ev["v2_sostenido_10m"] = _outcome(day, last.name + timedelta(minutes=sub_min), float(last["Close"]), d, sub_min)
    else:
        ev["v2_sostenido_10m"] = None

    ev["v3_ruptura_extremo"] = None
    for ts, b in nxt.iterrows():
        if d == 1 and float(b["High"]) > sig["sig_high"]:
            px = max(sig["sig_high"], float(b["Open"]))
        elif d == -1 and float(b["Low"]) < sig["sig_low"]:
            px = min(sig["sig_low"], float(b["Open"]))
        else:
            continue
        ev["v3_ruptura_extremo"] = _outcome(day, ts + timedelta(minutes=sub_min), px, d, sub_min)
        break
    return ev


def _by_day(df):
    return {d: g for d, g in df.groupby(df.index.date)} if len(df) else {}


def run_ticker(args):
    sym, skip_1m, overrides = args
    try:
        import paper_trading.bollinger_strategy as bs

        overrides = dict(overrides)
        regular_only = overrides.pop("__regular_only__", False)
        if overrides.pop("__ema_bands__", False):
            def _bollinger_ema(closes):
                mid = closes.ewm(span=bs.BB_PERIOD, adjust=False).mean()
                std = closes.rolling(bs.BB_PERIOD).std(ddof=0)
                upper, lower = mid + bs.BB_STD * std, mid - bs.BB_STD * std
                return mid, upper, lower, (upper - lower) / mid * 100

            bs._bollinger = _bollinger_ema
        for name, value in overrides.items():
            setattr(bs, name, value)
        h15 = _hist(sym, "60d", "15m", not regular_only)
        h1h = _hist(sym, "3mo", "1h", True)
        h5 = _hist(sym, "60d", "5m", False)
        h1 = pd.DataFrame() if skip_1m else _hist(sym, "7d", "1m", False)
        if len(h15) < MIN_LOOKBACK_BARS + 50 or h5.empty:
            return sym, [], "sin datos suficientes"
        d5, d1 = _by_day(h5), _by_day(h1)
        sigs = _gen_signals(sym, h15, h1h)
        for s in sigs:
            s["ev5"] = _evaluate(s, d5, 5)
            s["ev1"] = _evaluate(s, d1, 1) if d1 else None
            s["T"] = s["T"].isoformat()
        return sym, sigs, None
    except Exception as e:
        return sym, [], repr(e)


def _stat(vals):
    n = len(vals)
    if n == 0:
        return None
    m = sum(vals) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1)) if n > 1 else 0.0
    return m, (1.96 * sd / math.sqrt(n) if n > 1 else float("nan"))


def _fmt(vals, pct_pos=False):
    if pct_pos:
        return f"{sum(1 for v in vals if v > 0) / len(vals) * 100:5.1f}%" if vals else "   -  "
    st = _stat(vals)
    return "     -     " if st is None else f"{st[0]:+.3f}±{st[1]:.3f}"


def report(sigs, key, label):
    print(f"\n{'=' * 100}\n{label}\n{'=' * 100}")
    groups = (
        ("squeeze_breakout | confianza media+alta", lambda s: s["strategy"] == "squeeze_breakout" and s["confidence"] in ("media", "alta")),
        ("squeeze_breakout | solo alta", lambda s: s["strategy"] == "squeeze_breakout" and s["confidence"] == "alta"),
        ("giro_sma20 | confianza media+alta (hasta 10:30 ET)", lambda s: s["strategy"] == "giro_sma20" and s["confidence"] in ("media", "alta")),
        ("giro_sma20 | solo alta", lambda s: s["strategy"] == "giro_sma20" and s["confidence"] == "alta"),
        ("AMBAS | media+alta", lambda s: s["confidence"] in ("media", "alta")),
    )
    for gname, cond in groups:
        pool = [s for s in sigs if s.get(key) and cond(s)]
        if not pool:
            print(f"\n{gname}: sin señales")
            continue
        against = [s["next15_against"] for s in pool if s["next15_against"] is not None]
        print(f"\n{gname}: {len(pool)} señales (la vela de 15m siguiente cerro EN CONTRA en "
              f"{sum(against) / len(against) * 100:.0f}% de ellas)")
        print(f"  {'variante':<20}{'n':>5}{'%tom':>6} | {'ret30 medio±IC95':>17}{'win30':>8} | {'ret60':>15}{'win60':>8} | "
              f"{'ret hasta cierre':>17}{'winEOD':>8} | {'MAE30':>8}")
        base_pool = [s[key]["base"] for s in pool if s[key].get("base")]
        for v in VARIANTS:
            taken = [s[key][v] for s in pool if s[key].get(v)]
            if not taken:
                print(f"  {v:<20}{0:>5}")
                continue
            r30 = [t["r30"] for t in taken]
            r60 = [t["r60"] for t in taken]
            re = [t["reod"] for t in taken]
            mae = [t["mae30"] for t in taken]
            print(f"  {v:<20}{len(taken):>5}{len(taken) / len(base_pool) * 100:>5.0f}% | {_fmt(r30):>17}{_fmt(r30, True):>8} | "
                  f"{_fmt(r60):>15}{_fmt(r60, True):>8} | {_fmt(re):>17}{_fmt(re, True):>8} | {sum(mae) / len(mae):>+7.3f}%")
        for v in VARIANTS[1:]:
            skipped = [s[key]["base"] for s in pool if s[key].get("base") and not s[key].get(v)]
            if skipped:
                print(f"    omitidas por {v}: n={len(skipped)} | base ret30 {_fmt([t['r30'] for t in skipped])} "
                      f"win30 {_fmt([t['r30'] for t in skipped], True)} | ret EOD {_fmt([t['reod'] for t in skipped])}")


def main():
    ap = argparse.ArgumentParser(description="Backtest de confirmacion intra-vela")
    ap.add_argument("--tickers", default=None)
    ap.add_argument("--skip-1m", action="store_true")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--out", default=str(ROOT / "backtest_confirmacion_resultados.json"))
    ap.add_argument("--squeeze-percentile", type=float, default=None, help="prueba otro SQUEEZE_PERCENTILE (hoy 35)")
    ap.add_argument("--squeeze-min-bars", type=int, default=None, help="prueba otro SQUEEZE_MIN_BARS (hoy 2)")
    ap.add_argument("--giro-ratio", type=float, default=None, help="prueba otro SMA_TURN_MIN_MAGNITUDE_RATIO (hoy 1.1)")
    ap.add_argument("--regular-only", action="store_true", help="velas de 15m SOLO de horario regular (sin premarket), como el grafico por defecto de Robinhood")
    ap.add_argument("--ema-bands", action="store_true", help="bandas como Robinhood: EMA20 +- 2 desviaciones (en vez de SMA20)")
    args = ap.parse_args()
    overrides = {}
    if args.ema_bands:
        overrides["__ema_bands__"] = True
    if args.regular_only:
        overrides["__regular_only__"] = True
    if args.squeeze_percentile is not None:
        overrides["SQUEEZE_PERCENTILE"] = args.squeeze_percentile
    if args.squeeze_min_bars is not None:
        overrides["SQUEEZE_MIN_BARS"] = args.squeeze_min_bars
    if args.giro_ratio is not None:
        overrides["SMA_TURN_MIN_MAGNITUDE_RATIO"] = args.giro_ratio
    symbols = [t.strip().upper() for t in (args.tickers or WATCHLIST).split(",") if t.strip()]
    print(f"Backtest de confirmacion sobre {len(symbols)} tickers ... umbrales alterados: {overrides or 'ninguno (los de hoy)'}", flush=True)

    all_sigs = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for sym, sigs, err in pool.map(run_ticker, [(s, args.skip_1m, overrides) for s in symbols]):
            print(f"[{sym}] {len(sigs)} señales" + (f" (ERROR: {err})" if err else "") + f"  t={time.time() - t0:.0f}s", flush=True)
            all_sigs.extend(sigs)
    Path(args.out).write_text(json.dumps(all_sigs, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(all_sigs)} señales totales -> {args.out}")
    report(all_sigs, "ev5", "SUB-VELAS DE 5m (ultimos ~60 dias)")
    if not args.skip_1m:
        report(all_sigs, "ev1", "SUB-VELAS DE 1m (ultimos ~7 dias -- muestra CHICA)")


if __name__ == "__main__":
    main()
