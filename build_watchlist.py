#!/usr/bin/env python3
"""
Construye una watchlist de hasta 30 tickers rankeados por volumen y
volatilidad real (datos de Yahoo Finance de los ultimos dias), a partir de
un conjunto amplio de ETFs y acciones populares y liquidas para day trading.

No adivina: descarga datos reales, calcula volumen medio y rango diario
medio (%) de cada candidato, y rankea por una combinacion de ambos
(mas volumen = mas liquido/facil de entrar y salir; mas rango diario =
mas margen de movimiento intradia para el day trading).

Uso:
  python build_watchlist.py                # solo muestra el ranking
  python build_watchlist.py --apply         # ademas lo guarda como watchlist
  python build_watchlist.py --apply --top 20
"""

import argparse

import yfinance as yf

from paper_trading.engine import update_settings

CANDIDATE_POOL = [
    # ETFs de indices y sectores muy liquidos
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "XLI", "XLY", "XLC",
    "XLV", "XLP", "XLU", "SMH", "XBI", "KRE", "XOP", "JETS",
    # ETFs apalancados/volatilidad (movimiento intradia grande)
    "TQQQ", "SQQQ", "SOXL", "SOXS", "LABU", "UVXY", "GDX",
    # Acciones mega-cap de altisimo volumen
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "NFLX",
    # Semiconductores / tecnologia muy volatil
    "AMD", "INTC", "SMCI", "AVGO",
    # Alto interes especulativo / day trading retail
    "COIN", "PLTR", "MARA", "RIOT", "MSTR", "SOFI", "NIO", "RIVN", "LCID",
    "GME", "AMC", "SNAP", "UBER", "BABA", "F", "BAC", "PYPL",
]


def rank_candidates(pool: list[str]) -> list[dict]:
    data = yf.download(pool, period="5d", group_by="ticker", threads=True, progress=False)

    results = []
    for symbol in pool:
        try:
            hist = data[symbol].dropna()
            if len(hist) < 2:
                continue
            avg_volume = float(hist["Volume"].mean())
            avg_range_pct = float(((hist["High"] - hist["Low"]) / hist["Close"]).mean() * 100)
            last_close = float(hist["Close"].iloc[-1])
            results.append({
                "symbol": symbol,
                "avg_volume": avg_volume,
                "avg_daily_range_pct": avg_range_pct,
                "last_close": last_close,
            })
        except Exception:
            continue

    if not results:
        return []

    by_volume = sorted(results, key=lambda r: r["avg_volume"], reverse=True)
    by_range = sorted(results, key=lambda r: r["avg_daily_range_pct"], reverse=True)
    volume_rank = {r["symbol"]: i for i, r in enumerate(by_volume)}
    range_rank = {r["symbol"]: i for i, r in enumerate(by_range)}

    for r in results:
        r["combined_rank"] = volume_rank[r["symbol"]] + range_rank[r["symbol"]]

    results.sort(key=lambda r: r["combined_rank"])
    return results


def main():
    parser = argparse.ArgumentParser(description="Rankea candidatos para watchlist de day trading por volumen+volatilidad reales")
    parser.add_argument("--top", type=int, default=30, help="Cuantos tickers dejar en la watchlist final (default 30)")
    parser.add_argument("--apply", action="store_true", help="Guarda el resultado como watchlist en Configuracion de operativa")
    args = parser.parse_args()

    print(f"Analizando {len(CANDIDATE_POOL)} candidatos con datos reales de Yahoo Finance...")
    ranked = rank_candidates(CANDIDATE_POOL)
    top = ranked[: args.top]

    print(f"\n{'Ticker':8s} {'Precio':>10s} {'Volumen medio':>16s} {'Rango diario medio':>20s}")
    print("-" * 58)
    for r in top:
        print(f"{r['symbol']:8s} {r['last_close']:>10.2f} {r['avg_volume']:>16,.0f} {r['avg_daily_range_pct']:>19.2f}%")

    symbols = [r["symbol"] for r in top]
    print(f"\nTop {len(symbols)}: {', '.join(symbols)}")

    if args.apply:
        settings = update_settings(watchlist=symbols)
        print(f"\nWatchlist guardada ({len(settings['watchlist'])} tickers).")
    else:
        print("\n(No se guardo. Vuelve a ejecutar con --apply para fijarla como watchlist.)")


if __name__ == "__main__":
    main()
