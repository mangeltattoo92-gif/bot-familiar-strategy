#!/usr/bin/env python3
"""
Motor de ORDENES REALES en Tradier -- 2026-10-10, a pedido explicito del
usuario ("si construye el motor que coloque las ordenes reales en
Tradier"): hasta ahora tradier_cycle.py solo usaba Tradier como fuente de
DATOS; este script es el que de verdad coloca ordenes en la cuenta
conectada de cada usuario que activo el interruptor "operar con dinero
real" (ver webapp/brokers/store.py set_live_trading / dashboard.html).

Reutiliza TODO lo ya construido y probado en vivo esta sesion:
  - webapp/brokers/tradier.py (cliente, place_option_order, place_oco)
  - webapp/brokers/pricing.py (entry_limit/exit_limit, select_contract,
    build_exit_oco, order_needs_attention, effective_window)
  - webapp/brokers/store.py (conexiones cifradas por usuario)
  - webapp/brokers/orders.py (historial de ordenes reales, nuevo)
  - paper_trading/bollinger_strategy.py (las MISMAS estrategias, via el
    mismo _ACTIVE_FETCHER que ya usa tradier_cycle.py)

Un ciclo, por cada conexion con dinero real activado:
  1. Revisa ordenes de entrada pendientes -- si ya llenaron, lo registra.
  2. Para entradas llenas sin salida, coloca la orden OCO (meta + stop).
  3. Si no hay accion reciente (lock de 5 min) y no se llego al limite
     diario, escanea la MISMA watchlist/estrategias del usuario y, si
     hay una señal de confianza media o alta, coloca UNA orden de
     entrada real.

Nunca corre mas de una cuenta a la vez en el mismo hilo (secuencial,
simple) -- el volumen de cuentas reales conectadas hoy es chico.
"""
import argparse
import sys
import time
from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import paper_trading.bollinger_strategy as bollinger_strategy  # noqa: E402
from paper_trading.engine import get_settings  # noqa: E402
from paper_trading.option_selection import _nearest_business_day_expiration  # noqa: E402
from multi_user_entry import db_path_for  # noqa: E402
from webapp.brokers import orders as real_orders  # noqa: E402
from webapp.brokers import store  # noqa: E402
from webapp.brokers import tradier_feed  # noqa: E402
from webapp.brokers.pricing import (  # noqa: E402
    build_exit_oco, entry_limit, normalize_chain, normalize_positions,
    order_needs_attention, select_contract,
)
from webapp.brokers.tradier import TradierClient, TradierError  # noqa: E402
from paper_trading.singleton_lock import acquire_single_instance_lock  # noqa: E402

LOG_PATH = ROOT / "data" / "real_trading_engine.log"

# Limites de seguridad -- mismo espiritu que el piloto real anterior
# (deprecado, Robinhood), ahora generalizado a cualquier cuenta de
# Tradier conectada. Conservador a proposito; ajustar con mas datos en
# vivo, nunca a ciegas.
MAX_PREMIUM_PER_CONTRACT = 50.0  # USD, prima x 100 (costo real del contrato)
MIN_DAYS_TO_EXPIRY = 7
MAX_BUYS_PER_DAY = 1
POST_ACTION_LOCK_SECONDS = 300
MIN_CONFIDENCE = ("media", "alta")  # nunca entra en una señal de confianza "baja"
DEFAULT_PROFIT_TARGET_PCT = 0.15
DEFAULT_STOP_LOSS_PCT = 0.20


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _watchlist_for(username: str) -> list[str]:
    settings = get_settings(db_path=db_path_for(username))
    watchlist: set[str] = set()
    if settings.get("default_capital_list_enabled", False):
        from paper_trading.engine import DEFAULT_FAST_WATCHLIST, DEFAULT_WATCHLIST
        watchlist |= set(DEFAULT_WATCHLIST.split(",")) | set(DEFAULT_FAST_WATCHLIST.split(","))
    else:
        if settings.get("watchlist_enabled", True):
            watchlist |= set(settings.get("watchlist") or [])
        if settings.get("fast_watchlist_enabled", True):
            watchlist |= set(settings.get("fast_watchlist") or [])
    return sorted(watchlist)


def _review_pending_entries(client: TradierClient, account_id: str, connection_id: int, username: str) -> None:
    for o in real_orders.pending_entries(connection_id):
        try:
            resp = client.order_status(account_id, int(o["tradier_order_id"]))
        except TradierError as e:
            _log(f"[{username}] ERROR consultando orden {o['tradier_order_id']}: {e}")
            continue
        info = resp.get("order") or {}
        st = info.get("status")
        if st == "filled":
            fill_price = float(info.get("avg_fill_price") or 0)
            real_orders.update_order(o["id"], status="filled", fill_price=fill_price)
            _log(f"[{username}] entrada LLENADA {o['ticker']} {o['option_symbol']} @ {fill_price}")
        elif st in ("rejected", "canceled", "expired"):
            real_orders.update_order(o["id"], status=st)
            _log(f"[{username}] entrada {st} -- {o['ticker']} ({info.get('reason_description', '')})")
        elif st == "pending":
            created = datetime.fromisoformat(o["created_at"])
            minutes = (datetime.now(timezone.utc) - created).total_seconds() / 60
            if order_needs_attention(st, minutes, client.data_delay_minutes):
                _log(f"[{username}] AVISO: orden {o['ticker']} sigue pendiente hace {minutes:.0f} min "
                     f"(retraso de cuenta: {client.data_delay_minutes} min)")


def _place_exit_for_filled_entries(client: TradierClient, account_id: str, connection_id: int, user_id: int, username: str) -> None:
    for o in real_orders.entries_awaiting_exit(connection_id):
        try:
            quote = client.option_quotes([o["option_symbol"]])
        except TradierError as e:
            _log(f"[{username}] ERROR cotizando {o['option_symbol']} para salida: {e}")
            continue
        rows = ((quote.get("quotes") or {}).get("quote"))
        row = rows[0] if isinstance(rows, list) else rows
        if not row:
            continue
        fill_price = float(o["fill_price"] or 0)
        if fill_price <= 0:
            continue
        target_pct = o["profit_target_pct"] or DEFAULT_PROFIT_TARGET_PCT
        stop_pct = o["stop_loss_pct"] or DEFAULT_STOP_LOSS_PCT
        target_price = fill_price * (1 + target_pct)
        stop_price = fill_price * (1 - stop_pct)
        try:
            legs = build_exit_oco(o["option_symbol"], int(o["quantity"]), target_price, stop_price)
        except ValueError as e:
            _log(f"[{username}] ERROR armando OCO de salida {o['ticker']}: {e}")
            continue
        try:
            result = client.place_oco(account_id, legs)
        except TradierError as e:
            _log(f"[{username}] ERROR colocando OCO de salida {o['ticker']}: {e}")
            continue
        order_info = result.get("order") or {}
        tradier_order_id = str(order_info.get("id") or "")
        exit_id = real_orders.create_order(
            connection_id, user_id, username, o["ticker"], o["option_type"], o["option_symbol"],
            "sell_to_close", int(o["quantity"]), status="pending", parent_order_id=o["id"],
            reason=f"OCO objetivo {target_pct*100:.0f}% / stop {stop_pct*100:.0f}%",
        )
        real_orders.update_order(exit_id, tradier_order_id=tradier_order_id)
        _log(f"[{username}] salida OCO colocada {o['ticker']} -- objetivo ${target_price:.2f} / stop ${stop_price:.2f}")


def _try_new_entry(client: TradierClient, account_id: str, connection_id: int, user_id: int, username: str) -> None:
    last = real_orders.last_action_at(connection_id)
    if last is not None:
        last_aware = last if last.tzinfo is not None else last.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - last_aware).total_seconds()
        if elapsed < POST_ACTION_LOCK_SECONDS:
            return
    todays_buys = real_orders.buys_today(connection_id)
    if len(todays_buys) >= MAX_BUYS_PER_DAY:
        return
    symbols_today = {o["ticker"] for o in todays_buys}

    bollinger_strategy.set_active_fetcher(partial(tradier_feed.fetch_history, client))
    try:
        watchlist = _watchlist_for(username)
        signals = []
        for sym in watchlist:
            if sym in symbols_today:
                continue
            try:
                signals.extend(bollinger_strategy.scan_all_signals(sym))
            except Exception as e:
                _log(f"[{username}] escaneo {sym}: {e}")
        candidates = [
            s for s in signals
            if s.get("signal") in ("buy_call", "buy_put") and s.get("confidence") in MIN_CONFIDENCE
        ]
        if not candidates:
            return
        candidates.sort(key=lambda r: (r["confidence"] != "alta", -r.get("volume_ratio", 0)))
        best = candidates[0]
        ticker = best["symbol"]
        option_type = "call" if best["signal"] == "buy_call" else "put"

        quote_resp = client.option_quotes([ticker])
        rows = ((quote_resp.get("quotes") or {}).get("quote"))
        row = rows[0] if isinstance(rows, list) else rows
        spot = float((row or {}).get("last") or 0)
        if spot <= 0:
            _log(f"[{username}] sin precio spot real de {ticker}, se salta este ciclo")
            return

        exp_resp = client.expirations(ticker)
        dates = ((exp_resp.get("expirations") or {}).get("date"))
        if isinstance(dates, str):
            dates = [dates]
        if not dates:
            return
        expiration = _nearest_business_day_expiration(list(dates), date.today())
        if expiration is None:
            return
        chain_resp = client.option_chain(ticker, expiration, greeks=True)
        raw_options = ((chain_resp.get("options") or {}).get("option")) or []
        if isinstance(raw_options, dict):
            raw_options = [raw_options]
        chain = normalize_chain(raw_options)

        contract = select_contract(
            chain, spot, option_type, MAX_PREMIUM_PER_CONTRACT, date.today(), min_days=MIN_DAYS_TO_EXPIRY,
        )
        if contract is None:
            _log(f"[{username}] señal {ticker} {option_type} pero ningun contrato cumple los filtros reales")
            return

        order_id = real_orders.create_order(
            connection_id, user_id, username, ticker, option_type, contract["symbol"], "buy_to_open",
            1, limit_price=contract["limit_price"], status="attempt",
            profit_target_pct=best.get("profit_target_pct"), stop_loss_pct=best.get("stop_loss_pct"),
            reason=best.get("reason", "")[:500],
        )
        try:
            result = client.place_option_order(
                account_id, contract["symbol"], ticker, "buy_to_open", 1, contract["limit_price"], duration="day",
            )
        except TradierError as e:
            real_orders.update_order(order_id, status="rejected")
            _log(f"[{username}] ERROR colocando entrada real {ticker}: {e}")
            return
        order_info = result.get("order") or {}
        tradier_order_id = str(order_info.get("id") or "")
        real_orders.update_order(order_id, status="pending", tradier_order_id=tradier_order_id)
        _log(f"[{username}] ENTRADA REAL colocada: {ticker} {option_type} {contract['symbol']} "
             f"@ ${contract['limit_price']:.2f} (orden Tradier {tradier_order_id})")
    finally:
        bollinger_strategy.set_active_fetcher(None)


def process_connection(conn_info: dict) -> None:
    connection_id = conn_info["id"]
    user_id = conn_info["user_id"]
    username = conn_info["username"]
    account_id = conn_info["account_id"]
    try:
        tokens = store.get_tokens(user_id, connection_id)
    except PermissionError:
        _log(f"[{username}] conexion {connection_id} no disponible (revocada?) -- saltando")
        return
    client = TradierClient(token=tokens["token"], env=tokens["env"])

    _review_pending_entries(client, account_id, connection_id, username)
    _place_exit_for_filled_entries(client, account_id, connection_id, user_id, username)
    _try_new_entry(client, account_id, connection_id, user_id, username)


def run_cycle() -> None:
    connections = store.list_live_trading_connections()
    if not connections:
        return
    for conn_info in connections:
        try:
            process_connection(conn_info)
        except Exception as e:
            _log(f"[{conn_info.get('username')}] ERROR inesperado en el ciclo: {e}")


def main():
    parser = argparse.ArgumentParser(description="Motor de ordenes reales en Tradier (cuentas con dinero real activado)")
    parser.add_argument("--interval", type=int, default=120)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        run_cycle()
        return

    acquire_single_instance_lock("real_trading_engine")
    _log(f"real_trading_engine.py iniciado -- ciclo cada {args.interval}s.")
    while True:
        try:
            run_cycle()
        except Exception as e:
            _log(f"ERROR en el ciclo: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
