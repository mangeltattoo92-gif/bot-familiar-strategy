#!/usr/bin/env python3
"""
Loop externo para multi_user_entry.py -- proceso NUEVO en cada ciclo en
vez de un proceso que vive para siempre reutilizando el mismo pool de
conexiones de yfinance.

Por que existe (portado de trading-bot/family_sim_loop.py, 2026-09-10):
el modo de loop continuo (`--interval`) se colgaba en silencio despues
de un rato en pruebas de trading-bot -- vivo, usando algo de CPU, pero
sin loguear nada durante 40+ minutos. `--once` en cambio SIEMPRE
funciono rapido y limpio. Sospecha (no 100% confirmada): threads de
yfinance que quedan colgados de forma indefinida van saturando el pool
compartido del proceso con el correr de los ciclos. Un proceso NUEVO en
cada ciclo arranca con un pool limpio.

Uso (produccion, este es el que se deja corriendo):
  python multi_user_entry_loop.py --interval 120
"""
import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paper_trading.singleton_lock import acquire_single_instance_lock  # noqa: E402

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "data" / "multi_user_entry.log"


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    parser = argparse.ArgumentParser(description="Loop externo de ciclos frescos para multi_user_entry.py")
    parser.add_argument("--interval", type=int, default=120)
    args = parser.parse_args()

    # Bug real encontrado 2026-09-11: este wrapper NO tenia lock de
    # instancia unica -- correr start_bot.py dos veces (o cualquier doble
    # arranque) creaba 2 loops compitiendo, cada uno lanzando
    # multi_user_entry.py --once por su cuenta, con riesgo real de
    # duplicar ordenes para los mismos usuarios. `--once` en si mismo
    # sigue sin lock a proposito (es la herramienta de prueba manual,
    # igual que en trading-bot/family_sim.py) -- el lock va aca, en el
    # wrapper que es el que de verdad se deja corriendo en produccion.
    acquire_single_instance_lock("multi_user_entry_loop")

    _log(f"multi_user_entry_loop.py iniciado -- ciclo nuevo cada {args.interval}s (proceso fresco por vuelta).")
    while True:
        _run_once_cycle()
        time.sleep(args.interval)


def _run_once_cycle() -> None:
    # Bug real encontrado en vivo 2026-09-11: con capture_output=True,
    # subprocess.run() abre PIPES para stdout/stderr -- si el hijo (o un
    # thread suyo que no termina limpio con el proceso) deja ese pipe
    # abierto, el communicate() interno de subprocess.run() se queda
    # esperando a que el pipe cierre AUN DESPUES de que el timeout mate
    # al proceso principal -- el wrapper quedaba vivo con 0% CPU,
    # bloqueado para siempre, sin loguear nada (visto 3 veces seguidas
    # en produccion). Fix: nada de pipes -- stdout/stderr van a
    # DEVNULL (el propio script ya loguea a su archivo con _log()), y si
    # se excede el timeout se mata el ARBOL COMPLETO de procesos via
    # taskkill /T (no solo el proceso principal -- un kill() de Python
    # no mata threads/hijos que haya dejado colgados).
    stdout_target = subprocess.DEVNULL
    stderr_target = subprocess.DEVNULL
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "multi_user_entry.py", "--once"],
            cwd=str(ROOT), stdout=stdout_target, stderr=stderr_target, **kwargs,
        )
    except Exception as e:
        _log(f"[loop] ERROR lanzando multi_user_entry.py --once: {e}")
        return
    try:
        returncode = proc.wait(timeout=90)
        if returncode != 0:
            _log(f"[loop] multi_user_entry.py --once termino con codigo {returncode}.")
    except subprocess.TimeoutExpired:
        _log(f"[loop] multi_user_entry.py --once excedio 90s (PID {proc.pid}) -- "
             f"matando el arbol de procesos con taskkill y reintentando el proximo ciclo.")
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


if __name__ == "__main__":
    main()
