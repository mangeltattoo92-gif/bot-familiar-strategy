#!/usr/bin/env python3
"""
Rastreador SOMBRA de niveles de stop-loss mas ajustados -- 2026-09-15,
a pedido del usuario despues de una sesion con varias perdidas
(-20/-22/-25%). NUNCA cierra ni modifica ninguna posicion real -- solo
observa y registra.

Por que existe en vez de un backtest: se intento simular "que hubiera
pasado con un stop de -12%/-15%" aproximando la prima con
delta * movimiento del subyacente, y esa aproximacion dio FALSOS
positivos sobre las propias ganadoras de hoy (decia que QCOM/META/PLTR
habrian tocado stop, cuando en la realidad nunca lo tocaron -- la
aproximacion lineal de delta no captura bien la dinamica real de una
opcion de muy corto plazo). En vez de inventar numeros, este script usa
el MISMO precio real (_current_exit_price -- bid real con proteccion de
spread) que ya usa multi_user_entry.py para decidir salidas de verdad
-- datos genuinos, recolectados en vivo de aca en adelante.

Para cada posicion ABIERTA: calcula el P&L real actual y anota la
PRIMERA vez que cruza cada nivel candidato (-10/-12/-15/-18%, el actual
-20% ya se sabe -- es el que ya usa el sistema). Cuando la posicion se
CIERRA de verdad, completa el registro con el resultado real final, asi
se puede comparar por nivel: "se hubiera disparado antes, en que P&L" vs
"que paso de verdad despues" (¿la posicion se recupero, o la perdida
real hubiera sido peor?).

Estado persistente en data/shadow_stop_watch.json.

Uso:
  python shadow_stop_watch.py --once            # un chequeo (pruebas)
  python shadow_stop_watch.py --report          # imprime el resumen actual
  python shadow_stop_watch.py --interval 30     # loop continuo -- ver
    shadow_stop_watch_loop.py para el wrapper de produccion.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from multi_user_entry import _current_exit_price, active_accounts  # noqa: E402
from paper_trading.engine import get_history, get_status  # noqa: E402

CANDIDATE_LEVELS = [-10.0, -12.0, -15.0, -18.0]
STATE_PATH = ROOT / "data" / "shadow_stop_watch.json"
LOG_PATH = ROOT / "data" / "shadow_stop_watch.log"


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_id(username: str, p: dict) -> str:
    return f"{username}::{p['key']}::{p['opened_at']}"


def _pnl_pct(entry_price: float, current_price: float) -> float:
    return (current_price / entry_price - 1.0) * 100.0


def _find_actual_sell(username: str, ticker: str, entry_ts: str) -> dict | None:
    """Busca en el historial la venta real mas cercana en el tiempo, DESPUES
    de la entrada, para este ticker -- misma logica de emparejamiento que se
    uso en el analisis manual de hoy (por ticker+tiempo, no hay un id de
    posicion estable entre compra y venta en el historial)."""
    db_path = Path(f"data/users/{username}/paper_trading.db")
    if not db_path.exists():
        return None
    for t in get_history(limit=200, db_path=db_path):
        if t["side"] == "sell" and t["ticker"] == ticker and t["timestamp"] > entry_ts:
            return t
    return None


def run_once() -> dict:
    state = _load_state()
    accounts = active_accounts()
    open_ids = set()

    for username, db_path in accounts:
        status = get_status(db_path=db_path)
        for p in status["positions"]:
            if p["asset_type"] != "option":
                continue
            rec_id = _record_id(username, p)
            open_ids.add(rec_id)
            current = _current_exit_price(p)
            if current is None:
                continue
            pnl_pct = _pnl_pct(p["avg_cost"], current)

            if rec_id not in state:
                state[rec_id] = {
                    "username": username, "ticker": p["ticker"], "opened_at": p["opened_at"],
                    "entry_price": p["avg_cost"], "levels_triggered": {}, "closed": False,
                    "min_pnl_pct_seen": pnl_pct, "max_pnl_pct_seen": pnl_pct,
                }
            rec = state[rec_id]
            rec["min_pnl_pct_seen"] = min(rec["min_pnl_pct_seen"], pnl_pct)
            rec["max_pnl_pct_seen"] = max(rec["max_pnl_pct_seen"], pnl_pct)
            for level in CANDIDATE_LEVELS:
                key = str(level)
                if key not in rec["levels_triggered"] and pnl_pct <= level:
                    rec["levels_triggered"][key] = {
                        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "pnl_pct": round(pnl_pct, 2),
                    }
                    _log(f"[{username}] {p['ticker']}: nivel sombra {level}% cruzado (P&L real {pnl_pct:+.2f}%)")

    # Reconciliar posiciones que ya no estan abiertas -- cerradas de verdad.
    newly_closed = 0
    for rec_id, rec in state.items():
        if rec["closed"] or rec_id in open_ids:
            continue
        sell = _find_actual_sell(rec["username"], rec["ticker"], rec["opened_at"])
        if sell is None:
            continue  # todavia no aparece en el historial -- se reintenta el proximo ciclo
        rec["closed"] = True
        rec["actual_exit_ts"] = sell["timestamp"]
        rec["actual_result_pct"] = _pnl_pct(rec["entry_price"], sell["price"])
        rec["actual_reason"] = (sell.get("reason") or "")[:200]
        newly_closed += 1

    _save_state(state)
    if newly_closed:
        _log(f"{newly_closed} posicion(es) sombra reconciliada(s) con su cierre real.")
    return state


def print_report(state: dict) -> None:
    closed = [r for r in state.values() if r["closed"]]
    if not closed:
        print("Todavia no hay posiciones cerradas para comparar.")
        return
    print(f"{'ticker':6s} {'actual%':>8s}  " + "  ".join(f"{l:>6.0f}%" for l in CANDIDATE_LEVELS))
    for r in closed:
        line = f"{r['ticker']:6s} {r['actual_result_pct']:>7.1f}%  "
        for level in CANDIDATE_LEVELS:
            trig = r["levels_triggered"].get(str(level))
            line += f"  {'SI@' + str(trig['pnl_pct']) + '%' if trig else '  no  ':>7s}"
        print(line)
    print()
    for level in CANDIDATE_LEVELS:
        triggered = [r for r in closed if str(level) in r["levels_triggered"]]
        if not triggered:
            print(f"{level}%: nunca se cruzo en {len(closed)} posiciones cerradas.")
            continue
        # Cuantas de las que cruzaron este nivel terminaron GANANDO en la realidad
        # (es decir, un stop ahi las habria cortado innecesariamente)
        would_have_cut_a_winner = len([r for r in triggered if r["actual_result_pct"] > 0])
        avoided_loss = len([r for r in triggered if r["actual_result_pct"] <= level])
        print(f"{level}%: se cruzo en {len(triggered)}/{len(closed)} -- "
              f"{would_have_cut_a_winner} hubieran sido ganadoras cortadas, "
              f"{avoided_loss} hubieran evitado una perdida mayor.")


def main():
    parser = argparse.ArgumentParser(description="Rastreador sombra de stop-loss mas ajustados (no ejecuta nada)")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()

    if args.report:
        print_report(_load_state())
        return

    if args.once:
        run_once()
        return

    _log(f"shadow_stop_watch.py iniciado -- chequeo cada {args.interval}s.")
    while True:
        try:
            run_once()
        except Exception as e:
            _log(f"[shadow_stop_watch] ERROR: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
