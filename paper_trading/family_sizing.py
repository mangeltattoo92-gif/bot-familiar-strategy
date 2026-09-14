"""
Seleccion de contrato Y CANTIDAD ajustadas al capital/riesgo de una
cuenta -- capa que se monta SOBRE select_contract() (que sigue siendo
la fuente de verdad de CUAL es el mejor contrato por delta/volumen:
mayor volumen dentro del rango de delta 0.40-0.60). No reimplementa esa
logica, la reusa tal cual y solo agrega el dimensionamiento por riesgo.

Creado 2026-09-10 para la prueba de 10 cuentas simuladas de bot-familiar
(paso previo a construir el backend multi-usuario completo -- validar el
dimensionamiento por capital reusando el motor YA probado de trading-bot
en vez de escribir un motor nuevo sin probar).

Regla de negocio confirmada por el usuario: la MISMA señal puede
terminar en un contrato, cantidad o incluso ticker distinto por cuenta,
segun lo que cada una puede pagar/arriesgar -- es el comportamiento
esperado, no un bug.

2026-09-13, a pedido explicito del usuario ("el slider de riesgo"):
reemplazado el `contracts_per_trade` fijo (misma cantidad para todos
sin importar el capital) por `risk_pct_per_trade` -- cada cuenta arriesga
un % configurable (0.5%-10%, default 2%) de SU valor total de cuenta por
operacion, y la cantidad de contratos se deriva de eso, no al reves.
"""
from datetime import date, datetime

import yfinance as yf

from paper_trading.option_selection import (
    FALLBACK_DELTA_RANGE,
    MAX_SPREAD_PCT,
    MIN_IMPLIED_VOL,
    MIN_OPEN_INTEREST,
    MIN_PREMIUM,
    TARGET_DELTA,
    _bs_delta,
    _nearest_business_day_expiration,
    select_contract,
)
from webapp.market_data import _option_row_price, _with_timeout


def compute_quantity_from_risk(
    account_value: float, risk_pct: float, contract_price: float, available_cash: float,
    multiplier: float = 100.0,
) -> int:
    """Cuantos contratos comprar para que el costo total no supere NI el
    riesgo objetivo (risk_pct% del valor total de la cuenta) NI el cash
    disponible (limite duro -- nunca se puede gastar mas de lo que hay,
    incluso si el riesgo objetivo en teoria lo permitiria).

    Si ni 1 solo contrato entra dentro del riesgo objetivo, devuelve 0
    -- NO fuerza una operacion que de entrada ya excede el riesgo que
    el usuario configuro (mentalidad de trader real: mejor no operar
    que romper el limite de riesgo propio)."""
    if contract_price <= 0 or multiplier <= 0:
        return 0
    cost_per_contract = contract_price * multiplier
    risk_amount = account_value * (risk_pct / 100.0)
    by_risk = int(risk_amount // cost_per_contract)
    if by_risk < 1:
        return 0
    by_cash = int(available_cash // cost_per_contract)
    return min(by_risk, by_cash)


def select_affordable_contract(
    symbol: str, option_type: str, spot_price: float, available_cash: float,
    account_value: float, risk_pct: float, multiplier: float = 100.0,
) -> tuple[dict | None, str | None, int]:
    """Devuelve (contrato, motivo, cantidad) o (None, None, 0) si nada
    entra en el riesgo/presupuesto de esta cuenta.

    1. Primero intenta el contrato PRIMARIO de select_contract() (mayor
       volumen dentro de delta 0.40-0.60, o el mas cercano a 0.50 si
       ninguno cae en rango) -- igual que el sistema principal. La
       cantidad se deriva de `risk_pct` sobre `account_value` para ESE
       contrato especifico.
    2. Si esa cuenta no puede comprar ni 1 contrato del primario dentro
       de su riesgo/cash, cae a buscar entre los candidatos LIQUIDOS
       (mismo filtro MIN_OPEN_INTEREST) alguno que SI entre (aunque sea
       1 contrato) dentro del riesgo/presupuesto, priorizando entre esos
       el de mayor volumen. Esto es lo que hace que 2 cuentas con la
       MISMA señal terminen en contrato, cantidad o hasta ticker
       distinto segun su capital -- a proposito.

    `risk_pct`: settings['risk_pct_per_trade'], % del valor TOTAL de la
    cuenta (cash + posiciones) que esta cuenta esta dispuesta a arriesgar
    en esta operacion especifica."""
    primary = select_contract(symbol, option_type, spot_price)
    if primary:
        # Se compra al ASK real, no al punto medio -- lo que de verdad
        # costaria llenar la orden. Si no hay ask en vivo (raro, ya que
        # select_contract exige bid/ask>0 para considerar el candidato),
        # cae al punto medio como ultimo recurso.
        primary_price = primary.get("ask") or primary["price"]
        qty = compute_quantity_from_risk(account_value, risk_pct, primary_price, available_cash, multiplier)
        if qty >= 1:
            return primary, "primario (mayor volumen en rango de delta 0.40-0.60)", qty

    ticker = yf.Ticker(symbol)
    expirations = _with_timeout(lambda: ticker.options)
    if not expirations:
        return None, None, 0
    today = date.today()
    expiration = _nearest_business_day_expiration(list(expirations), today)
    if expiration is None:
        return None, None, 0
    chain = _with_timeout(lambda: ticker.option_chain(expiration))
    df = chain.calls if option_type == "call" else chain.puts
    if df is None or df.empty:
        return None, None, 0
    exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
    years = max((exp_date - today).days, 1) / 365.0

    affordable = []
    for _, row in df.iterrows():
        bid = float(row.get("bid") or 0)
        ask = float(row.get("ask") or 0)
        if bid <= 0 or ask <= 0:
            continue
        if ask < MIN_PREMIUM:
            continue  # prima demasiado chica -- un solo tick ya mueve el % mas que el subyacente
        if (ask - bid) / ask > MAX_SPREAD_PCT:
            continue  # spread demasiado ancho -- comprar y vender al instante ya perderia mas de esto
        open_interest = float(row.get("openInterest") or 0)
        if open_interest < MIN_OPEN_INTEREST:
            continue
        price = _option_row_price(row)
        if price is None:
            continue
        qty = compute_quantity_from_risk(account_value, risk_pct, ask, available_cash, multiplier)
        if qty < 1:
            continue  # ni 1 solo contrato entra en el riesgo/cash de esta cuenta
        iv = float(row.get("impliedVolatility") or 0)
        if iv < MIN_IMPLIED_VOL:
            continue  # IV placeholder/invalida -- no confiable para calcular delta
        strike = float(row["strike"])
        delta = _bs_delta(spot_price, strike, years, iv, option_type)
        affordable.append((row, delta, price, qty))

    if not affordable:
        return None, None, 0

    # Mismo tope que select_contract(): un delta demasiado lejos del
    # objetivo (ej. 0.81 en un contrato de centavos) no vale la pena ni
    # como "el mas barato disponible" -- 2026-09-11, mismo caso real NIO.
    affordable = [c for c in affordable if c[1] is not None and FALLBACK_DELTA_RANGE[0] <= abs(c[1]) <= FALLBACK_DELTA_RANGE[1]]
    if not affordable:
        return None, None, 0

    # A pedido explicito del usuario (2026-09-11): "siempre comprar los
    # mejores contratos at the money con el mayor volumen" -- mismo
    # criterio que select_contract(), tambien aca: entre los que ya
    # entran en riesgo/presupuesto y en el rango de delta aceptable, el
    # de MAYOR VOLUMEN, no el de delta mas cercano.
    best_row, best_delta, best_price, best_qty = max(
        affordable, key=lambda cand: float(cand[0].get("volume") or 0)
    )
    contract = {
        "strike": float(best_row["strike"]),
        "expiration": expiration,
        "price": best_price,
        "bid": float(best_row.get("bid") or 0) or None,
        "ask": float(best_row.get("ask") or 0) or None,
        "delta": round(best_delta, 4) if best_delta is not None else None,
        "option_type": option_type,
        "open_interest": int(best_row.get("openInterest") or 0),
        "volume": int(best_row.get("volume") or 0),
    }
    return contract, "fallback por presupuesto (el mas barato que entra en el riesgo/cash disponible, no el de mayor volumen)", best_qty
