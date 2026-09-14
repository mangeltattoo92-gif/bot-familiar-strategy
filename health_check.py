#!/usr/bin/env python3
"""
Verificacion de salud del sistema de paper trading. Detecta anomalias
reales (no las "arregla" solo -- las reporta con detalle para corregirlas
de inmediato, siguiendo el mismo patron ya usado en este proyecto: dogfooding
en vivo para encontrar bugs y corregirlos en el momento).

Chequeos:
  1. Matematica de la cuenta: cash + valor de posiciones (a costo) + P&L
     realizado debe cuadrar con el historial de trades -- si no, hay un
     trade mal registrado (ej. el bug real de multiplicador de venta que
     no se aplico, encontrado el 2026-09-08).
  2. Precio en vivo disponible para cada posicion abierta -- si
     market_data no puede fijar precio, la posicion no se puede evaluar
     para su cierre (riesgo real: se queda abierta indefinidamente).
  3. Sin posiciones duplicadas (misma key con mas de una fila -- no deberia
     poder pasar por la restriccion PRIMARY KEY, pero se verifica igual).
  4. Los procesos de fondo esperados (Flask, multi_user_entry_loop.py,
     proximity_watch.py) siguen vivos.

Cada anomalia encontrada se registra en el error_log (mismo panel web que
ya existe) y se imprime en consola. No hace ninguna correccion automatica
de datos -- decidir si corregir queda a criterio de quien lo revise.

Uso:
  python health_check.py
"""

import os
import socket
import subprocess
import sys
from pathlib import Path

from paper_trading.engine import DEFAULT_DB_PATH, _connect, get_status, log_error
from paper_trading.trade_journal import get_closed_trades
from webapp import market_data
from webapp.auth import STATUS_ACTIVE, list_all_users

# 5001 es el puerto del servidor de desarrollo de Flask (PC local, Windows).
# En el servidor de Hetzner (Linux) gunicorn escucha en 127.0.0.1:8000
# detras de nginx -- BOT_FAMILIAR_PORT lo pisa (seteado en la unidad
# systemd), 2026-09-13, bug real: el panel de administrador reportaba
# TODO como caido en produccion porque esto seguia apuntando a 5001.
FLASK_PORT = int(os.environ.get("BOT_FAMILIAR_PORT", 5001))
ROOT = Path(__file__).resolve().parent
USERS_DATA_DIR = ROOT / "data" / "users"


def _active_user_db_paths() -> list[tuple[str, Path]]:
    """(username, db_path) de cada usuario ACTIVO cuya base de datos ya
    existe (todavia no creo su cuenta si nunca abrio el panel ni el motor
    de entradas la creo por el -- nada que chequear ahi)."""
    out = []
    try:
        users = list_all_users()
    except Exception:
        return out
    for u in users:
        if u["status"] != STATUS_ACTIVE:
            continue
        path = USERS_DATA_DIR / u["username"] / "paper_trading.db"
        if path.exists():
            out.append((u["username"], path))
    return out


def check_account_math() -> list[str]:
    """Cash actual + costo de posiciones abiertas (a avg_cost) debe cuadrar
    con: balance inicial - suma de compras + suma de ventas. Se verifica
    indirectamente reconstruyendo cash esperado desde el historial de
    trades -- POR CADA USUARIO ACTIVO por separado (2026-09-10, sistema
    multi-tenant: cada uno tiene su propia base de datos, no hay una
    unica cuenta compartida que chequear)."""
    problems = []
    for username, db_path in _active_user_db_paths():
        conn = _connect(db_path)
        try:
            account = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
            trades = conn.execute("SELECT * FROM trades ORDER BY id ASC").fetchall()
        finally:
            conn.close()

        if account is None:
            problems.append(f"[{username}] Cuenta no inicializada (tabla 'account' vacia).")
            continue

        expected_cash = account["initial_balance"]
        expected_realized = 0.0
        for t in trades:
            expected_cash += t["cash_effect"]
            expected_realized += t["realized_pnl_delta"]

        cash_diff = round(account["cash_balance"] - expected_cash, 2)
        realized_diff = round(account["realized_pnl"] - expected_realized, 2)

        if abs(cash_diff) > 0.01:
            problems.append(
                f"[{username}] Cash balance no cuadra con la suma de cash_effect del historial: "
                f"cash actual ${account['cash_balance']:.2f}, esperado ${expected_cash:.2f} "
                f"(diferencia ${cash_diff:+.2f})."
            )
        if abs(realized_diff) > 0.01:
            problems.append(
                f"[{username}] P&L realizado no cuadra con la suma de realized_pnl_delta del historial: "
                f"actual ${account['realized_pnl']:.2f}, esperado ${expected_realized:.2f} "
                f"(diferencia ${realized_diff:+.2f})."
            )
    return problems


def check_position_prices() -> list[str]:
    problems = []
    for username, db_path in _active_user_db_paths():
        status = get_status(db_path=db_path)
        for p in status["positions"]:
            if p["asset_type"] == "equity":
                price = market_data.get_quote(p["ticker"])
            elif p["asset_type"] == "crypto":
                price = market_data.get_quote(f"{p['ticker']}-USD")
            elif p["asset_type"] == "option":
                od = p["option_details"]
                price = market_data.get_option_price(p["ticker"], od["expiration"], od["strike"], od["option_type"])
            else:
                price = None
            if price is None:
                problems.append(
                    f"[{username}] {p['ticker']} ({p['asset_type']}): no se pudo obtener precio en vivo -- "
                    f"esta posicion no se puede evaluar para su cierre hasta que haya datos."
                )
    return problems


def check_duplicate_positions() -> list[str]:
    problems = []
    for username, db_path in _active_user_db_paths():
        conn = _connect(db_path)
        try:
            rows = conn.execute("SELECT key, COUNT(*) as n FROM positions GROUP BY key HAVING n > 1").fetchall()
        finally:
            conn.close()
        problems.extend(f"[{username}] Key de posicion duplicada: {r['key']} ({r['n']} filas)." for r in rows)
    return problems


def _script_is_running_posix(match: str) -> bool | None:
    """Escanea /proc directamente -- mas rapido y confiable que lanzar
    `ps` como subproceso, y no depende de que `ps`/`procps` este
    instalado. Cada PID numerico bajo /proc tiene un cmdline propio
    (argv con separadores NUL); si el `match` aparece ahi, el proceso
    esta corriendo. Bug real encontrado 2026-09-13: la version anterior
    solo sabia llamar a PowerShell, que no existe en Linux -- el
    servidor de Hetzner reportaba TODOS los procesos como caidos aunque
    estuvieran corriendo bien (confirmado con systemctl + logs)."""
    try:
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit():
                continue
            try:
                cmdline = (pid_dir / "cmdline").read_bytes().decode("utf-8", errors="replace")
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue  # el proceso murio entre el listado y la lectura, u otro dueño
            if match in cmdline.replace("\x00", " "):
                return True
        return False
    except Exception:
        return None


def _script_is_running(script_name: str) -> bool | None:
    """None si no se pudo verificar (no es lo mismo que 'no esta corriendo')."""
    if sys.platform != "win32":
        return _script_is_running_posix(script_name)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             f"Where-Object {{ $_.CommandLine -like '*{script_name}*' }} | Measure-Object | Select-Object -ExpandProperty Count"],
            capture_output=True, text=True, timeout=15,
        )
        return int((result.stdout or "0").strip() or "0") > 0
    except Exception:
        return None


def check_background_processes() -> list[str]:
    problems = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        if s.connect_ex(("127.0.0.1", FLASK_PORT)) != 0:
            problems.append(f"El panel web (Flask) no responde en el puerto {FLASK_PORT} -- parece caido.")

    # Se busca por RUTA ABSOLUTA dentro de ESTE proyecto, no por nombre
    # pelado -- bug real encontrado 2026-09-10: trading-bot y bot-familiar
    # tienen scripts con el mismo nombre corriendo en la misma PC durante
    # desarrollo, y un filtro por nombre solo encuentra el de CUALQUIERA
    # de los 2 proyectos (se detecto en vivo con proximity_watch.py).
    background_scripts = {
        "proximity_watch.py": "vigia de casi-señales compartido (detecta tickers cerca de disparar, cada 30s)",
        "multi_user_entry_loop.py": "motor de entradas Y salidas multi-usuario (todas las cuentas activas, cada 2 min)",
    }
    # watchdog.py (reinicia procesos caidos, chequea matematica de cuenta,
    # sincroniza respaldo) solo se corre como proceso propio en Windows
    # (PC local). En el servidor Linux, `systemd` (Restart=always en cada
    # unidad) cumple el rol de reiniciar procesos caidos -- no tiene
    # sentido pedirle a este chequeo que busque un proceso que a
    # proposito no se lanza ahi (2026-09-13).
    if sys.platform == "win32":
        background_scripts["watchdog.py"] = "censor que repara los procesos de arriba si caen o se cuelgan, cada 10 min"
    for script, description in background_scripts.items():
        running = _script_is_running(str(ROOT / script))
        if running is False:
            problems.append(f"El {description} ({script}) no parece estar corriendo.")
        elif running is None:
            problems.append(f"No se pudo verificar si {script} esta corriendo.")

    return problems


def main() -> int:
    all_problems = []
    checks = [
        ("Matematica de la cuenta", check_account_math),
        ("Precios de posiciones abiertas", check_position_prices),
        ("Posiciones duplicadas", check_duplicate_positions),
        ("Procesos de fondo", check_background_processes),
    ]

    for name, check_fn in checks:
        problems = check_fn()
        if problems:
            print(f"[{name}] {len(problems)} problema(s):")
            for msg in problems:
                print(f"  - {msg}")
                log_error(message=msg, reason=f"health_check.py: {name}")
            all_problems.extend(problems)
        else:
            print(f"[{name}] OK")

    if not all_problems:
        print("\nSin anomalias detectadas.")
        return 0
    print(f"\n{len(all_problems)} anomalia(s) en total -- registradas en el error_log del panel web.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
