#!/usr/bin/env python3
"""
Motor de entradas Y salidas AUTONOMO multi-usuario -- version real de
`trading-bot/family_sim.py` (que uso 10 cuentas de PRUEBA hardcodeadas)
adaptada a los usuarios REALES del sistema (tabla `users` de
`webapp/auth.py`, solo los que estan `active`).

Cada usuario:
- Tiene su propia base de datos SQLite independiente
  (data/users/<username>/paper_trading.db) -- la MISMA ruta que usa
  webapp/app.py::_user_db_path(), para que el panel web y este motor
  vean siempre la misma cuenta.
- Comparte la MISMA señal (el watchlist se escanea UNA sola vez por
  ciclo, no una vez por usuario) pero cada uno elige su propio contrato
  segun lo que puede pagar (paper_trading/family_sizing.py).
- Tiene sus propias reglas independientes: bot_enabled,
  max_trades_per_day, max_daily_loss_pct (circuit breaker por cuenta).
- Ejecuta al ASK real (compra) / BID real (venta), no al punto medio --
  portado de trading-bot 2026-09-10.
- Respeta el cooldown de reingreso de 15 min y la ventana de apertura
  para giro_sma20 (premarket-10:30 ET), mismas reglas que trading-bot.

Uso:
  python multi_user_entry.py --once            # un ciclo (pruebas)
  python multi_user_entry.py --interval 120     # loop continuo -- OJO,
    ver multi_user_entry_loop.py para el wrapper recomendado en
    produccion (proceso fresco por ciclo, evita el problema de threads
    colgados que se encontro hoy en trading-bot con el loop largo).
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from paper_trading.bollinger_strategy import (
    NO_NEW_ENTRY_MINUTES_BEFORE_CLOSE,
    analyze,
    evaluate_open_position_exit,
    get_trend,
    scan_all_signals,
    too_close_to_open_new_position,
)
from paper_trading.engine import (
    _connect,
    get_daily_realized_pnl,
    get_settings,
    get_status,
    init_db,
    log_error,
    record_trade,
    update_settings,
)
from paper_trading.family_sizing import select_affordable_contract
from paper_trading.option_selection import MAX_SPREAD_PCT
from paper_trading.singleton_lock import acquire_single_instance_lock
from webapp import market_data
from webapp.auth import STATUS_ACTIVE, list_all_users

DATA_DIR = ROOT / "data"
USERS_DATA_DIR = DATA_DIR / "users"
LOG_PATH = DATA_DIR / "multi_user_entry.log"

DEFAULT_INTERVAL_SECONDS = 120
REENTRY_COOLDOWN_MINUTES = 15
DEFAULT_INITIAL_BALANCE = 10000.0  # mismo placeholder que webapp/app.py hasta que cada quien fije su capital real

CONFIDENCE_ORDER = {"alta": 0, "media": 1, "baja": 2}
ALLOW_LOW_CONFIDENCE = True  # mismo criterio que trading-bot hoy -- revisar si genera señales de mala calidad

MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN_BAR_TIME = dtime(9, 30)
GIRO_SMA20_WINDOW_END = dtime(10, 30)


def _giro_sma20_allowed_now() -> bool:
    now_et = datetime.now(MARKET_TZ).time()
    return now_et < MARKET_OPEN_BAR_TIME or now_et <= GIRO_SMA20_WINDOW_END


def db_path_for(username: str) -> Path:
    return USERS_DATA_DIR / username / "paper_trading.db"


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def active_accounts() -> list[tuple[str, Path]]:
    """(username, db_path) de cada usuario ACTIVO -- crea su base de
    datos sola con el balance placeholder si todavia no existe (se
    registro y activo pero nunca abrio su panel web)."""
    accounts = []
    for u in list_all_users():
        if u["status"] != STATUS_ACTIVE:
            continue
        path = db_path_for(u["username"])
        if not path.exists():
            init_db(initial_balance=DEFAULT_INITIAL_BALANCE, db_path=path)
            _log(f"[{u['username']}] cuenta creada -- capital inicial ${DEFAULT_INITIAL_BALANCE:,.2f}")
        accounts.append((u["username"], path))
    return accounts


def _minutes_since_last_trade(ticker: str, db_path: Path) -> float | None:
    conn = _connect(db_path)
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


def _current_price(position: dict):
    if position["asset_type"] == "equity":
        return market_data.get_quote(position["ticker"])
    if position["asset_type"] == "crypto":
        return market_data.get_quote(f"{position['ticker']}-USD")
    if position["asset_type"] == "option":
        od = position["option_details"]
        return market_data.get_option_price(position["ticker"], od["expiration"], od["strike"], od["option_type"])
    return None


def _current_exit_price(position: dict):
    """Ver la misma nota en trading-bot/position_monitor.py -- portado
    aca 2026-09-11: si el spread del contrato esta demasiado ancho AHORA
    (no al entrar, sino en el momento de decidir el cierre), el BID no es
    un precio confiable para evaluar stop_loss/profit_target -- cae al
    punto medio en vez de castigar la posicion por ruido de cotizacion
    que nunca fue una perdida real."""
    if position["asset_type"] != "option":
        return _current_price(position)
    od = position["option_details"]
    bid, ask = market_data.get_option_bid_ask(position["ticker"], od["expiration"], od["strike"], od["option_type"])
    if bid is not None and ask is not None and ask > 0 and (ask - bid) / ask > MAX_SPREAD_PCT:
        return _current_price(position)  # spread demasiado ancho ahora mismo -- BID no es confiable
    return bid if bid is not None else _current_price(position)


def _is_near_expiration(position: dict, now_utc: datetime) -> bool:
    if position["asset_type"] != "option":
        return False
    exp_date = datetime.strptime(position["option_details"]["expiration"], "%Y-%m-%d").date()
    return (exp_date - now_utc.date()).days <= 1


def _check_circuit_breaker(db_path: Path, username: str, current_prices: dict[str, float]) -> None:
    settings = get_settings(db_path)
    if not settings["bot_enabled"]:
        return
    status = get_status(current_prices=current_prices, db_path=db_path)
    if status["initial_balance"] <= 0:
        return
    daily_realized = get_daily_realized_pnl(db_path)
    daily_pct = (daily_realized + status["unrealized_pnl"]) / status["initial_balance"] * 100
    if daily_pct <= -settings["max_daily_loss_pct"]:
        update_settings(bot_enabled=False, db_path=db_path)
        msg = (f"[{username}] CIRCUIT BREAKER: perdida del dia {daily_pct:+.2f}% supera "
               f"-{settings['max_daily_loss_pct']:.1f}% -- bot apagado para ESTA cuenta unicamente.")
        log_error(msg, reason="multi_user_entry.py: circuit breaker", db_path=db_path)
        _log(msg)


def run_exits_for_account(username: str, db_path: Path) -> int:
    status = get_status(db_path=db_path)
    closed = 0
    current_prices: dict[str, float] = {}
    for p in status["positions"]:
        price = _current_price(p)
        if price is None:
            continue
        current_prices[p["key"] if p["asset_type"] == "option" else p["ticker"]] = price
        exit_price = _current_exit_price(p)
        if exit_price is None:
            exit_price = price

        opened = datetime.fromisoformat(p["opened_at"])
        now_utc = datetime.now(timezone.utc)
        minutes_since_entry = (now_utc - opened).total_seconds() / 60

        exit_signal, hourly_aligned = False, True
        if p["asset_type"] == "option":
            try:
                r = analyze(p["ticker"])
                exit_signal = (r["exit_short_signal"] if p["option_details"]["option_type"] == "put"
                                else r["exit_long_signal"])
            except Exception:
                exit_signal = False
            try:
                trend_1h = get_trend(p["ticker"], interval="1h", period="3mo")
            except Exception:
                trend_1h = None
            wanted = "bajista" if p["option_details"]["option_type"] == "put" else "alcista"
            hourly_aligned = trend_1h == wanted

        result = evaluate_open_position_exit(
            entry_premium=p["avg_cost"], current_premium=exit_price,
            profit_target_pct=p["profit_target_pct"] or 0.10, technical_exit_signal=exit_signal,
            hourly_aligned=hourly_aligned, force_eod_exit=_is_near_expiration(p, now_utc),
            stop_loss_pct=p["stop_loss_pct"] or 0.20, minutes_since_entry=minutes_since_entry,
        )
        if result["should_close"]:
            reason = (f"[multi_user_entry -- cuenta {username}] Cierre automatico por regla "
                      f"'{result['reason']}' (P&L {result['pnl_pct']*100:+.2f}%). Ejecutado al BID real "
                      f"${exit_price:.2f}.")
            record_trade(
                side="sell", ticker=p["ticker"], asset_type=p["asset_type"], quantity=p["quantity"],
                price=exit_price, reason=reason, multiplier=p["multiplier"],
                expiration=p["option_details"]["expiration"] if p["asset_type"] == "option" else None,
                strike=p["option_details"]["strike"] if p["asset_type"] == "option" else None,
                option_type=p["option_details"]["option_type"] if p["asset_type"] == "option" else None,
                db_path=db_path,
            )
            _log(f"[{username}] CIERRE {p['ticker']} -> {result['reason']} "
                 f"({result['pnl_pct']*100:+.2f}%) @ {exit_price:.2f} (bid real)")
            closed += 1
    _check_circuit_breaker(db_path, username, current_prices)
    return closed


def run_entry_cycle(accounts: list[tuple[str, Path]]) -> None:
    if not accounts:
        _log("Sin usuarios activos todavia -- nada que escanear.")
        return

    # Mismo rule que trading-bot/auto_entry.py -- no abrir posiciones
    # NUEVAS a menos de NO_NEW_ENTRY_MINUTES_BEFORE_CLOSE minutos del
    # cierre (day trading, sin overnight para ENTRADAS nuevas -- las
    # posiciones YA abiertas si se mantienen overnight, eso no cambia).
    # Bug encontrado 2026-09-10: esto se habia olvidado al portar
    # family_sim.py a multi_user_entry.py.
    if too_close_to_open_new_position():
        _log(f"Cerca del cierre de sesion (<= {NO_NEW_ENTRY_MINUTES_BEFORE_CLOSE} min) o mercado cerrado -- "
             f"no se abren posiciones nuevas para ninguna cuenta.")
        return

    settings_main = get_settings()  # watchlist compartida (DB por defecto, no ligada a ningun usuario)
    watchlist = list(dict.fromkeys(settings_main["watchlist"] + settings_main["fast_watchlist"]))

    def _scan(sym):
        try:
            return sym, scan_all_signals(sym), None
        except Exception as e:
            return sym, None, e

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(_scan, watchlist))

    signals = []
    for symbol, strategy_results, error in results:
        if error is not None:
            _log(f"[scan] ERROR {symbol}: {error}")
            continue
        signals.extend(r for r in strategy_results if r["signal"] != "none")
    signals.sort(key=lambda r: (CONFIDENCE_ORDER[r["confidence"]], -r.get("volume_ratio", 0)))

    if not signals:
        _log(f"Sin señales de entrada este ciclo (escaneo compartido para {len(accounts)} cuenta(s)).")
        return

    unique_symbols = {r["symbol"] for r in signals}
    spot_prices = {sym: market_data.get_quote(sym) for sym in unique_symbols}

    for username, db_path in accounts:
        settings = get_settings(db_path)
        if not settings["bot_enabled"]:
            continue
        status = get_status(db_path=db_path)
        open_tickers = {p["ticker"] for p in status["positions"]}
        trades_this_cycle = 0
        used_tickers = set()

        for r in signals:
            if trades_this_cycle >= settings["max_trades_per_day"]:
                break
            if r["confidence"] == "baja" and not ALLOW_LOW_CONFIDENCE:
                continue
            if r["symbol"] in open_tickers or r["symbol"] in used_tickers:
                continue
            if r["strategy"] == "giro_sma20" and not _giro_sma20_allowed_now():
                continue
            minutes_since = _minutes_since_last_trade(r["symbol"], db_path)
            if minutes_since is not None and minutes_since < REENTRY_COOLDOWN_MINUTES:
                continue

            spot = spot_prices.get(r["symbol"])
            if spot is None:
                continue
            option_type = "call" if r["signal"] == "buy_call" else "put"
            # Tamano de posicion por riesgo (cada usuario tiene su propio
            # settings["risk_pct_per_trade"], % de SU valor de cuenta) --
            # 2026-09-13, reemplaza el contracts_per_trade fijo. La
            # cantidad se deriva DENTRO de select_affordable_contract()
            # segun el precio real del contrato elegido.
            risk_pct = float(settings["risk_pct_per_trade"])
            contract, method, quantity = select_affordable_contract(
                r["symbol"], option_type, spot, status["cash_balance"], status["total_account_value"], risk_pct,
            )
            if contract is None:
                continue

            fill_price = contract.get("ask") or contract["price"]
            reason = (
                f"[multi_user_entry -- cuenta {username}] Señal {r['strategy']} "
                f"{r['signal'].upper()} (confianza {r['confidence']}). Contrato elegido: {method}. "
                f"Ejecutado al ASK real ${fill_price:.2f}. {r['reason']}"
            )
            try:
                record_trade(
                    side="buy", ticker=r["symbol"], asset_type="option", quantity=quantity,
                    price=fill_price, reason=reason, multiplier=100.0,
                    expiration=contract["expiration"], strike=contract["strike"], option_type=option_type,
                    profit_target_pct=r.get("profit_target_pct"), stop_loss_pct=r.get("stop_loss_pct"),
                    entry_delta=contract["delta"], entry_signal=r["signal"],
                    entry_volatility_strength=r.get("volatility_strength"),
                    entry_band_width_pct=r.get("band_width_pct"),
                    entry_band_width_percentile=r.get("band_width_percentile"),
                    entry_volume_ratio=r.get("volume_ratio"),
                    entry_consecutive_squeeze_bars=r.get("consecutive_squeeze_bars"),
                    entry_strategy=r["strategy"], db_path=db_path,
                )
                _log(f"[{username}] EJECUTADO {r['signal'].upper()} {r['symbol']} strike {contract['strike']} "
                     f"@ {fill_price:.2f} ask real ({method})")
                trades_this_cycle += 1
                used_tickers.add(r["symbol"])
                status = get_status(db_path=db_path)
            except Exception as e:
                _log(f"[{username}] OMITIDO {r['symbol']}: {e}")


def run_cycle() -> None:
    accounts = active_accounts()
    closed_total = 0
    for username, db_path in accounts:
        closed_total += run_exits_for_account(username, db_path)
    run_entry_cycle(accounts)
    if closed_total:
        _log(f"Ciclo completo -- {closed_total} posicion(es) cerradas en total esta vuelta.")


def main():
    parser = argparse.ArgumentParser(description="Motor de entradas y salidas multi-usuario (usuarios reales, activos)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        run_cycle()
        return

    acquire_single_instance_lock("multi_user_entry")
    _log(f"multi_user_entry.py iniciado -- ciclo cada {args.interval}s.")
    while True:
        try:
            run_cycle()
        except Exception as e:
            _log(f"ERROR en el ciclo: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
