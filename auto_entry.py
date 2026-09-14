#!/usr/bin/env python3
"""
Motor de entradas 100% AUTONOMO -- corre en bucle continuo (por defecto
cada 15 minutos, alineado a las velas de 15m) escaneando la watchlist con
las mismas estrategias de siempre (scan_all_signals) y EJECUTANDO
(registrando en el simulador) las señales de confianza alta/media, sin
depender de que un agente lo corra a mano.

Por que existe (2026-09-09): watchlist_scan.py solo corria cuando el
usuario le pedia al agente que lo corriera -- una ruptura real de SPY a
las 11:00-11:15 ET se perdio porque nadie escaneo justo en ese momento (el
motor de señales SI la habia detectado correctamente en retrospectiva: no
fue un bug de la estrategia, fue falta de un escaneo recurrente). Este
script cierra ese hueco.

DIFERENCIA con el flujo manual del agente: la seleccion de contrato aqui
usa yfinance + Black-Scholes aproximado (paper_trading/option_selection.py)
en vez del MCP de Robinhood -- prioriza disponibilidad 24/7 sobre la
precision de delta/precio en vivo que da el agente. Las señales de
confianza BAJA NUNCA se auto-ejecutan aqui tampoco -- se registran para
revision manual, igual que en watchlist_scan.py.

NUNCA coloca ordenes reales -- solo registra en el simulador (SQLite
local), igual que el resto del sistema.

Uso:
  python auto_entry.py --once          # un solo ciclo (pruebas)
  python auto_entry.py --interval 900  # bucle continuo (produccion, 15 min)
"""

import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from paper_trading.bollinger_strategy import (
    NO_NEW_ENTRY_MINUTES_BEFORE_CLOSE,
    scan_all_signals,
    too_close_to_open_new_position,
)
from paper_trading.engine import (
    DEFAULT_DB_PATH,
    PaperTradingError,
    _connect,
    get_settings,
    get_status,
    get_trades_count_today,
    log_error,
    record_trade,
)
from paper_trading.option_selection import select_contract
from paper_trading.singleton_lock import acquire_single_instance_lock
from webapp import market_data

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOG_PATH = DATA_DIR / "auto_entry.log"

DEFAULT_INTERVAL_SECONDS = 120  # 2 minutos, a pedido del usuario (2026-09-09)
MAX_PARALLEL_REQUESTS = 8
CONFIDENCE_ORDER = {"alta": 0, "media": 1, "baja": 2}

# Una vela de 15m completa. Evita re-comprar el mismo ticker apenas se
# cierra una posicion sobre la MISMA señal (el bar cerrado no cambia hasta
# que pasan 15 min, asi que sin esto un ciclo de 2 min puede re-entrar
# segundos despues de haber tomado ganancia -- paso de verdad con HOOD el
# 2026-09-09: cerro con +10.27% y se re-compro 19s despues sobre la misma
# vela, ver INCIDENTS.md). El cooldown corre desde el ULTIMO trade
# (compra o venta) de ese ticker, no solo desde una compra.
REENTRY_COOLDOWN_MINUTES = 15


def _minutes_since_last_trade(ticker: str) -> float | None:
    """Minutos desde el ultimo trade (compra o venta) de `ticker`, o None
    si nunca se opero ese ticker."""
    conn = _connect(DEFAULT_DB_PATH)
    try:
        row = conn.execute(
            "SELECT timestamp FROM trades WHERE ticker = ? ORDER BY id DESC LIMIT 1", (ticker,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    last_ts = datetime.fromisoformat(row["timestamp"])
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last_ts).total_seconds() / 60


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _scan_one(symbol: str):
    try:
        return symbol, scan_all_signals(symbol), None
    except Exception as e:
        return symbol, None, e


def _execute_signal(r: dict) -> bool:
    option_type = "call" if r["signal"] == "buy_call" else "put"
    spot = market_data.get_quote(r["symbol"])
    if spot is None:
        _log(f"[{r['symbol']}] OMITIDO -- no se pudo obtener precio spot via yfinance.")
        return False

    contract = select_contract(r["symbol"], option_type, spot)
    if contract is None:
        _log(f"[{r['symbol']}] OMITIDO -- no se encontro contrato utilizable via yfinance "
             f"(sin datos de opciones o sin cotizacion bid/ask en vivo).")
        return False

    reason = (
        f"[AUTO-ENTRY -- ciclo autonomo cada {DEFAULT_INTERVAL_SECONDS // 60} min, SIN agente activo] "
        f"Senal {r['strategy']} {r['signal'].upper()} (confianza {r['confidence']}, "
        f"volumen {r.get('volume_ratio', 0):.2f}x). {r['reason']} "
        f"Contrato elegido via yfinance con delta Black-Scholes aproximado {contract['delta']:+.3f}, "
        f"open interest {contract['open_interest']:,} / volumen {contract['volume']:,} "
        f"(filtro de liquidez real aplicado, no solo bid/ask no-cero -- ver MIN_OPEN_INTEREST en "
        f"paper_trading/option_selection.py) (NO via MCP de Robinhood). "
        f"Vencimiento {contract['expiration']} (mas cercano a 7 dias habiles)."
    )
    try:
        record_trade(
            side="buy", ticker=r["symbol"], asset_type="option",
            quantity=None, price=contract["price"], reason=reason, multiplier=100.0,
            expiration=contract["expiration"], strike=contract["strike"], option_type=option_type,
            profit_target_pct=r.get("profit_target_pct"), stop_loss_pct=r.get("stop_loss_pct"),
            entry_delta=contract["delta"], entry_signal=r["signal"],
            entry_volatility_strength=r.get("volatility_strength"),
            entry_band_width_pct=r.get("band_width_pct"),
            entry_band_width_percentile=r.get("band_width_percentile"),
            entry_volume_ratio=r.get("volume_ratio"),
            entry_consecutive_squeeze_bars=r.get("consecutive_squeeze_bars"),
            entry_strategy=r["strategy"],
        )
    except PaperTradingError as e:
        _log(f"[{r['symbol']}] ERROR al registrar la operacion: {e}")
        return False

    _log(f"[{r['symbol']}] EJECUTADO {r['signal'].upper()} ({r['strategy']}, confianza {r['confidence']}) "
         f"strike {contract['strike']} exp {contract['expiration']} @ {contract['price']:.4f} "
         f"(delta {contract['delta']:+.3f})")
    return True


def run_cycle() -> int:
    settings = get_settings()
    if not settings["bot_enabled"]:
        _log("bot_enabled=False -- no se abren posiciones nuevas este ciclo.")
        return 0
    if too_close_to_open_new_position():
        _log(f"Cerca del cierre de sesion (<= {NO_NEW_ENTRY_MINUTES_BEFORE_CLOSE} min) o mercado cerrado -- "
             f"no se abren posiciones nuevas.")
        return 0

    max_trades = settings["max_trades_per_day"]
    trades_today = get_trades_count_today()
    remaining = max(0, max_trades - trades_today)
    if remaining <= 0:
        _log(f"Limite diario alcanzado ({trades_today}/{max_trades}) -- no se abren posiciones nuevas.")
        return 0

    # Sin esto, un mismo cierre de vela puede re-comprarse en cada ciclo de
    # 2 min mientras siga siendo la ultima vela cerrada -- ya paso en
    # pruebas (SPY PUT termino en 2.0 contratos por dos ciclos separados
    # sobre la misma señal). Corriendo sin supervision humana en el loop,
    # la regla segura por defecto es: no agregar a un ticker que ya tiene
    # posicion abierta, sin importar de que estrategia/ciclo vino.
    open_tickers = {p["ticker"] for p in get_status()["positions"]}

    symbols = list(dict.fromkeys(settings["watchlist"] + settings["fast_watchlist"]))
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_REQUESTS) as pool:
        results = list(pool.map(_scan_one, symbols))

    signals = []
    for symbol, strategy_results, error in results:
        if error is not None:
            _log(f"[{symbol}] ERROR en el escaneo: {error}")
            continue
        signals.extend(r for r in strategy_results if r["signal"] != "none")

    if not signals:
        _log("Sin señales de entrada este ciclo.")
        return 0

    signals.sort(key=lambda r: (CONFIDENCE_ORDER[r["confidence"]], -r.get("volume_ratio", 0)))

    executed = 0
    used_tickers = set()
    for i, r in enumerate(signals):
        if executed >= remaining:
            # A partir de aca, executed ya no puede volver a subir en este
            # ciclo -- seguir iterando solo repetiria el mismo log por cada
            # señal restante sin ningun efecto (encontrado en el review del
            # 2026-09-09, ver INCIDENTS.md). Un solo resumen y corta.
            omitted = len(signals) - i
            _log(f"Limite diario alcanzado -- {omitted} señal(es) restante(s) de este ciclo se omiten.")
            break
        if r["confidence"] == "baja":
            _log(f"[{r['symbol']}] REVISION MANUAL -- confianza baja (contradice tendencia mayor), "
                 f"no se auto-ejecuta.")
            continue
        if r["symbol"] in open_tickers:
            continue  # ya tiene posicion abierta -- no se agrega, ver nota arriba
        if r["symbol"] in used_tickers:
            continue
        minutes_since = _minutes_since_last_trade(r["symbol"])
        if minutes_since is not None and minutes_since < REENTRY_COOLDOWN_MINUTES:
            _log(f"[{r['symbol']}] OMITIDO -- ultimo trade hace {minutes_since:.1f} min "
                 f"(cooldown de {REENTRY_COOLDOWN_MINUTES} min tras cualquier compra/venta de este ticker).")
            continue
        if _execute_signal(r):
            executed += 1
            used_tickers.add(r["symbol"])

    return executed


def main():
    acquire_single_instance_lock("auto_entry")
    parser = argparse.ArgumentParser(description="Motor de entradas autonomo (sin agente) -- escanea y ejecuta cada N segundos")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS,
                         help=f"Segundos entre ciclos (default {DEFAULT_INTERVAL_SECONDS})")
    parser.add_argument("--once", action="store_true", help="Corre un solo ciclo y termina (para pruebas)")
    args = parser.parse_args()

    if args.once:
        n = run_cycle()
        _log(f"Ciclo unico completado -- {n} operacion(es) ejecutada(s).")
        return

    _log(f"Motor de entradas autonomo iniciado -- ciclo cada {args.interval}s. Ctrl+C para detener.")
    while True:
        try:
            run_cycle()
        except Exception as e:
            _log(f"ERROR en el ciclo (no se detiene, reintenta en el proximo): {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
