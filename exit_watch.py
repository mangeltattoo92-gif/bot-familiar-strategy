#!/usr/bin/env python3
"""
Motor liviano de SOLO salidas -- 2026-09-15, a pedido del usuario
("hay que mejorar el chequeo automatico cada dos minutos").

El ciclo normal (multi_user_entry_loop.py, cada 120s) ya revisa salidas
primero y entradas despues DENTRO del mismo ciclo (ver run_cycle() en
multi_user_entry.py) -- pero el escaneo de entrada (hasta 45 tickers con
5 estrategias cada uno, con llamadas de red a yfinance) puede tardar
bastante mas que el chequeo de salidas en si, que solo necesita el
precio actual de las posiciones YA abiertas de cada cuenta. En la
practica eso significa que el stop-loss de una posicion abierta puede
tardar bastante mas de 120s en detectarse si el escaneo de entrada del
ciclo anterior todavia esta corriendo.

Este script hace SOLO el paso de salidas (reusa run_exits_for_account,
la MISMA funcion ya probada que usa multi_user_entry.py -- no reimplementa
la logica), en un loop propio mucho mas frecuente (30s por defecto) y
mucho mas rapido por ciclo al no escanear señales de entrada. Nunca abre
posiciones nuevas. Comparte paper_trading.singleton_lock.exits_critical_section()
con multi_user_entry.py para que los dos procesos nunca revisen/cierren la
misma posicion al mismo tiempo.

Uso:
  python exit_watch.py --once            # un solo chequeo (pruebas)
  python exit_watch.py --interval 30     # loop continuo -- ver
    exit_watch_loop.py para el wrapper recomendado en produccion
    (proceso fresco por ciclo, mismo patron que multi_user_entry_loop.py).
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from multi_user_entry import _log, active_accounts, run_exits_for_account  # noqa: E402
from paper_trading.singleton_lock import exits_critical_section  # noqa: E402

DEFAULT_INTERVAL_SECONDS = 30


def run_once() -> int:
    accounts = active_accounts()
    closed_total = 0
    with exits_critical_section():
        for username, db_path in accounts:
            closed_total += run_exits_for_account(username, db_path)
    return closed_total


def main():
    parser = argparse.ArgumentParser(description="Motor liviano de solo-salidas (stop-loss/objetivo mas rapido)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        closed = run_once()
        print(f"{closed} posicion(es) cerrada(s).")
        return

    _log(f"exit_watch.py iniciado -- chequeo de salidas cada {args.interval}s.")
    while True:
        try:
            run_once()
        except Exception as e:
            _log(f"[exit_watch] ERROR: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
