#!/usr/bin/env python3
"""
Instalacion inicial para una copia PERSONAL de este bot -- para cada
persona que corre su propia instancia en su propia computadora (no
comparte cuenta ni datos con nadie mas).

SOLO WINDOWS por ahora: el sistema de lock (paper_trading/singleton_lock.py,
usa msvcrt) y el censor (watchdog.py, usa PowerShell) son especificos de
Windows. En Mac/Linux no van a funcionar sin adaptarlos primero.

NO necesita cuenta de Robinhood -- el motor autonomo (auto_entry.py) usa
solo datos publicos de yfinance. El MCP de Robinhood es opcional, solo
para quien quiera el flujo manual de mas precision con un agente de
Claude Code.

Uso:
  python setup.py
"""

import getpass
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print(f"\nFallo: {' '.join(cmd)}")
        sys.exit(1)


def main():
    print("=" * 60)
    print("Instalacion de tu copia personal del bot de paper trading")
    print("=" * 60)

    if sys.platform != "win32":
        print("\nAVISO: este bot esta armado para Windows (msvcrt/PowerShell).")
        print("En Mac/Linux no va a arrancar sin adaptar singleton_lock.py y watchdog.py primero.")
        if input("Continuar de todos modos? (s/N): ").strip().lower() != "s":
            sys.exit(0)

    print("\n[1/4] Instalando dependencias (requirements.txt)...")
    _run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])

    print("\n[2/4] Balance inicial de tu cuenta simulada.")
    while True:
        raw = input("Cuanto capital virtual queres empezar? (ej. 10000): ").strip()
        try:
            balance = float(raw)
            break
        except ValueError:
            print("Poné un numero, ej. 10000")
    _run([sys.executable, "paper_trade.py", "init", "--balance", str(balance)])

    print("\n[3/4] Tu usuario y contrasena para el panel web (127.0.0.1:5000).")
    username = input("Nombre de usuario: ").strip()
    _run([sys.executable, "webapp/manage_users.py", username])

    print("\n[4/4] Listo.")
    print("=" * 60)
    print(f"Tu cuenta simulada arranca con ${balance:,.2f}.")
    print("Para prender el bot (todos los procesos de una vez):")
    print("    python start_bot.py")
    print("Panel web: http://127.0.0.1:5000")
    print("=" * 60)


if __name__ == "__main__":
    main()
