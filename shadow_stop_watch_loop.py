#!/usr/bin/env python3
"""
Loop externo para shadow_stop_watch.py -- mismo patron de proceso fresco
por ciclo que exit_watch_loop.py (ver la nota larga en
multi_user_entry_loop.py sobre por que existe este patron).

Uso (produccion, este es el que se deja corriendo):
  python shadow_stop_watch_loop.py --interval 30
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
LOG_PATH = ROOT / "data" / "shadow_stop_watch_loop.log"


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    parser = argparse.ArgumentParser(description="Loop externo de ciclos frescos para shadow_stop_watch.py")
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()

    acquire_single_instance_lock("shadow_stop_watch_loop")

    _log(f"shadow_stop_watch_loop.py iniciado -- ciclo nuevo cada {args.interval}s (proceso fresco por vuelta).")
    while True:
        _run_once_cycle()
        time.sleep(args.interval)


def _run_once_cycle() -> None:
    stdout_target = subprocess.DEVNULL
    stderr_target = subprocess.DEVNULL
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "shadow_stop_watch.py", "--once"],
            cwd=str(ROOT), stdout=stdout_target, stderr=stderr_target, **kwargs,
        )
    except Exception as e:
        _log(f"[loop] ERROR lanzando shadow_stop_watch.py --once: {e}")
        return
    try:
        returncode = proc.wait(timeout=25)
        if returncode != 0:
            _log(f"[loop] shadow_stop_watch.py --once termino con codigo {returncode}.")
    except subprocess.TimeoutExpired:
        _log(f"[loop] shadow_stop_watch.py --once excedio 25s (PID {proc.pid}) -- "
             f"matando el arbol de procesos y reintentando el proximo ciclo.")
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
