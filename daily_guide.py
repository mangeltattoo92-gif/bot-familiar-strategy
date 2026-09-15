#!/usr/bin/env python3
"""
Motor de guia diaria por ticker (2026-09-15, a pedido del usuario: "una
guia segura en el day trading diario... que ayude a tomar mejores
decisiones"). Corre 1 vez por dia, antes de la apertura, y backtestea
cada ticker de la watchlist sobre los ultimos 60 dias REALES (reusa
backtest.py tal cual -- no reimplementa esa logica, misma fuente de
verdad que el backtest manual que ya existia).

Arma un veredicto simple por ticker segun como le fue HISTORICAMENTE a
las estrategias reales en ESE ticker especifico:
  - 'seguro':    win rate >= 55%, con muestra suficiente (n >= MIN_SAMPLE)
  - 'neutral':   45-55% win rate, o muestra insuficiente para confiar del todo
  - 'cuidado':   < 45% win rate con muestra suficiente -- historicamente
                 este ticker le costo mas de lo que le dio a las estrategias
  - 'sin_datos': ninguna señal disparo en 60 dias (no hay con que juzgar)

Dos usos, NINGUNO reemplaza el analisis tecnico en vivo de cada ciclo:
  1. Se guarda en data/daily_guide.json -- el panel web lo muestra para
     que el usuario lo lea.
  2. multi_user_entry.py lo consulta antes de comprar: en tickers
     'cuidado' exige confianza ALTA (no alcanza con media), como capa
     extra de prudencia sobre lo que ya filtra por señal individual.

Uso:
  python daily_guide.py              # corre completo, guarda el JSON
  python daily_guide.py --tickers AAPL,GLD   # subset, para pruebas rapidas
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backtest import backtest_ticker, summarize  # noqa: E402
from paper_trading.engine import get_settings  # noqa: E402

GUIDE_PATH = ROOT / "data" / "daily_guide.json"
MIN_SAMPLE_FOR_VERDICT = 5  # menos de esto, no hay muestra suficiente para confiar en el win rate


def verdict_for(summary: dict) -> str:
    n = summary["n"]
    if n == 0:
        return "sin_datos"
    if n < MIN_SAMPLE_FOR_VERDICT:
        return "neutral"
    wr = summary["win_rate"]
    if wr >= 55:
        return "seguro"
    if wr >= 45:
        return "neutral"
    return "cuidado"


def build_guide(symbols: list[str]) -> dict:
    def _run(sym):
        try:
            trades = backtest_ticker(sym)
            return sym, summarize(trades), None
        except Exception as e:
            return sym, None, str(e)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_run, symbols))

    guide = {}
    for sym, summary, error in results:
        if error or summary is None:
            guide[sym] = {"verdict": "sin_datos", "n": 0, "win_rate": None,
                          "avg_pnl_pct": None, "total_pnl_pct": None, "error": error}
            continue
        guide[sym] = {
            "verdict": verdict_for(summary),
            "n": summary["n"],
            "win_rate": summary["win_rate"],
            "avg_pnl_pct": summary["avg_pnl_pct"],
            "total_pnl_pct": summary["total_pnl_pct"],
        }
    return guide


def save_guide(guide: dict) -> None:
    GUIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tickers": guide}
    with open(GUIDE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_guide() -> dict:
    """Usado por multi_user_entry.py -- si el archivo no existe todavia
    (primera vez, o fallo el cron de anoche) devuelve {} y el llamador
    debe tratar CUALQUIER ticker como si no hubiera veredicto (no
    bloquear nada por falta de datos, ver nota en multi_user_entry.py)."""
    try:
        with open(GUIDE_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("tickers", {})
    except Exception:
        return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default=None)
    args = parser.parse_args()

    if args.tickers:
        symbols = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        settings = get_settings()
        symbols = list(dict.fromkeys(settings["watchlist"] + settings["fast_watchlist"]))

    print(f"Backtesteando {len(symbols)} tickers para la guia diaria...")
    guide = build_guide(symbols)
    save_guide(guide)

    seguro = [s for s, g in guide.items() if g["verdict"] == "seguro"]
    cuidado = [s for s, g in guide.items() if g["verdict"] == "cuidado"]
    sin_datos = [s for s, g in guide.items() if g["verdict"] == "sin_datos"]
    print(f"Guia actualizada ({GUIDE_PATH}): {len(seguro)} seguros, {len(cuidado)} con cuidado, "
          f"{len(sin_datos)} sin datos, {len(guide)} total.")
    if cuidado:
        print("Cuidado con:", ", ".join(f"{s} ({guide[s]['win_rate']}% wr, n={guide[s]['n']})" for s in cuidado))


if __name__ == "__main__":
    main()
