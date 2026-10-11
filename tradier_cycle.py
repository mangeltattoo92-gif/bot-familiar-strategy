#!/usr/bin/env python3
"""
Ciclo de entradas y salidas para UNA cuenta (por ahora solo el admin,
Miguel) usando datos de Tradier en vez de yfinance -- a pedido explicito
del usuario 2026-10-07 ("mi bot simulado desde ahora va ser solo para
tradier... una vez que se vayan incorporando los diferentes usuarios
ellos van a agregar sus datos api y demas").

Reutiliza EXACTAMENTE la misma logica de multi_user_entry.py
(run_exits_for_account, run_entry_cycle) -- lo unico que cambia es de
donde sale el DataFrame de velas y la cadena de opciones (ver los
overrides en bollinger_strategy.set_active_fetcher() /
option_selection.set_active_chain_fetcher() / market_data.set_active_chain_fetcher(),
y el adaptador en webapp/brokers/tradier_feed.py). Ninguna estrategia
cambia de comportamiento.

La(s) cuenta(s) procesadas aqui DEBEN estar en
data/tradier_only_users.json para que multi_user_entry.py (el motor
compartido, yfinance) las excluya -- si no, se procesarian dos veces.

Uso (produccion, ver tradier_cycle_loop.py para el wrapper con reinicio):
  python tradier_cycle.py --once
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import paper_trading.bollinger_strategy as bollinger_strategy  # noqa: E402
import paper_trading.option_selection as option_selection  # noqa: E402
import webapp.market_data as market_data  # noqa: E402
from webapp.brokers import tradier_feed  # noqa: E402
from webapp.brokers.tradier import TradierClient  # noqa: E402
from multi_user_entry import (  # noqa: E402
    db_path_for, run_entry_cycle, run_exits_for_account, _tradier_only_usernames,
)
from paper_trading.singleton_lock import acquire_single_instance_lock, exits_critical_section  # noqa: E402

LOG_PATH = ROOT / "data" / "tradier_cycle.log"


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _tradier_accounts() -> list[tuple[str, Path]]:
    usernames = _tradier_only_usernames()
    # 2026-10-10, mismo criterio que multi_user_entry.active_accounts():
    # si el interruptor de dinero real esta encendido, la procesa
    # real_trading_engine.py, nunca este ciclo simulado tambien.
    try:
        from webapp.brokers.store import live_trading_usernames
        usernames = usernames - live_trading_usernames()
    except Exception:
        pass
    return [(u, db_path_for(u)) for u in sorted(usernames)]


def run_cycle() -> None:
    accounts = _tradier_accounts()
    if not accounts:
        _log("data/tradier_only_users.json vacio o no existe -- nada que procesar.")
        return

    client = TradierClient()
    bollinger_strategy.set_active_fetcher(partial(tradier_feed.fetch_history, client))
    option_selection.set_active_chain_fetcher(partial(tradier_feed.get_expiration_and_chain, client))
    market_data.set_active_chain_fetcher(partial(tradier_feed.get_chain_for_expiration, client))
    try:
        closed_total = 0
        with exits_critical_section():
            for username, db_path in accounts:
                closed_total += run_exits_for_account(username, db_path)
        run_entry_cycle(accounts)
        if closed_total:
            _log(f"Ciclo Tradier completo -- {closed_total} posicion(es) cerradas.")
    finally:
        # Nunca dejar el override activo si algo mas en este proceso
        # (o un import circular) llega a usar estas funciones sin pasar
        # por aca -- simetria con el estado por defecto (yfinance).
        bollinger_strategy.set_active_fetcher(None)
        option_selection.set_active_chain_fetcher(None)
        market_data.set_active_chain_fetcher(None)


def main():
    parser = argparse.ArgumentParser(description="Ciclo de entradas/salidas via Tradier para cuentas opt-in")
    parser.add_argument("--interval", type=int, default=120)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        run_cycle()
        return

    acquire_single_instance_lock("tradier_cycle")
    _log(f"tradier_cycle.py iniciado -- ciclo cada {args.interval}s.")
    while True:
        try:
            run_cycle()
        except Exception as e:
            _log(f"ERROR en el ciclo: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
