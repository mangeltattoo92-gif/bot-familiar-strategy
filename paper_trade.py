#!/usr/bin/env python3
"""
CLI de paper trading (simulacion) para la cuenta Agentic de Robinhood.

IMPORTANTE: esta herramienta NUNCA envia ordenes reales. Solo lleva un
libro de contabilidad local en SQLite (data/paper_trading.db). Los precios
deben obtenerse en tiempo real via el MCP robinhood-trading (fuera de este
script) y pasarse aqui como argumento --price.

Ejemplos:
  python paper_trade.py init --balance 33.00
  python paper_trade.py buy --ticker BTC --asset-type crypto --quantity 0.0005 --price 62000 --reason "Momentum alcista 4h"
  python paper_trade.py sell --ticker BTC --asset-type crypto --quantity 0.0005 --price 63500 --reason "Take profit +2.4%"
  python paper_trade.py buy --ticker NFLX --asset-type option --option-type call --strike 25 --expiration 2026-10-16 --quantity 1 --price 1.20 --reason "IV baja pre-earnings"
  python paper_trade.py status --prices "BTC=63500,AAPL=230.10"
  python paper_trade.py history --limit 20
"""

import argparse
import sys
from pathlib import Path

from paper_trading.engine import (
    init_db,
    record_trade,
    get_status,
    get_history,
    PaperTradingError,
    DEFAULT_DB_PATH,
)


def parse_prices(raw: str | None) -> dict:
    if not raw:
        return {}
    prices = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        ticker, _, value = part.partition("=")
        prices[ticker.strip().upper()] = float(value)
    return prices


def cmd_init(args):
    init_db(args.balance, db_path=Path(args.db), reset=args.reset)
    print(f"Cuenta simulada inicializada con balance virtual de ${args.balance:.2f}")
    print(f"Base de datos: {args.db}")


def cmd_trade(side, args):
    result = record_trade(
        side=side,
        ticker=args.ticker,
        asset_type=args.asset_type,
        quantity=args.quantity,
        price=args.price,
        reason=args.reason,
        multiplier=args.multiplier,
        expiration=args.expiration,
        strike=args.strike,
        option_type=args.option_type,
        force=args.force,
        profit_target_pct=args.profit_target_pct,
        stop_loss_pct=args.stop_loss_pct,
        entry_delta=args.entry_delta,
        entry_signal=args.entry_signal,
        entry_volatility_strength=args.entry_volatility_strength,
        entry_band_width_pct=args.entry_band_width_pct,
        entry_band_width_percentile=args.entry_band_width_percentile,
        entry_volume_ratio=args.entry_volume_ratio,
        entry_consecutive_squeeze_bars=args.entry_consecutive_squeeze_bars,
        entry_strategy=args.entry_strategy,
        db_path=Path(args.db),
    )
    print(f"[SIMULADO] {result['side'].upper()} {result['quantity']} {result['ticker']} "
          f"@ ${result['price']:.4f} -> efecto en cash: ${result['cash_effect']:+.2f}")
    print(f"Razon: {result['reason']}")
    if result["realized_pnl_delta"]:
        print(f"P&L realizado en esta operacion: ${result['realized_pnl_delta']:+.2f}")
    print()
    _print_status(get_status(db_path=Path(args.db)))


def cmd_status(args):
    prices = parse_prices(args.prices)
    status = get_status(current_prices=prices, db_path=Path(args.db))
    _print_status(status)


def _print_status(status):
    print("=" * 60)
    print("RESUMEN CUENTA SIMULADA (paper trading)")
    print("=" * 60)
    print(f"Balance inicial:        ${status['initial_balance']:.2f}")
    print(f"Cash virtual actual:    ${status['cash_balance']:.2f}")
    print(f"Valor posiciones:       ${status['total_market_value']:.2f}")
    print(f"Valor total cuenta:     ${status['total_account_value']:.2f}")
    print(f"P&L realizado:          ${status['realized_pnl']:+.2f}")
    print(f"P&L no realizado:       ${status['unrealized_pnl']:+.2f}")
    print(f"P&L TOTAL:              ${status['total_pnl']:+.2f} ({status['total_pnl_pct']:+.2f}%)")
    print("-" * 60)
    if not status["positions"]:
        print("Sin posiciones abiertas.")
    else:
        print("Posiciones abiertas:")
        for p in status["positions"]:
            live_flag = "" if p["priced_live"] else "  (sin precio en vivo, usando coste medio)"
            label = p["ticker"]
            if p["asset_type"] == "option":
                od = p["option_details"]
                label = f"{p['ticker']} {od['option_type'].upper()} ${od['strike']} exp {od['expiration']}"
            print(f"  - [{p['asset_type']}] {label}: {p['quantity']} @ coste medio ${p['avg_cost']:.4f} "
                  f"| precio actual ${p['market_price']:.4f} | valor ${p['market_value']:.2f} "
                  f"| P&L no realizado ${p['unrealized_pnl']:+.2f}{live_flag}")
    print("=" * 60)


def cmd_history(args):
    trades = get_history(limit=args.limit, db_path=Path(args.db))
    if not trades:
        print("Sin operaciones registradas todavia.")
        return
    for t in trades:
        print(f"[{t['timestamp']}] {t['side'].upper():4s} {t['quantity']} {t['ticker']} "
              f"@ ${t['price']:.4f} | cash tras op: ${t['cash_after']:.2f} | razon: {t['reason']}")


def main():
    parser = argparse.ArgumentParser(description="Paper trading simulado (sin ordenes reales)")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Ruta a la base de datos SQLite")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Inicializa la cuenta simulada")
    p_init.add_argument("--balance", type=float, required=True, help="Balance virtual inicial (= saldo real actual)")
    p_init.add_argument("--reset", action="store_true", help="Borra historial existente y reinicia")
    p_init.set_defaults(func=cmd_init)

    def add_trade_args(p):
        p.add_argument("--ticker", required=True)
        p.add_argument("--asset-type", required=True, choices=["equity", "option", "crypto"])
        p.add_argument("--quantity", type=float, default=None,
                        help="Si se omite, usa 'contratos por operacion' de Configuracion de operativa")
        p.add_argument("--price", type=float, required=True, help="Precio de mercado en el momento de la decision (via MCP)")
        p.add_argument("--reason", required=True, help="Logica/razon detras de la decision")
        p.add_argument("--multiplier", type=float, default=1.0, help="100 para opciones, 1 para equity/crypto")
        p.add_argument("--expiration", default=None, help="YYYY-MM-DD (solo opciones)")
        p.add_argument("--strike", type=float, default=None, help="(solo opciones)")
        p.add_argument("--option-type", default=None, choices=["call", "put"], help="(solo opciones)")
        p.add_argument("--force", action="store_true", help="Forzar aunque falten fondos o posicion")
        p.add_argument("--profit-target-pct", type=float, default=None,
                        help="Plan de salida day trading: objetivo de ganancia (ej. 0.20 = +20%%), solo en compras nuevas")
        p.add_argument("--stop-loss-pct", type=float, default=None,
                        help="Plan de salida day trading: stop-loss (ej. 0.20 = -20%%), solo en compras nuevas")
        p.add_argument("--entry-delta", type=float, default=None,
                        help="Delta del contrato en el momento de la compra (ej. 0.52, o -0.47 para puts)")
        p.add_argument("--entry-signal", default=None, choices=["buy_call", "buy_put"],
                        help="Señal Bollinger que origino la compra (para el diario de operaciones)")
        p.add_argument("--entry-volatility-strength", default=None, choices=["extrema", "regular"],
                        help="Fuerza del volumen de ruptura en la entrada (de bollinger_strategy.analyze)")
        p.add_argument("--entry-band-width-pct", type=float, default=None,
                        help="Ancho de banda de Bollinger (%%) en el momento de la señal")
        p.add_argument("--entry-band-width-percentile", type=float, default=None,
                        help="Percentil del ancho de banda vs su rango reciente")
        p.add_argument("--entry-volume-ratio", type=float, default=None,
                        help="Volumen de la barra de señal / promedio reciente (ej. 5.55)")
        p.add_argument("--entry-consecutive-squeeze-bars", type=int, default=None,
                        help="Barras consecutivas en squeeze antes de la ruptura")
        p.add_argument("--entry-strategy", default=None,
                        choices=["squeeze_breakout", "squeeze_breakout_temprano", "gap_fade_apertura",
                                 "giro_sma20", "vwap_cross", "rsi_reversal"],
                        help="Cual estrategia genero la señal (para comparar rendimiento por separado). "
                             "squeeze_breakout_temprano = entrada sobre vela en formacion, ver analyze_forming_bar()")

    p_buy = sub.add_parser("buy", help="Registra una COMPRA simulada")
    add_trade_args(p_buy)
    p_buy.set_defaults(func=lambda a: cmd_trade("buy", a))

    p_sell = sub.add_parser("sell", help="Registra una VENTA simulada")
    add_trade_args(p_sell)
    p_sell.set_defaults(func=lambda a: cmd_trade("sell", a))

    p_status = sub.add_parser("status", help="Muestra balance, posiciones y P&L")
    p_status.add_argument("--prices", default=None, help="Precios actuales: 'TICKER=precio,TICKER2=precio2'")
    p_status.set_defaults(func=cmd_status)

    p_hist = sub.add_parser("history", help="Historial de operaciones simuladas")
    p_hist.add_argument("--limit", type=int, default=50)
    p_hist.set_defaults(func=cmd_history)

    args = parser.parse_args()
    try:
        args.func(args)
    except PaperTradingError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
