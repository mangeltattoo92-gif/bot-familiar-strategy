#!/usr/bin/env python3
"""
Version del motor liviano de SOLO salidas (ver exit_watch.py en
bot-familiar) para el piloto de dinero real -- 2026-09-15, mismo pedido
del usuario aplicado tambien al piloto.

pilot_cycle.py YA revisa salidas primero, pero corre solo cada 5
minutos (via run_pilot.sh, que ademas necesita levantar el agente de
Claude para la parte de entrada -- mas lento que el chequeo de salidas
en si). Este script hace SOLO el paso de salidas de la cuenta "piloto"
(reusa run_exits_for_account, la MISMA funcion ya probada) en un loop
propio mucho mas frecuente (30s por defecto), sin agente y sin tocar
Robinhood -- las salidas ya se ejecutan con precio real de yfinance,
igual que en pilot_cycle.py. Nunca abre posiciones nuevas. Comparte
paper_trading.singleton_lock.exits_critical_section() con
pilot_cycle.py para que los dos procesos nunca revisen/cierren la misma
posicion al mismo tiempo.

Uso:
  python pilot_exit_watch.py --once            # un solo chequeo (pruebas)
  python pilot_exit_watch.py --interval 30      # loop continuo -- ver
    pilot_exit_watch_loop.py para el wrapper recomendado en produccion.
"""
import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from multi_user_entry import run_exits_for_account  # noqa: E402
from paper_trading.engine import DEFAULT_DB_PATH  # noqa: E402
from paper_trading.singleton_lock import exits_critical_section  # noqa: E402

DEFAULT_INTERVAL_SECONDS = 30
LOG_PATH = ROOT / "data" / "pilot_exit_watch.log"


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_once() -> int:
    with exits_critical_section():
        return run_exits_for_account("piloto", DEFAULT_DB_PATH)


def main():
    parser = argparse.ArgumentParser(description="Motor liviano de solo-salidas para el piloto (stop-loss mas rapido)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        closed = run_once()
        print(f"{closed} posicion(es) cerrada(s).")
        return

    _log(f"pilot_exit_watch.py iniciado -- chequeo de salidas cada {args.interval}s.")
    while True:
        try:
            closed = run_once()
            if closed:
                _log(f"{closed} posicion(es) cerrada(s) este ciclo.")
        except Exception as e:
            _log(f"[pilot_exit_watch] ERROR: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
