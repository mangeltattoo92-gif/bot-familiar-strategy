#!/usr/bin/env python3
"""
Censor de salud del sistema -- corre en bucle continuo (por defecto cada 10
minutos) y AUTO-REPARA lo que puede reparar con seguridad:

  1. Panel Flask (:5000) caido -> se reinicia.
  2. multi_user_entry_loop.py / proximity_watch.py caidos -> se arrancan.
  3. multi_user_entry_loop.py / proximity_watch.py CORRIENDO PERO COLGADOS
     (el PID existe pero su log no crece desde hace mucho mas que su
     intervalo esperado -- normalmente por una llamada de red a yfinance
     sin limite de tiempo, ver _fetch_history()/_with_timeout() en
     bollinger_strategy.py y market_data.py) -> se matan y se reinician.

Todo lo que reinicia este script se arranca CON stdout/stderr redirigido a
su archivo de log en data/ (append), asi el log deja de quedar huerfano y
sirve para diagnosticar la proxima vez sin tener que investigar desde cero.

Lo que NO repara solo (matematica de cuenta, posiciones duplicadas): lo
registra en el error_log del panel web y en data/watchdog.log para
revision humana -- corregir datos de la cuenta a ciegas es mas peligroso
que dejarlo visible.

  4. Respaldo: en cada ciclo sincroniza todo el proyecto (codigo + base de
     datos) hacia BACKUP_DIR en el escritorio, via robocopy /MIR -- a
     pedido del usuario (2026-09-09), para que el respaldo se mantenga
     siempre al dia con los cambios, no sea una foto unica que envejece.

Uso:
  python watchdog.py --once           # una pasada (pruebas)
  python watchdog.py --interval 600   # bucle continuo (produccion, 10 min)
"""

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import health_check as hc
from paper_trading.engine import log_error
from paper_trading.singleton_lock import acquire_single_instance_lock

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
WATCHDOG_LOG = DATA_DIR / "watchdog.log"
# Ver la misma nota en health_check.py -- 5001 es el dev server local
# (Windows), el servidor de Hetzner corre gunicorn en 8000 (BOT_FAMILIAR_PORT).
FLASK_PORT = int(os.environ.get("BOT_FAMILIAR_PORT", 5001))
# "Desktop" no existe en el servidor Linux (headless) -- ahi el respaldo
# va al home del usuario que corre el servicio (botfam), no a /opt (root
# no le da permiso de escritura ahi al usuario de servicio) (2026-09-13).
BACKUP_DIR = (Path.home() / "Desktop" / "bot-familiar-backup") if sys.platform == "win32" \
    else Path.home() / "bot-familiar-backup"

DEFAULT_INTERVAL_SECONDS = 600  # 10 minutos

# script -> log a vigilar, intervalo esperado de ese proceso (segundos, o
# None si no aplica un "ciclo" -- ej. Flask, que se vigila por el puerto),
# y comando de arranque.
#
# CORREGIDO 2026-09-10: position_monitor.py/auto_entry.py (de un solo
# usuario) quedaron REEMPLAZADOS por multi_user_entry_loop.py (entradas Y
# salidas para todos los usuarios activos, ver ese archivo) -- esos 2 ya
# no hace falta vigilarlos. proximity_watch.py NO fue reemplazado -- es
# un servicio COMPARTIDO (alertas de "casi señal", no toca cuentas de
# nadie, solo lee la watchlist) que sigue corriendo igual que en
# trading-bot, asi que se lo agrega de vuelta.
# IMPORTANTE (bug real encontrado 2026-09-10): trading-bot y bot-familiar
# tienen scripts con el MISMO NOMBRE (proximity_watch.py, run_dashboard.py,
# etc.) corriendo en la MISMA PC durante desarrollo -- un filtro de
# PowerShell que solo compara el nombre del archivo (`*proximity_watch.py*`)
# encuentra el proceso de CUALQUIERA de los 2 proyectos, no solo el propio
# (se detecto en vivo: health_check.py de bot-familiar penso que su propio
# proximity_watch.py estaba corriendo cuando en realidad era el de
# trading-bot). Fix: lanzar y buscar cada script por su RUTA ABSOLUTA
# dentro de ESTE proyecto (`match`), no por el nombre pelado.
MANAGED_PROCESSES = {
    "run_dashboard.py": {
        "log": DATA_DIR / "flask.log",
        "expected_interval": None,
        "match": str(ROOT / "run_dashboard.py"),
        "cmd": [sys.executable, "-u", str(ROOT / "run_dashboard.py")],
    },
    "proximity_watch.py": {
        "log": DATA_DIR / "proximity_watch.log",
        "expected_interval": 30,
        "match": str(ROOT / "proximity_watch.py"),
        "cmd": [sys.executable, "-u", str(ROOT / "proximity_watch.py"), "--interval", "30"],
    },
    "multi_user_entry_loop.py": {
        "log": DATA_DIR / "multi_user_entry.log",
        "expected_interval": 120,
        "match": str(ROOT / "multi_user_entry_loop.py"),
        "cmd": [sys.executable, "-u", str(ROOT / "multi_user_entry_loop.py"), "--interval", "120"],
    },
}

# Un proceso se considera "colgado" (no solo lento) si su log no crece en
# mas de este multiplo de su intervalo esperado -- da margen a chequeos
# legitimamente lentos (ej. rate-limit de 15s en una sola llamada) sin
# generar falsos positivos ni reiniciar en exceso.
STALL_MULTIPLIER = 6


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _kill(script_name: str) -> None:
    if sys.platform != "win32":
        _kill_posix(script_name)
        return
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
         f"Where-Object {{ $_.CommandLine -like '*{script_name}*' }} | "
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
        capture_output=True, text=True, timeout=15,
    )


def _kill_posix(match: str) -> None:
    """Escanea /proc (mismo mecanismo que hc._script_is_running_posix) y
    manda SIGTERM a cada PID cuyo cmdline contenga `match`. En el
    servidor Linux esto alcanza -- no hace falta relanzar el proceso
    despues (a diferencia de Windows): `systemd` tiene `Restart=always`
    en cada unidad y lo levanta solo apenas lo ve morir, normalmente
    mas rapido que el ciclo de este censor (2026-09-13)."""
    import os
    import signal
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            cmdline = (pid_dir / "cmdline").read_bytes().decode("utf-8", errors="replace")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if match in cmdline.replace("\x00", " "):
            try:
                os.kill(int(pid_dir.name), signal.SIGTERM)
            except ProcessLookupError:
                pass


def _kill_and_wait(script_name: str, timeout: float = 10.0) -> None:
    """Mata `script_name` y ESPERA a que realmente desaparezca antes de
    volver -- un time.sleep(N) fijo no garantiza nada: si _start() lanza el
    reemplazo mientras el proceso viejo todavia tiene el lock de instancia
    unica (singleton_lock.py) tomado, el proceso NUEVO se cierra solo al
    arrancar (por diseño) y el 'reiniciado' que loguea watchdog.py termina
    siendo falso -- el proceso sigue caido hasta el proximo ciclo (bug real
    encontrado en el review del 2026-09-09, ver INCIDENTS.md)."""
    _kill(script_name)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not hc._script_is_running(script_name):
            return
        time.sleep(0.5)


def _start(spec: dict) -> None:
    # En el servidor Linux cada proceso ya corre bajo systemd con
    # Restart=always -- si este censor lo mato (_kill_posix) o lo
    # encontro caido, systemd ya lo esta relanzando solo (normalmente en
    # <5s, RestartSec de cada unidad), sin que este script tenga que
    # lanzar nada el mismo. Lanzarlo aca TAMBIEN no rompe nada gracias al
    # lock de instancia unica de cada wrapper (uno de los dos pierde la
    # carrera y se cierra solo) pero es trabajo de mas -- mejor no
    # duplicar el mecanismo de arranque (2026-09-13).
    if sys.platform != "win32":
        return
    log_path = spec["log"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "a", encoding="utf-8")
    subprocess.Popen(
        spec["cmd"], cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        close_fds=False,
    )


def _is_stalled(spec: dict) -> bool:
    if spec["expected_interval"] is None:
        return False
    log_path = spec["log"]
    if not log_path.exists():
        return False  # nunca arranco -- eso lo cubre "no esta corriendo", no es un stall
    age = time.time() - log_path.stat().st_mtime
    return age > spec["expected_interval"] * STALL_MULTIPLIER


def _flask_down() -> bool:
    """True si Flask no responde. Un connect() TCP no alcanza -- el
    servidor de desarrollo de Flask es de un solo hilo, y un yfinance
    colgado (ver market_data.py) puede dejarlo con el puerto ABIERTO pero
    sin contestar ninguna peticion (el bug real que origino todo esto el
    2026-09-09). Se hace un GET real con timeout a /login (no requiere
    sesion) para confirmar que de verdad responde, no solo que acepta la
    conexion TCP."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{FLASK_PORT}/login", timeout=5) as resp:
            return resp.status >= 500
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return True


def sync_backup() -> str | None:
    """Sincroniza todo el proyecto (codigo + data/paper_trading.db) hacia
    BACKUP_DIR via robocopy /MIR -- deja el respaldo identico al proyecto
    en cada ciclo (agrega archivos nuevos, actualiza los cambiados, borra
    los que ya no existen). Excluye __pycache__ (basura regenerable) y
    *.lock (siempre bloqueados por los procesos vivos -- ver
    singleton_lock.py, no son datos, se regeneran solos al arrancar).
    Devuelve un mensaje de error corto si robocopy fallo de verdad
    (codigo >= 8 -- 0-7 son variantes de 'exito', ver documentacion de
    robocopy), o None si salio bien.

    En Linux (servidor Hetzner) no existe robocopy ni una carpeta
    "Desktop" -- se usa `rsync -a --delete` (mismo espiritu de espejo
    exacto) hacia BACKUP_DIR, que en POSIX apunta a
    /opt/bot-familiar-backup en vez del escritorio (2026-09-13)."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        try:
            result = subprocess.run(
                ["rsync", "-a", "--delete", "--exclude=__pycache__", "--exclude=*.lock",
                 f"{ROOT}/", f"{BACKUP_DIR}/"],
                capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            return "rsync no termino en 120s"
        except FileNotFoundError:
            return "rsync no esta instalado"
        if result.returncode != 0:
            return f"rsync fallo (codigo {result.returncode}): {result.stderr[-500:]}"
        return None
    try:
        result = subprocess.run(
            ["robocopy", str(ROOT), str(BACKUP_DIR), "/MIR", "/XD", "__pycache__",
             "/XF", "*.lock", "/R:2", "/W:2", "/NFL", "/NDL", "/NP", "/NJH", "/NJS"],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "robocopy no termino en 120s"
    if result.returncode >= 8:
        return f"robocopy fallo (codigo {result.returncode}): {result.stdout[-500:]}"
    return None


def check_and_heal_once() -> list[str]:
    actions = []

    if _flask_down():
        _kill_and_wait(MANAGED_PROCESSES["run_dashboard.py"]["match"])
        _start(MANAGED_PROCESSES["run_dashboard.py"])
        msg = f"Panel Flask (:{FLASK_PORT}) no respondia -- reiniciado."
        actions.append(msg)
        _log(msg)
        log_error(message=msg, reason="watchdog.py: auto-reparacion")

    for name in ("proximity_watch.py", "multi_user_entry_loop.py"):
        spec = MANAGED_PROCESSES[name]
        # _script_is_running() es tri-estado (True/False/None -- None
        # significa "no se pudo verificar", NO "no esta corriendo", ver su
        # propio docstring en health_check.py). Actuar como si "no se pudo
        # verificar" fuera "esta caido" arrancaria una instancia extra
        # sobre una que quizas si esta viva -- mas seguro esperar al
        # proximo ciclo cuando el chequeo si pueda confirmar.
        running = hc._script_is_running(spec["match"])
        if running is None:
            _log(f"No se pudo verificar si {name} esta corriendo -- se reintenta el proximo ciclo, no se actua a ciegas.")
            continue
        if not running:
            _start(spec)
            msg = f"{name} no estaba corriendo -- arrancado."
            actions.append(msg)
            _log(msg)
            log_error(message=msg, reason="watchdog.py: auto-reparacion")
        elif _is_stalled(spec):
            _kill_and_wait(spec["match"])
            _start(spec)
            threshold = spec["expected_interval"] * STALL_MULTIPLIER
            msg = (f"{name} estaba corriendo pero colgado "
                   f"(su log no crecio en > {threshold}s, esperado cada {spec['expected_interval']}s) "
                   f"-- matado y reiniciado.")
            actions.append(msg)
            _log(msg)
            log_error(message=msg, reason="watchdog.py: auto-reparacion")

    # Chequeos de solo-reporte -- no se auto-reparan (corregir datos de
    # cuenta a ciegas es mas riesgoso que dejarlo para revision humana).
    for problems, label in (
        (hc.check_account_math(), "Matematica de la cuenta"),
        (hc.check_duplicate_positions(), "Posiciones duplicadas"),
    ):
        for p in problems:
            _log(f"[REVISION MANUAL -- {label}] {p}")
            log_error(message=p, reason=f"watchdog.py: {label} (no auto-reparable)")

    backup_error = sync_backup()
    if backup_error:
        _log(f"[RESPALDO] fallo la sincronizacion: {backup_error}")
        log_error(message=backup_error, reason="watchdog.py: sync_backup")
        actions.append("respaldo fallo")

    if not actions:
        _log("Chequeo OK -- todo corriendo, respaldo sincronizado, sin acciones necesarias.")
    return actions


def main():
    acquire_single_instance_lock("watchdog")
    parser = argparse.ArgumentParser(description="Censor de salud -- detecta y repara procesos caidos/colgados")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS,
                         help=f"Segundos entre chequeos (default {DEFAULT_INTERVAL_SECONDS})")
    parser.add_argument("--once", action="store_true", help="Corre un solo chequeo y termina (para pruebas)")
    args = parser.parse_args()

    if args.once:
        check_and_heal_once()
        return

    _log(f"Censor iniciado -- chequeando/reparando cada {args.interval}s. Ctrl+C para detener.")
    while True:
        try:
            check_and_heal_once()
        except Exception as e:
            _log(f"ERROR en el censor mismo (no se detiene, reintenta en el proximo ciclo): {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
