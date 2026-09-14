#!/usr/bin/env python3
"""
Muestra el contador REAL de tokens gastados por sub-agentes de Claude en
este proyecto (data/token_counter.json). Standalone, de solo lectura --
no toca ni afecta al bot. NO incluye tokens de la conversacion directa
(no medibles por Claude, ver la nota dentro del JSON).

Uso:
  python token_counter.py
"""

import json
from pathlib import Path

COUNTER_PATH = Path(__file__).resolve().parent / "data" / "token_counter.json"


def main():
    data = json.loads(COUNTER_PATH.read_text(encoding="utf-8"))
    print("=" * 60)
    print("CONTADOR DE TOKENS -- solo sub-agentes (dato real, no estimado)")
    print("=" * 60)
    print(f"Total gastado por sub-agentes completados: {data['total_subagent_tokens']:,}")
    print(f"Sub-agentes completados: {data['total_subagents_completed']}")
    print(f"Sub-agentes fallidos (sin reportar uso): {data['total_subagents_failed']}")
    print("-" * 60)
    for e in data["events"]:
        tokens = f"{e['tokens']:,}" if e.get("tokens") is not None else "?"
        print(f"[{e['date']}] {e['agent']}: {tokens} tokens ({e['outcome']})")
    print("=" * 60)


if __name__ == "__main__":
    main()
