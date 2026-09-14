#!/usr/bin/env python3
"""
Prende los 4 procesos de Bot Familiar de una sola vez: run_dashboard.py,
proximity_watch.py, multi_user_entry_loop.py, watchdog.py -- y abre el
panel en el navegador. Pensado para el acceso directo del escritorio.

Reescrito 2026-09-11 (la version vieja todavia lanzaba
position_monitor.py/auto_entry.py de un solo usuario, puerto 5000 --
ambos superados por la arquitectura multi-usuario actual, ver CLAUDE.md).

Cada proceso con su stdout/stderr redirigido a su log en data/ (append) --
sin esto los logs quedan huerfanos. Se lanza con la RUTA ABSOLUTA de cada
script (no el nombre pelado) -- necesario porque trading-bot tiene
scripts con los mismos nombres corriendo en la misma PC durante
desarrollo, y un match por nombre solo los confunde (ver nota en
watchdog.py). Los locks de instancia unica (paper_trading/singleton_lock.py)
evitan que se dupliquen si este script se corre dos veces por error.

Uso:
  python start_bot.py
"""

import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DASHBOARD_URL = "http://127.0.0.1:5001"
# A donde abre el navegador al terminar -- /admin directo, ya que este
# acceso directo es para el propio admin en su PC (2026-09-11, a pedido
# explicito: "al panel de administrador hazle un acceso directo"). Si
# todavia no hay sesion iniciada, /admin redirige solo a /login igual
# que cualquier ruta protegida -- despues de loguearse como admin cae
# directo aca (ver el redirect de is_admin en webapp/app.py::dashboard()).
OPEN_URL = f"{DASHBOARD_URL}/admin"

PROCESSES = [
    ("run_dashboard.py", [], "flask.log"),
    ("proximity_watch.py", ["--interval", "30"], "proximity_watch.log"),
    ("multi_user_entry_loop.py", ["--interval", "120"], "multi_user_entry.log"),
    ("watchdog.py", ["--interval", "600"], "watchdog.log"),
]


def _start(script: str, args: list[str], log_name: str) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logf = open(DATA_DIR / log_name, "a", encoding="utf-8")
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    p = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / script), *args], cwd=str(ROOT),
        stdout=logf, stderr=subprocess.STDOUT, close_fds=False, **kwargs,
    )
    return p.pid


def _dashboard_up() -> bool:
    try:
        urllib.request.urlopen(f"{DASHBOARD_URL}/login", timeout=2)
        return True
    except Exception:
        return False


def main():
    print("Prendiendo Bot Familiar...")
    for script, args, log_name in PROCESSES:
        pid = _start(script, args, log_name)
        print(f"  {script:26} PID {pid}")
    print(f"\nListo. Panel: {DASHBOARD_URL}")
    print("Para revisar salud: python health_check.py")
    print("Para apagar: cerra los procesos python.exe desde el Administrador de tareas.")

    for _ in range(20):
        if _dashboard_up():
            break
        time.sleep(0.5)
    webbrowser.open(OPEN_URL)


if __name__ == "__main__":
    main()
