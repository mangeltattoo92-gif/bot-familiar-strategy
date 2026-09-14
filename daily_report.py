#!/usr/bin/env python3
"""
Comparativa diaria de rendimiento -- HOY vs AYER y los ultimos 7 dias.

Standalone, de SOLO LECTURA -- no toca, no reinicia, no afecta en nada a
position_monitor.py / proximity_watch.py / auto_entry.py / watchdog.py.
Solo lee la base de datos. A pedido del usuario (2026-09-09).

Uso:
  python daily_report.py
"""

from paper_trading.engine import get_status
from paper_trading.trade_journal import daily_breakdown


def main():
    days = daily_breakdown(days=7)
    status = get_status()

    print("=" * 62)
    print("COMPARATIVA DIARIA -- rendimiento del bot")
    print("=" * 62)
    print(f"{'Fecha':12} {'Trades':>7} {'Ganados':>8} {'Perdidos':>9} {'P&L $':>11}")
    for d in days:
        print(f"{d['date']:12} {d['trades']:7} {d['wins']:8} {d['losses']:9} {d['pnl']:+11.2f}")

    if len(days) >= 2 and days[-2]["trades"] > 0:
        today, yesterday = days[-1], days[-2]
        pnl_diff = today["pnl"] - yesterday["pnl"]
        trades_diff = today["trades"] - yesterday["trades"]
        veredicto = "mejor" if pnl_diff > 0 else "peor" if pnl_diff < 0 else "igual"
        print("-" * 62)
        print(f"HOY vs AYER: P&L {pnl_diff:+.2f}$ ({veredicto}), trades {trades_diff:+d}")
    elif days[-1]["trades"] == 0:
        print("-" * 62)
        print("Sin operaciones hoy todavia -- nada que comparar.")

    print("-" * 62)
    print(f"Cuenta total: ${status['total_account_value']:.2f} "
          f"(P&L acumulado {status['total_pnl_pct']:+.2f}%)")
    print("=" * 62)


if __name__ == "__main__":
    main()
