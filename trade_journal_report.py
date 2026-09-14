#!/usr/bin/env python3
"""
Informe del diario de operaciones: rendimiento historico de las
operaciones cerradas, desglosado por las condiciones de entrada
(fuerza del volumen, delta, que tan apretadas estaban las bandas).

Uso:
  python trade_journal_report.py
"""

from paper_trading.trade_journal import (
    MIN_SAMPLE_SIZE_FOR_CONFIDENCE,
    overall_performance,
    performance_by_delta_bucket,
    performance_by_squeeze_strength,
    performance_by_volatility_strength,
)


def _print_bucket_table(title, buckets):
    print(f"\n{title}")
    print("-" * len(title))
    if not buckets:
        print("  (sin datos)")
        return
    for label, stats in buckets.items():
        if stats["n"] == 0:
            continue
        flag = "" if stats["reliable"] else "  *muestra pequeña*"
        print(f"  {label:20s} n={stats['n']:<3d} win rate={stats['win_rate']}%  "
              f"P&L medio={stats['avg_pnl_pct']}%  P&L total=${stats['total_pnl']}{flag}")


def main():
    overall = overall_performance()
    print("=" * 60)
    print("DIARIO DE OPERACIONES -- rendimiento historico")
    print("=" * 60)
    if overall["n"] == 0:
        print("Todavia no hay operaciones cerradas (compra + venta) registradas.")
        return

    print(f"Operaciones cerradas: {overall['n']}")
    print(f"Win rate: {overall['win_rate']}%")
    print(f"P&L medio por operacion: {overall['avg_pnl_pct']}%  (${overall['avg_pnl']})")
    print(f"P&L total acumulado: ${overall['total_pnl']}")
    if not overall["reliable"]:
        print(f"\n*AVISO*: menos de {MIN_SAMPLE_SIZE_FOR_CONFIDENCE} operaciones. "
              "Estas cifras todavia no son estadisticamente fiables -- son solo un registro, no una conclusion.")

    _print_bucket_table("Por fuerza de volumen en la entrada", performance_by_volatility_strength())
    _print_bucket_table("Por rango de delta en la entrada", performance_by_delta_bucket())
    _print_bucket_table("Por que tan apretadas estaban las bandas", performance_by_squeeze_strength())


if __name__ == "__main__":
    main()
