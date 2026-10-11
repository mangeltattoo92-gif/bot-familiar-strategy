#!/usr/bin/env python3
"""
Censor RAPIDO (2026-10-05, a pedido explicito: "un motor de vigilancia...
que chequee en tiempo real las posiciones, entradas y salidas de cada
cuenta y que corrija errores en segundos"): corre en bucle cada 15
segundos, SOLO los chequeos BARATOS (sin llamadas a yfinance, no
afectado por la cantidad de cuentas -- ver nota sobre 500 cuentas
simuladas) de watchdog.py:

  - Procesos caidos o colgados (panel Flask, multi_user_entry_loop.py,
    proximity_watch.py) -> se reinician EN SEGUNDOS, no en 10 minutos.
  - Matematica de cuenta rota, posiciones duplicadas -> ahora se
    autocorrigen (ver health_check.fix_account_math y
    health_check.fix_duplicate_positions), con el detalle de la
    correccion bien logueado para poder revisar la causa despues.

Lo que NO esta aca (sigue en watchdog.py, cada 10 min): el chequeo de
precios en vivo por posicion (check_position_prices(), pega a yfinance
por CADA posicion de CADA cuenta -- con 500+ cuentas correr eso cada 15s
seria exactamente el tipo de sobrecarga a yfinance que se queria evitar)
y el respaldo (robocopy del proyecto entero, operacion pesada).

Uso:
  python fast_watchdog.py --once
  python fast_watchdog.py --interval 15   # bucle continuo (produccion)
"""
import argparse
import time

import health_check as hc
import watchdog as wd
from paper_trading.engine import log_error
from paper_trading.singleton_lock import acquire_single_instance_lock

DEFAULT_INTERVAL_SECONDS = 15


def check_and_heal_fast() -> list[str]:
    actions = []

    if wd._flask_down():
        wd._kill_and_wait(wd.MANAGED_PROCESSES["run_dashboard.py"]["match"])
        wd._start(wd.MANAGED_PROCESSES["run_dashboard.py"])
        msg = f"Panel Flask (:{wd.FLASK_PORT}) no respondia -- reiniciado."
        actions.append(msg)
        wd._log(msg)
        log_error(message=msg, reason="fast_watchdog.py: auto-reparacion")

    for name in ("proximity_watch.py", "multi_user_entry_loop.py"):
        spec = wd.MANAGED_PROCESSES[name]
        running = hc._script_is_running(spec["match"])
        if running is None:
            continue
        if not running:
            wd._start(spec)
            msg = f"{name} no estaba corriendo -- arrancado."
            actions.append(msg)
            wd._log(msg)
            log_error(message=msg, reason="fast_watchdog.py: auto-reparacion")
        elif wd._is_stalled(spec):
            wd._kill_and_wait(spec["match"])
            wd._start(spec)
            threshold = spec["expected_interval"] * wd.STALL_MULTIPLIER
            msg = (f"{name} estaba corriendo pero colgado "
                   f"(su log no crecio en > {threshold}s) -- matado y reiniciado.")
            actions.append(msg)
            wd._log(msg)
            log_error(message=msg, reason="fast_watchdog.py: auto-reparacion")

    for fixed, label in (
        (hc.fix_account_math(), "Matematica de la cuenta"),
        (hc.fix_duplicate_positions(), "Posiciones duplicadas"),
    ):
        for f in fixed:
            actions.append(f)
            wd._log(f"[AUTO-CORREGIDO -- {label}] {f}")
            log_error(message=f, reason=f"fast_watchdog.py: {label} (auto-reparado)")

    if not actions:
        wd._log("Chequeo rapido OK.")
    return actions


def main():
    acquire_single_instance_lock("fast_watchdog")
    parser = argparse.ArgumentParser(description="Censor rapido -- procesos + matematica de cuenta, cada pocos segundos")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        check_and_heal_fast()
        return

    wd._log(f"fast_watchdog.py iniciado -- chequeo cada {args.interval}s (solo checks baratos, sin yfinance).")
    while True:
        try:
            check_and_heal_fast()
        except Exception as e:
            wd._log(f"Error en ciclo de fast_watchdog: {e!r}")
            log_error(message=repr(e), reason="fast_watchdog.py: excepcion en ciclo")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
