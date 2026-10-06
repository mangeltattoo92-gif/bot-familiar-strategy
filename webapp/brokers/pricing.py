"""Precios, seleccion de contratos y ordenes de salida para Tradier.

Misma logica que el motor simulado:
  - compra a 65% del camino entre bid y ask (LIMIT_FILL_FRACTION),
  - venta a 35% del camino desde el bid,
  - limites de la cuenta real (prima maxima, dias minimos, una compra por dia).
"""
import math
from datetime import date
from typing import Any

LIMIT_FILL_FRACTION = 0.65
MAX_SPREAD_PCT = 0.15


def tick_size(price: float) -> float:
    """Tick de opciones: 0.01 bajo $3, 0.05 desde $3 (como en Robinhood; confirmar en Tradier)."""
    return 0.01 if price < 3.0 else 0.05


def round_to_tick(price: float, mode: str = "nearest") -> float:
    tick = tick_size(price)
    steps = price / tick
    if mode == "up":
        steps = math.ceil(steps - 1e-9)
    elif mode == "down":
        steps = math.floor(steps + 1e-9)
    else:
        steps = round(steps)
    return round(steps * tick, 2)


def entry_limit(bid: float, ask: float) -> float:
    """Precio limite de compra: 65% del camino desde el bid hacia el ask, sin pasar del ask."""
    if not (ask > bid > 0):
        raise ValueError("se necesita bid > 0 y ask > bid")
    raw = bid + (ask - bid) * LIMIT_FILL_FRACTION
    return min(round_to_tick(raw, "nearest"), ask)


def exit_limit(bid: float, ask: float) -> float:
    """Precio limite de venta: 35% del camino desde el bid hacia el ask, sin bajar del bid."""
    if not (ask > bid > 0):
        raise ValueError("se necesita bid > 0 y ask > bid")
    raw = bid + (ask - bid) * (1 - LIMIT_FILL_FRACTION)
    return max(round_to_tick(raw, "nearest"), bid)


def select_contract(
    chain: list[dict[str, Any]],
    spot: float,
    option_type: str,
    budget: float,
    today: date,
    min_days: int = 7,
    max_distance_pct: float = 10.0,
    min_volume: int = 100,
    min_open_interest: int = 500,
) -> dict[str, Any] | None:
    """Elige el contrato mas cercano al precio que cumple costo, liquidez y spread.

    chain: filas con 'option_type', 'strike', 'bid', 'ask', 'volume', 'open_interest', 'expiration' (YYYY-MM-DD).
    budget: prima maxima por contrato en USD (prima x 100 debe ser <= budget).
    """
    best = None
    for row in chain:
        if row.get("option_type") != option_type:
            continue
        bid = float(row.get("bid") or 0)
        ask = float(row.get("ask") or 0)
        if not (ask > bid > 0):
            continue
        if (ask - bid) / ask > MAX_SPREAD_PCT:
            continue
        if ask * 100 > budget:
            continue
        days = (date.fromisoformat(row["expiration"]) - today).days
        if days < min_days:
            continue
        if int(row.get("volume") or 0) < min_volume or int(row.get("open_interest") or 0) < min_open_interest:
            continue
        distance = abs(float(row["strike"]) - spot) / spot * 100
        if distance > max_distance_pct:
            continue
        if best is None or distance < best["_distance"]:
            best = {**row, "_distance": distance, "_days": days}
    if best is not None:
        best.pop("_distance")
        best["limit_price"] = entry_limit(float(best["bid"]), float(best["ask"]))
    return best


def build_exit_oco(option_symbol: str, quantity: int, target_price: float, stop_price: float,
                   duration: str = "gtc") -> list[dict[str, Any]]:
    """Dos patas de salida para una posicion comprada: meta (limit) y stop. Una cancela a la otra."""
    if quantity < 1:
        raise ValueError("quantity debe ser al menos 1")
    if target_price <= stop_price or stop_price <= 0:
        raise ValueError("la meta debe estar por encima del stop, y el stop debe ser positivo")
    base = {"option_symbol": option_symbol, "side": "sell_to_close", "quantity": str(quantity), "duration": duration}
    return [
        {**base, "type": "limit", "price": f"{round_to_tick(target_price, 'up'):.2f}"},
        {**base, "type": "stop", "stop": f"{round_to_tick(stop_price, 'down'):.2f}"},
    ]
