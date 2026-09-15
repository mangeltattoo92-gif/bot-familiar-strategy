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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from multi_user_entry import run_exits_for_account
from paper_trading.bollinger_strategy import scan_all_signals, too_close_to_open_new_position
from paper_trading.engine import (
    DEFAULT_DB_PATH, get_settings, get_status, get_trades_count_today, get_unsettled_cash_today,
)
from paper_trading.family_sizing import select_affordable_contract
from paper_trading.singleton_lock import exits_critical_section
from webapp import market_data

CONFIDENCE_ORDER = {"alta": 0, "media": 1, "baja": 2}


def main():
    result = {"closed_positions_count": 0, "new_signal": None, "circuit_breaker_active": False, "notes": []}

    # Pasos 1+2: cerrar posiciones que correspondan y chequear el
    # circuit breaker -- funcion REAL del proyecto, no reimplementada.
    # Lock compartido con exit_watch.py (2026-09-15, motor liviano de
    # solo-salidas cada 30s) para que nunca revisen/cierren la misma
    # posicion al mismo tiempo.
    with exits_critical_section():
        result["closed_positions_count"] = run_exits_for_account("piloto", DEFAULT_DB_PATH)

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
    if trades_today >= settings["max_trades_per_day"]:
        result["notes"].append(f"Limite diario de operaciones alcanzado ({trades_today}/{settings['max_trades_per_day']}).")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    all_symbols = list(dict.fromkeys(settings["watchlist"] + settings["fast_watchlist"]))
    open_tickers = {p["ticker"] for p in status["positions"]}
    signals = []
    for symbol in all_symbols:
        try:
            for r in scan_all_signals(symbol):
                if r["signal"] == "none" or r["confidence"] == "baja" or symbol in open_tickers:
                    continue
                # 2026-09-15, mismo filtro que multi_user_entry.py -- ver la
                # nota larga ahi: giro_sma20 salio 0/2 hoy en confianza media,
                # sin el respaldo de volumen/compresion que si exige
                # squeeze_breakout por diseño.
                if r["strategy"] == "giro_sma20" and r["confidence"] != "alta":
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
