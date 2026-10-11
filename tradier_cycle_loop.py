#!/usr/bin/env python3
"""
Loop externo para tradier_cycle.py -- mismo patron que multi_user_entry_loop.py
(proceso NUEVO en cada ciclo, ver esa nota para el porque). Uso:
  python tradier_cycle_loop.py --interval 120
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
LOG_PATH = ROOT / "data" / "tradier_cycle.log"


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    parser = argparse.ArgumentParser(description="Loop externo de ciclos frescos para tradier_cycle.py")
    parser.add_argument("--interval", type=int, default=120)
    args = parser.parse_args()

    acquire_single_instance_lock("tradier_cycle_loop")
    _log(f"tradier_cycle_loop.py iniciado -- ciclo nuevo cada {args.interval}s.")
    while True:
        _run_once_cycle()
        time.sleep(args.interval)


def _run_once_cycle() -> None:
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "tradier_cycle.py", "--once"],
            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        _log(f"[loop] ERROR lanzando tradier_cycle.py --once: {e}")
        return
    try:
        returncode = proc.wait(timeout=90)
        if returncode != 0:
            _log(f"[loop] tradier_cycle.py --once termino con codigo {returncode}.")
    except subprocess.TimeoutExpired:
        _log(f"[loop] tradier_cycle.py --once excedio 90s (PID {proc.pid}) -- matando y reintentando el proximo ciclo.")
        proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


if __name__ == "__main__":
    main()
