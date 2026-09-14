#!/usr/bin/env python3
"""
Vigia de "casi-señales" -- corre en segundo plano de forma continua
(pura Python, SIN costo de tokens/LLM por chequeo) y detecta cuando un
ticker de la watchlist esta CERCA de disparar squeeze_breakout o
giro_sma20, aunque todavia no cumpla del todo las condiciones.

Objetivo: en vez de que cada ciclo de 5 minutos (que si cuesta tokens,
porque lo corre el agente) tenga que releer la salida completa de las 30
tickers -> genera un snapshot corto (data/proximity_watch.json) con solo
las tickers relevantes, ordenadas por que tan cerca estan, y un log de
alertas (data/proximity_alerts.log) que solo crece cuando algo CRUZA hacia
"cerca" (no en cada chequeo) -- asi el ciclo de 5 minutos puede revisar
ese archivo corto primero y solo profundizar donde de verdad vale la pena,
en vez de re-analizar las 30 tickers en detalle cada vez.

"Cerca" de squeeze_breakout: el ticker YA esta en squeeze (was_squeezed) y
el precio esta a menos de NEAR_BREAKOUT_PCT del precio de la banda que
rompería la señal.

"Cerca" de giro_sma20: hay una tendencia previa clara (prior_trend no es
None) y la pendiente actual ya cambio de signo, pero su magnitud todavia no
llega al umbral de "pronunciado" -- le falta menos de
NEAR_TURN_MAGNITUDE_GAP para calificar.

Uso:
  python proximity_watch.py --once          # un solo chequeo (pruebas)
  python proximity_watch.py --interval 60   # loop continuo (produccion)
"""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from paper_trading.bollinger_strategy import (
    SMA_TURN_MIN_MAGNITUDE_RATIO,
    analyze,
    detect_sma_turn,
)
from paper_trading.engine import get_settings
from paper_trading.nyse_calendar import is_trading_day
from paper_trading.singleton_lock import acquire_single_instance_lock

DEFAULT_INTERVAL_SECONDS = 60
NEAR_BREAKOUT_PCT = 0.15   # % del precio -- que tan cerca de la banda cuenta como "cerca"
NEAR_TURN_MAGNITUDE_GAP = 0.25  # cuanto le puede faltar al magnitude_ratio para contar como "cerca"

MARKET_TZ = ZoneInfo("America/New_York")
PREMARKET_OPEN_TIME = dtime(4, 0)
MARKET_CLOSE_TIME = dtime(16, 0)


def _market_is_active(now: datetime | None = None) -> bool:
    """True durante premarket + sesion regular en un dia de mercado real.
    Fuera de esta ventana, yfinance reporta volumen 0 en casi todas las
    barras -- correr el vigia igual solo genera alertas de "casi-señal"
    falsas (visto en vivo: varios tickers marcados 'cerca' con
    volume_ratio=0.0 al mismo tiempo, despues del cierre)."""
    now = now or datetime.now(MARKET_TZ)
    if not is_trading_day(now.date()):
        return False
    return PREMARKET_OPEN_TIME <= now.time() < MARKET_CLOSE_TIME

SNAPSHOT_PATH = "data/proximity_watch.json"
ALERTS_LOG_PATH = "data/proximity_alerts.log"


def _squeeze_proximity(symbol: str) -> dict | None:
    try:
        r = analyze(symbol)
    except Exception:
        return None
    if not r["was_squeezed"] or r["signal"] != "none":
        return None  # ya disparo, o ni siquiera esta en squeeze -- no es un "casi"
    last_close = r["last_close"]
    dist_upper_pct = abs(r["upper_band"] - last_close) / last_close * 100
    dist_lower_pct = abs(last_close - r["lower_band"]) / last_close * 100
    dist_pct = min(dist_upper_pct, dist_lower_pct)
    if dist_pct > NEAR_BREAKOUT_PCT:
        return None
    direction = "arriba (CALL)" if dist_upper_pct < dist_lower_pct else "abajo (PUT)"
    return {
        "symbol": symbol, "strategy": "squeeze_breakout", "distance_pct": round(dist_pct, 3),
        "direction": direction, "consecutive_squeeze_bars": r["consecutive_squeeze_bars"],
        "volume_ratio": round(r["volume_ratio"], 2),
    }


def _sma_turn_proximity(symbol: str) -> dict | None:
    try:
        r = detect_sma_turn(symbol)
    except Exception:
        return None
    if r["signal"] != "none" or r["prior_trend"] is None:
        return None  # ya disparo, o no hay tendencia previa clara -- no es un "casi"
    turning = (r["prior_trend"] == "alcista" and r["current_slope_pct"] < 0) or \
              (r["prior_trend"] == "bajista" and r["current_slope_pct"] > 0)
    if not turning:
        return None
    gap = SMA_TURN_MIN_MAGNITUDE_RATIO - r["magnitude_ratio"]
    if gap <= 0 or gap > NEAR_TURN_MAGNITUDE_GAP:
        return None
    direction = "abajo (PUT)" if r["prior_trend"] == "alcista" else "arriba (CALL)"
    return {
        "symbol": symbol, "strategy": "giro_sma20", "magnitude_ratio": round(r["magnitude_ratio"], 2),
        "magnitude_gap_to_threshold": round(gap, 2), "direction": direction, "prior_trend": r["prior_trend"],
    }


def check_once() -> list[dict]:
    settings = get_settings()
    symbols = list(dict.fromkeys(settings["watchlist"] + settings["fast_watchlist"]))

    def _check(symbol):
        results = []
        sq = _squeeze_proximity(symbol)
        if sq:
            results.append(sq)
        turn = _sma_turn_proximity(symbol)
        if turn:
            results.append(turn)
        return results

    # Bajado de 8 a 3 el 2026-09-10 -- ver misma nota en auto_entry.py
    # (rate-limit de yfinance con 3 procesos escaneando en paralelo).
    with ThreadPoolExecutor(max_workers=3) as pool:
        nested = list(pool.map(_check, symbols))
    near = [item for sub in nested for item in sub]
    near.sort(key=lambda x: x.get("distance_pct", x.get("magnitude_gap_to_threshold", 999)))

    snapshot = {"updated_at": datetime.now().isoformat(timespec="seconds"), "near_signals": near}
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    return near


def _load_previous_symbols() -> set[str]:
    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {(n["symbol"], n["strategy"]) for n in data.get("near_signals", [])}
    except Exception:
        return set()


def main():
    acquire_single_instance_lock("proximity_watch")
    parser = argparse.ArgumentParser(description="Vigia de casi-señales (squeeze/giro cerca de disparar)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        near = check_once()
        print(f"{len(near)} casi-señal(es) detectada(s).")
        for n in near:
            print(f"  {n}")
        return

    print(f"Vigia de casi-señales iniciado -- chequeando cada {args.interval}s. Ctrl+C para detener.")
    while True:
        try:
            if not _market_is_active():
                print(f"[{datetime.now().isoformat(timespec='seconds')}] mercado cerrado -- en espera.")
                time.sleep(args.interval)
                continue
            previous = _load_previous_symbols()
            near = check_once()
            current = {(n["symbol"], n["strategy"]) for n in near}
            newly_close = current - previous
            any_new = False
            for n in near:
                if (n["symbol"], n["strategy"]) in newly_close:
                    any_new = True
                    line = f"[{datetime.now().isoformat(timespec='seconds')}] NUEVO CERCA: {n}"
                    print(line)
                    with open(ALERTS_LOG_PATH, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
            if not any_new:
                # Heartbeat -- sin esto el log solo crece con alertas nuevas y
                # watchdog.py (mide "colgado" por inactividad en el log)
                # reiniciaba este proceso sano constantemente en ciclos
                # tranquilos (bug real detectado 2026-09-09).
                print(f"[{datetime.now().isoformat(timespec='seconds')}] chequeo OK -- {len(near)} cerca, nada nuevo.")
        except Exception as e:
            print(f"[{datetime.now().isoformat(timespec='seconds')}] ERROR: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
