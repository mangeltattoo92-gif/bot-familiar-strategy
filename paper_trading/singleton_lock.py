"""
Lock de instancia unica para los procesos de fondo (position_monitor.py,
proximity_watch.py, auto_entry.py, watchdog.py, run_dashboard.py) -- evita
que dos copias del mismo script corran en paralelo.

Motivo (2026-09-09): al reiniciar watchdog.py, su primer chequeo (corre
inmediato al arrancar, antes del primer sleep) detecto que auto_entry.py
no estaba corriendo y lo arranco solo -- un segundo antes de que el agente
TAMBIEN lo arrancara a mano. Quedaron 2 instancias de auto_entry.py
corriendo a la vez, y una de ellas re-compro un contrato de SPY que la
otra ya habia comprado en el ciclo anterior (la salvaguarda de "no
duplicar tickers ya abiertos" es por-proceso, no protege contra una
segunda instancia corriendo en paralelo).

Usa msvcrt.locking() en Windows y fcntl.flock() en Linux/Mac (POSIX) sobre
un archivo en data/ -- en ambos casos el lock lo libera el sistema
operativo automaticamente cuando el proceso termina, sea limpio o por
crash, asi que no hay riesgo de un "lock viejo" que se quede pegado y
bloquee arranques futuros (a diferencia de un archivo con PID que hay que
validar a mano). Portado a POSIX el 2026-09-10 para el servidor Hetzner
(Ubuntu) -- msvcrt no existe fuera de Windows.
"""

import os
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Mantiene una referencia global al file handle -- si se recolecta basura
# (garbage collected) el lock se libera antes de tiempo.
_held_locks = []


def _lock_windows(f) -> bool:
    import msvcrt
    # CRITICO: msvcrt.locking() bloquea `nbytes` a partir de la posicion
    # ACTUAL del archivo, no siempre desde el byte 0. En modo "a+", si el
    # archivo ya tenia contenido de una corrida anterior, la posicion
    # inicial puede quedar al FINAL del archivo -- dos procesos bloqueando
    # posiciones distintas nunca chocan entre si, y el lock no protege nada
    # (bug real detectado 2026-09-09: dos instancias de auto_entry.py
    # corrieron en paralelo sin que este lock lo impidiera, por esto mismo).
    # Por eso: SIEMPRE f.seek(0) antes de intentar el lock, para que todas
    # las instancias compitan por exactamente el mismo byte.
    f.seek(0)
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _lock_posix(f) -> bool:
    import fcntl
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def acquire_single_instance_lock(name: str) -> None:
    """Sale del proceso inmediatamente (sys.exit) si ya hay otra instancia
    de `name` corriendo. Debe llamarse una sola vez, al inicio de main()."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = DATA_DIR / f"{name}.lock"
    f = open(lock_path, "a+")
    acquired = _lock_windows(f) if sys.platform == "win32" else _lock_posix(f)
    if not acquired:
        print(f"Ya hay otra instancia de {name} corriendo (lock activo en {lock_path}) -- "
              f"esta instancia se cierra sin hacer nada, para no duplicar trabajo.")
        f.close()
        sys.exit(1)
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    _held_locks.append(f)  # nunca se cierra explicitamente -- vive hasta que el proceso termina
