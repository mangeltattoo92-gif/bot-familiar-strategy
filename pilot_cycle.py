#!/usr/bin/env python3
"""
Ciclo determinístico del piloto -- hace TODO el trabajo que no necesita
un agente de IA de una sola pasada en Python plano, reusando las MISMAS
funciones ya probadas de multi_user_entry.py (run_exits_for_account,
que incluye el circuit breaker) en vez de reimplementar esa lógica de
nuevo -- evita bugs de reescritura y garantiza que el piloto se comporta
exactamente igual que el sistema simulado real.

Lo UNICO que este script NO hace es verificar el precio final con
Robinhood real y registrar la ENTRADA -- eso lo hace el agente
despues, leyendo la salida JSON de este script, porque necesita las
herramientas MCP de Robinhood (que solo el agente tiene). Las SALIDAS
si se ejecutan directo aca (mismo criterio que el sistema real: precio
real de yfinance, sin necesidad de verificacion extra).

Uso: ./venv/bin/python3 pilot_cycle.py
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from daily_market_bias import load_bias as load_daily_market_bias
from multi_user_entry import run_exits_for_account
from news_watch import has_recent_news
from paper_trading.bollinger_strategy import scan_all_signals, too_close_to_open_new_position
from paper_trading.engine import (
    DEFAULT_DB_PATH, get_settings, get_status, get_trades_count_today, get_unsettled_cash_today,
)
from paper_trading.family_sizing import select_affordable_contract
from paper_trading.singleton_lock import exits_critical_section
from webapp import market_data

CONFIDENCE_ORDER = {"alta": 0, "media": 1, "baja": 2}

# 2026-10-06, a pedido explicito del usuario para la cuenta real de $33:
# limites duros sobre cada compra real -- prima maxima por contrato, dias
# minimos hasta vencimiento y una sola orden por dia.
REAL_MAX_PREMIUM_PER_CONTRACT = 30.0
REAL_MIN_DAYS_TO_EXPIRY = 7
REAL_MAX_BUYS_PER_DAY = 1
ROOT = Path(__file__).resolve().parent
REAL_ORDERS_FILE = ROOT / "real_orders.json"
REAL_ORDERS_SWITCH = ROOT / "REAL_ORDERS_ENABLED"


def _real_buys_today() -> int:
    if not REAL_ORDERS_FILE.exists():
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    orders = json.loads(REAL_ORDERS_FILE.read_text(encoding="utf-8") or "[]")
    return sum(
        1 for o in orders
        if o.get("side") == "buy" and str(o.get("ts", "")).startswith(today)
        and o.get("status") not in ("rejected", "cancelled", "failed")
    )


def _real_symbols_bought_today() -> set[str]:
    """Tickers con una compra real de hoy que no fue rechazada: no se vuelve a comprar el mismo ticker."""
    if not REAL_ORDERS_FILE.exists():
        return set()
    today = datetime.now(timezone.utc).date().isoformat()
    orders = json.loads(REAL_ORDERS_FILE.read_text(encoding="utf-8") or "[]")
    return {
        o["symbol"] for o in orders
        if o.get("side") == "buy" and str(o.get("ts", "")).startswith(today)
        and o.get("status") not in ("rejected", "cancelled", "failed")
    }


def _days_to_expiry(expiration: str) -> int:
    return (datetime.fromisoformat(expiration).date() - datetime.now(timezone.utc).date()).days


def main():
    result = {
        "closed_positions": [], "closed_positions_count": 0, "new_signal": None,
        "circuit_breaker_active": False, "real_orders_enabled": REAL_ORDERS_SWITCH.exists(), "notes": [],
    }

    # Pasos 1+2: cerrar posiciones que correspondan y chequear el
    # circuit breaker -- funcion REAL del proyecto, no reimplementada.
    # Lock compartido con exit_watch.py (2026-09-15, motor liviano de
    # solo-salidas cada 30s) para que nunca revisen/cierren la misma
    # posicion al mismo tiempo.
    before = {p["key"]: p for p in get_status(db_path=DEFAULT_DB_PATH)["positions"]}
    with exits_critical_section():
        result["closed_positions_count"] = run_exits_for_account("piloto", DEFAULT_DB_PATH)
    after_keys = {p["key"] for p in get_status(db_path=DEFAULT_DB_PATH)["positions"]}
    for key, p in before.items():
        if key not in after_keys:
            result["closed_positions"].append({
                "key": key, "ticker": p["ticker"], "asset_type": p["asset_type"],
                "quantity": p["quantity"], "avg_cost": p["avg_cost"],
                "option_details": p["option_details"],
            })

    settings = get_settings()
    status = get_status()
    if not settings["bot_enabled"]:
        result["circuit_breaker_active"] = True
        result["notes"].append("Circuit breaker activo -- no se buscan entradas nuevas este ciclo.")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # Paso 3: escanear señales de entrada.
    if too_close_to_open_new_position():
        result["notes"].append("Cerca del cierre de sesion o mercado cerrado -- no se abren posiciones nuevas.")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    trades_today = get_trades_count_today()
    if not REAL_ORDERS_SWITCH.exists() and trades_today >= settings["max_trades_per_day"]:
        result["notes"].append(f"Limite diario de operaciones alcanzado ({trades_today}/{settings['max_trades_per_day']}).")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    all_symbols = list(dict.fromkeys(settings["watchlist"] + settings["fast_watchlist"]))
    open_tickers = {p["ticker"] for p in status["positions"]}
    real_symbols_today = _real_symbols_bought_today()
    daily_bias = load_daily_market_bias()
    signals = []
    for symbol in all_symbols:
        try:
            for r in scan_all_signals(symbol):
                if r["signal"] == "none" or r["confidence"] == "baja" or symbol in open_tickers or symbol in real_symbols_today:
                    continue
                # 2026-09-15, mismos filtros que multi_user_entry.py -- ver
                # las notas largas ahi para cada uno.
                if r["strategy"] == "giro_sma20" and r["confidence"] != "alta":
                    continue
                if r["strategy"] == "squeeze_breakout_temprano" and r["confidence"] != "alta":
                    continue
                # 2026-10-02: auditoria real de operaciones ya ejecutadas
                # (6 cuentas) encontro que confianza "media" perdio
                # -$501.75 neto en agregado (n=47, 51% acierto) contra
                # +$819.25 de "alta" (n=52, 67%) -- este script tenia su
                # propio filtro de "volatilidad extrema" para
                # squeeze_breakout, separado del requisito de confianza
                # alta que ya se agrego en multi_user_entry.py. Se exigen
                # AMBOS aca (el piloto es dinero real, criterio mas
                # estricto que el sistema simulado).
                if (r["strategy"] in ("squeeze_breakout", "squeeze_breakout_temprano")
                        and r.get("volatility_strength") != "extrema"):
                    continue
                if r["strategy"] == "squeeze_breakout" and r["confidence"] != "alta":
                    continue
                ticker_bias = daily_bias.get(symbol)
                wanted_bias = "alcista" if r["signal"] == "buy_call" else "bajista"
                if ticker_bias not in (None, "lateral", wanted_bias) and r["confidence"] != "alta":
                    continue
                if r["confidence"] != "alta":
                    has_news, _headline = has_recent_news(symbol)
                    if has_news:
                        continue
                signals.append(r)
        except Exception as e:
            result["notes"].append(f"Error escaneando {symbol}: {e}")

    if not signals:
        result["notes"].append("Sin señales de entrada de confianza alta/media este ciclo.")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    signals.sort(key=lambda r: (CONFIDENCE_ORDER[r["confidence"]], -r.get("volume_ratio", 0)))
    top = signals[0]

    # Paso 4: tamaño por riesgo, basado en la cuenta de PAPEL (no Robinhood real).
    option_type = "call" if top["signal"] == "buy_call" else "put"
    spot = market_data.get_quote(top["symbol"])
    if spot is None:
        result["notes"].append(f"No se pudo obtener spot price de {top['symbol']} -- se omite esta señal.")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    status = get_status()  # refrescar por si hubo cierres arriba
    risk_pct = float(settings["risk_pct_per_trade"])

    # Buying power real de Robinhood: la plata de una venta cerrada HOY no
    # esta liquidada hasta la proxima sesion de bolsa (T+1) -- no se puede
    # usar para abrir una posicion nueva el mismo dia, aunque el
    # cash_balance ya la sume. Se resta solo del cash disponible para
    # dimensionar (nunca de total_account_value, que es para medir el
    # riesgo en % de la cuenta, no cash disponible para gastar).
    unsettled_today = get_unsettled_cash_today()
    available_cash = max(0.0, status["cash_balance"] - unsettled_today)
    if unsettled_today > 0:
        result["notes"].append(
            f"${unsettled_today:.2f} de ventas cerradas hoy todavia no estan liquidadas (T+1) -- "
            f"no cuentan como buying power disponible para una entrada nueva este ciclo."
        )

    contract, method, qty = select_affordable_contract(
        top["symbol"], option_type, spot, available_cash, status["total_account_value"], risk_pct,
    )
    if contract is None or qty < 1:
        result["notes"].append(f"Señal en {top['symbol']} pero ningun contrato entra en el riesgo/cash disponible (liquidado) de la cuenta de papel.")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # Limites de la cuenta real: prima por contrato, dias hasta vencimiento
    # y una sola compra por dia. Si no se cumplen, no se propone la senal.
    real_limit_reason = None
    if contract["ask"] * 100 > REAL_MAX_PREMIUM_PER_CONTRACT:
        real_limit_reason = f"prima ${contract['ask'] * 100:.2f} supera el maximo de ${REAL_MAX_PREMIUM_PER_CONTRACT:.2f} por contrato"
    elif _days_to_expiry(contract["expiration"]) < REAL_MIN_DAYS_TO_EXPIRY:
        real_limit_reason = f"vence en menos de {REAL_MIN_DAYS_TO_EXPIRY} dias"
    elif _real_buys_today() >= REAL_MAX_BUYS_PER_DAY:
        real_limit_reason = f"ya hubo {REAL_MAX_BUYS_PER_DAY} compra(s) real(es) hoy"
    if real_limit_reason:
        result["notes"].append(f"Senal en {top['symbol']} descartada por limite de cuenta real: {real_limit_reason}.")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    last_action = ROOT / "last_action.txt"
    if last_action.exists() and datetime.now(timezone.utc).timestamp() - float(last_action.read_text().strip()) < 300:
        result["notes"].append("Hubo una accion del agente hace menos de 5 minutos: no se propone una senal nueva hasta verificar el resultado.")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    result["new_signal"] = {
        "symbol": top["symbol"], "strategy": top["strategy"], "signal": top["signal"],
        "confidence": top["confidence"], "reason": top["reason"],
        "profit_target_pct": top.get("profit_target_pct"), "stop_loss_pct": top.get("stop_loss_pct"),
        "option_type": option_type, "strike": contract["strike"], "expiration": contract["expiration"],
        "delta": contract["delta"], "bid": contract["bid"], "ask": contract["ask"],
        "quantity": qty, "sizing_method": method, "risk_pct": risk_pct,
        "account_value": status["total_account_value"], "cash_balance": status["cash_balance"],
        "unsettled_cash_today": unsettled_today, "available_cash": available_cash,
    }
    result["notes"].append(
        "Candidato listo para verificar con Robinhood y registrar en papel -- el agente debe llamar a "
        "record_trade(side='buy', ...) con el precio ASK verificado, no este script."
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
