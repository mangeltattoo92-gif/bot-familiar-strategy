"""
Seleccion de contrato de opciones via yfinance -- SIN el MCP de Robinhood --
para que auto_entry.py pueda escanear y ejecutar entradas sin depender de
una sesion de Claude activa (ver auto_entry.py para el porque).

yfinance no expone delta directamente (ver columnas de .option_chain()):
se calcula via Black-Scholes a partir de su impliedVolatility. Suficiente
para ELEGIR el strike -- no se usa para el precio final, que sigue siendo
el bid/ask real de yfinance (_option_row_price, igual que en market_data.py).

Es MENOS preciso que la seleccion manual via MCP de Robinhood (que trae
delta real del motor de precios de Robinhood, no una aproximacion): esta
funcion prioriza disponibilidad 24/7 sobre precision perfecta -- decision
tomada con el usuario el 2026-09-09 tras perderse una entrada real de SPY
por falta de un escaneo automatico recurrente.
"""

import math
from datetime import date, datetime, timedelta

import yfinance as yf

from paper_trading.nyse_calendar import is_trading_day
from webapp.market_data import _option_row_price, _with_timeout

TARGET_DELTA = 0.50
DELTA_RANGE = (0.40, 0.60)  # mismo rango objetivo que la seleccion manual via MCP de Robinhood

# Cuando ningun strike cae en DELTA_RANGE (strikes muy espaciados, tipico
# de subyacentes baratos/poco liquidos como NIO), el fallback "mas
# cercano a 0.50" no tenia piso -- podia aceptar un delta 0.81 (call casi
# ITM) como si fuera aceptable. Visto en vivo 2026-09-11: NIO entro con
# delta 0.8062 sobre una prima de $0.25 -- el subyacente se quedo
# PRACTICAMENTE PLANO (-0.3%) pero la prima cayo -20% solo por ruido de
# cotizacion de un contrato de centavos, mal seleccionado desde el
# principio. El fallback ahora solo acepta candidatos dentro de este
# rango mas ancho -- fuera de esto, ni siquiera vale la pena el
# "mejor disponible", se prefiere no entrar (a pedido explicito del
# usuario tras revisar el caso).
FALLBACK_DELTA_RANGE = (0.25, 0.75)
TARGET_BUSINESS_DAYS_OUT = 7
RISK_FREE_RATE = 0.045  # aproximacion -- no vale la pena pagar una llamada de red extra por esto

# bid/ask>0 por si solo NO garantiza un contrato liquido -- yfinance puede
# mostrar una cotizacion tecnicamente no-cero en un strike con casi nada
# de interes abierto (quote stale/ancho). MIN_OPEN_INTEREST descarta esos
# antes de elegir por delta (2026-09-09, a pedido del usuario: "no
# comprando por volumen" -- se agrega el filtro que faltaba).
MIN_OPEN_INTEREST = 50

# yfinance a veces devuelve impliedVolatility ~0.00001 como placeholder
# cuando no tiene una IV real calculada para esa fila (visto en vivo con
# AAPL 2026-09-11: dos strikes profundamente ITM con OI real pero IV
# placeholder saturaban el delta Black-Scholes a 1.0, haciendo que el
# fallback "mas cercano a delta 0.50" eligiera un contrato ITM carisimo
# como si fuera ATM). Cualquier IV por debajo de este umbral se trata
# como dato invalido, no como IV real de mercado.
MIN_IMPLIED_VOL = 0.03

# Visto en vivo 2026-09-11: se compraron NIO/COST/CRM/UBER/SNAP y 5 de 7
# fueron cerradas por stop_loss a los 15-100 SEGUNDOS de abrirse, con
# perdidas de -23% a -80%. Causa real: el spread bid/ask de yfinance para
# esos contratos era enorme (ej. NIO ask $1.68 / bid $0.33 = 80% de
# spread) -- comprar al ask y marcar a mercado casi al instante contra el
# bid ya cruza el stop_loss SOLO por el spread, sin que el precio
# subyacente se haya movido. MIN_OPEN_INTEREST no alcanza para filtrar
# esto (esos contratos SI tenian OI>=50). Se descarta cualquier candidato
# cuyo spread relativo al ask supere este umbral -- no es negociable de
# forma realista si comprar y vender al instante ya perderia mas de esto.
MAX_SPREAD_PCT = 0.15

# Visto en vivo 2026-09-11: contrato de AMC comprado a $0.08 de prima
# (strike $2.50 practicamente ATM, spot $2.47) -- paso todos los filtros
# de arriba pero un trader real lo descartaria: con una prima asi de
# chica, UN SOLO tick de $0.01 ya es un movimiento de ~12%, asi que el
# profit_target/stop_loss en % deja de medir el movimiento real del
# subyacente y pasa a medir ruido de cotizacion. Se descarta cualquier
# candidato cuyo ASK (lo que realmente se paga) sea menor a este piso,
# sin importar que tan bueno sea el delta o la liquidez.
MIN_PREMIUM = 0.15


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs_delta(spot: float, strike: float, years: float, iv: float, option_type: str) -> float | None:
    if years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return None
    d1 = (math.log(spot / strike) + (RISK_FREE_RATE + iv ** 2 / 2) * years) / (iv * math.sqrt(years))
    call_delta = _norm_cdf(d1)
    return call_delta if option_type == "call" else call_delta - 1


def _nearest_business_day_expiration(expirations: list[str], today: date) -> str | None:
    best, best_diff = None, None
    for exp_str in expirations:
        exp = datetime.strptime(exp_str, "%Y-%m-%d").date()
        if exp <= today:
            continue
        business_days = sum(
            1 for i in range(1, (exp - today).days + 1) if is_trading_day(today + timedelta(days=i))
        )
        diff = abs(business_days - TARGET_BUSINESS_DAYS_OUT)
        if best_diff is None or diff < best_diff:
            best, best_diff = exp_str, diff
    return best


def select_contract(symbol: str, option_type: str, spot_price: float) -> dict | None:
    """Elige un contrato de `symbol` (call/put), vencimiento mas cercano a
    TARGET_BUSINESS_DAYS_OUT dias habiles. Descarta filas sin bid/ask en
    vivo o con poco open interest (cotizacion presente pero no liquida no
    es confiable). Entre los candidatos con |delta| dentro de DELTA_RANGE
    (0.40-0.60), elige el de MAYOR VOLUMEN -- mas volumen = mas
    probabilidad de conseguir buen fill, no solo el delta mas cercano a
    0.50 (2026-09-09, a pedido del usuario). Si ningun candidato cae
    dentro del rango de delta (strikes muy espaciados respecto al precio,
    ej. AMC), se cae a elegir por |delta| mas cercano a TARGET_DELTA entre
    los que sI pasan el filtro de liquidez, para no devolver None de mas.
    None si no hay datos utilizables para este simbolo."""
    ticker = yf.Ticker(symbol)
    expirations = _with_timeout(lambda: ticker.options)
    if not expirations:
        return None

    today = date.today()
    expiration = _nearest_business_day_expiration(list(expirations), today)
    if expiration is None:
        return None

    chain = _with_timeout(lambda: ticker.option_chain(expiration))
    df = chain.calls if option_type == "call" else chain.puts
    if df is None or df.empty:
        return None

    exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
    years = max((exp_date - today).days, 1) / 365.0

    candidates = []  # (row, delta) -- solo los que pasan bid/ask y liquidez
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
            continue  # cotizacion presente pero sin liquidez real -- no confiable para entrar
        iv = float(row.get("impliedVolatility") or 0)
        if iv < MIN_IMPLIED_VOL:
            continue  # IV placeholder/invalida -- no confiable para calcular delta
        strike = float(row["strike"])
        delta = _bs_delta(spot_price, strike, years, iv, option_type)
        if delta is None:
            continue
        candidates.append((row, delta))

    if not candidates:
        return None

    in_range = [(row, d) for row, d in candidates if DELTA_RANGE[0] <= abs(d) <= DELTA_RANGE[1]]
    used_fallback_range = False
    if in_range:
        # Mayor volumen entre los que ya estan en el rango de delta objetivo.
        best_row, best_delta = max(in_range, key=lambda rd: float(rd[0].get("volume") or 0))
    else:
        used_fallback_range = True
        # Ningun strike cayo en el rango (strikes muy espaciados) -- se
        # cae a |delta| mas cercano a TARGET_DELTA, pero SOLO entre los
        # que igual caen dentro de FALLBACK_DELTA_RANGE -- un delta mas
        # extremo que eso no vale la pena ni como "mejor disponible".
        fallback = [(row, d) for row, d in candidates if FALLBACK_DELTA_RANGE[0] <= abs(d) <= FALLBACK_DELTA_RANGE[1]]
        if not fallback:
            return None
        # A pedido explicito del usuario (2026-09-11): "siempre comprar
        # los mejores contratos at the money con el mayor volumen" -- el
        # criterio de "mejor" es SIEMPRE volumen, igual que en el camino
        # principal (in_range de arriba), tambien aca en el fallback. Antes
        # elegia por delta mas cercano a 0.50, lo que podia preferir un
        # contrato casi sin operar (volumen 4) sobre uno mas liquido con
        # delta un poco mas lejos.
        best_row, best_delta = max(fallback, key=lambda rd: float(rd[0].get("volume") or 0))

    price = _option_row_price(best_row)
    if price is None:
        return None

    return {
        "strike": float(best_row["strike"]),
        "expiration": expiration,
        "price": price,
        "bid": float(best_row.get("bid") or 0) or None,
        "ask": float(best_row.get("ask") or 0) or None,
        "delta": round(best_delta, 4),
        "option_type": option_type,
        "open_interest": int(best_row.get("openInterest") or 0),
        "volume": int(best_row.get("volume") or 0),
        # Bug real encontrado 2026-09-15: family_sizing.select_affordable_contract()
        # etiquetaba CUALQUIER resultado de select_contract() como "primario
        # (rango 0.40-0.60)" sin chequear si en realidad vino del camino de
        # respaldo (FALLBACK_DELTA_RANGE, 0.25-0.75) -- un contrato con delta
        # 0.67 (fuera del rango objetivo pero dentro del respaldo, ambos
        # comportamientos correctos) se reportaba como si hubiera cumplido el
        # rango objetivo. Este flag permite armar el motivo real.
        "used_fallback_range": used_fallback_range,
    }
