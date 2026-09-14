"""
Seleccion de contrato de opcion (delta + vencimiento objetivo), para las
entradas de la estrategia de Bandas de Bollinger (day trading, velas de 15
minutos).

Este modulo es PURO (no llama a ningun API): recibe datos ya consultados en
vivo via el MCP de Robinhood (get_option_chains / get_option_instruments /
get_option_quotes, que incluye el campo 'delta') y devuelve:
  - que vencimiento pedir (7 dias habiles desde hoy)
  - que contrato comprar (delta entre 0.50 y 0.65, mas cerca de "dentro del
    dinero" que exactamente en el dinero -- ver DEFAULT_DELTA_RANGE)

Los puts reportan delta negativo (ej. -0.55) -- se compara por valor
absoluto, ya que el rango es una franja de "moneyness", no de signo.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")

# Rango de delta objetivo: "en el dinero (ATM) pero lo mas cercano posible a
# dentro del dinero (ITM)", a pedido explicito del usuario (2026-09-08,
# cambiado desde el rango simetrico anterior 0.40-0.60/mid 0.50). Mas delta
# = mas prima/mas valor intrinseco/mas probabilidad de terminar ITM, a
# cambio de menos apalancamiento (movimiento porcentual mas chico por cada
# punto que se mueva el subyacente) que un contrato mas afuera del dinero.
DEFAULT_DELTA_RANGE = (0.50, 0.65)
DEFAULT_DELTA_MID = 0.60
DEFAULT_EXPIRATION_BUSINESS_DAYS = 7

# Volumen minimo para que un contrato se considere "liquido" y se prefiera
# sobre otro con delta igual de cercano a target_mid pero menos negociado.
# Motivo: un strike casi sin volumen (ej. GLD 403, volume=1) puede no estar
# listado en yfinance -- la fuente gratuita que usa el panel web y el
# monitor continuo para seguir el precio -- forzando a interpolar entre
# strikes vecinos, lo que puede desviarse varios % del precio real de
# mercado (visto en vivo: ~5% de diferencia). Preferir volumen real reduce
# el riesgo de terminar en un strike que el panel no pueda seguir con precision.
# Subido de 50 a 100 (2026-09-08) a pedido explicito del usuario ("con mayor
# volumen"), priorizando liquidez con mas exigencia todavia.
DEFAULT_MIN_PREFERRED_VOLUME = 100


def add_business_days(start_date: date, business_days: int) -> date:
    """Suma dias habiles (lunes-viernes) a una fecha. No conoce festivos de
    mercado (ej. Labor Day) -- para eso, target_expiration_from_chain()
    ajusta al vencimiento REAL mas cercano de la cadena de opciones."""
    current = start_date
    remaining = business_days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def target_expiration_date(
    business_days: int = DEFAULT_EXPIRATION_BUSINESS_DAYS,
    from_date: date | None = None,
) -> date:
    """Fecha objetivo de vencimiento: `business_days` dias habiles desde hoy
    (hoy = fecha de calendario en hora de mercado, NY -- no la fecha local
    del sistema, que podria estar en otro huso horario)."""
    from_date = from_date or datetime.now(MARKET_TZ).date()
    return add_business_days(from_date, business_days)


def pick_closest_expiration(available_expirations: list[str], target: date | None = None) -> str:
    """De las expiraciones REALES que ofrece la cadena (get_option_chains,
    formato 'YYYY-MM-DD'), elige la mas cercana a la fecha objetivo. Esto
    absorbe automaticamente festivos de mercado, ya que un dia festivo
    nunca aparece como expiracion disponible."""
    if not available_expirations:
        raise ValueError("La cadena de opciones no tiene expiraciones disponibles")
    target = target or target_expiration_date()

    def _distance(exp_str: str) -> int:
        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        return abs((exp_date - target).days)

    return min(available_expirations, key=lambda e: (_distance(e), e))


def select_by_delta(
    candidates: list[dict],
    target_range: tuple[float, float] = DEFAULT_DELTA_RANGE,
    target_mid: float = DEFAULT_DELTA_MID,
    min_preferred_volume: int = DEFAULT_MIN_PREFERRED_VOLUME,
) -> dict | None:
    """Elige el mejor contrato de una lista de candidatos.

    candidates: lista de dicts, cada uno con al menos 'strike' y 'delta'
    (delta puede ser negativo para puts). Se recomienda incluir tambien
    'instrument_id', 'mark_price' (o 'bid'/'ask') y 'volume' -- este ultimo
    para poder preferir contratos liquidos (ver DEFAULT_MIN_PREFERRED_VOLUME).
    Si no se incluye 'volume', se asume 0 (no liquido) para ese candidato.

    Dentro del rango de delta objetivo, se prefiere el subconjunto con
    volumen >= min_preferred_volume (y, dentro de ese subconjunto, el mas
    cercano a target_mid). Si NINGUN candidato del rango cumple el minimo
    de volumen, se usa el rango completo igual -- la falta de liquidez no
    bloquea la entrada, solo se evita cuando hay alternativa mejor.

    Devuelve None si no hay ningun candidato con delta valido. En otro
    caso devuelve:
      {
        "selected": <candidato elegido>,
        "in_target_range": bool,   # False si tuvo que usar un fallback fuera de rango
        "candidates_in_range": [...],  # todos los que si caian en el rango, ordenados por cercania a target_mid
        "used_liquid_pool": bool,  # True si el elegido salio del subconjunto liquido
      }
    """
    valid = [c for c in candidates if c.get("delta") is not None]
    if not valid:
        return None

    lo, hi = target_range
    in_range = [c for c in valid if lo <= abs(c["delta"]) <= hi]
    in_range.sort(key=lambda c: abs(abs(c["delta"]) - target_mid))

    pool = in_range if in_range else valid
    liquid_pool = [c for c in pool if (c.get("volume") or 0) >= min_preferred_volume]
    search_pool = liquid_pool if liquid_pool else pool

    best = min(search_pool, key=lambda c: abs(abs(c["delta"]) - target_mid))

    return {
        "selected": best,
        "in_target_range": best in in_range,
        "candidates_in_range": in_range,
        "used_liquid_pool": bool(liquid_pool),
    }


def describe_selection(result: dict, option_type: str) -> str:
    """Texto legible para incluir en la razon del trade."""
    if result is None:
        return "No se encontraron contratos con delta disponible para elegir por moneyness."

    c = result["selected"]
    delta = c["delta"]
    strike = c["strike"]
    volume = c.get("volume")
    liquidity_note = (
        f" Volumen {volume:,} (liquido, >= {DEFAULT_MIN_PREFERRED_VOLUME})." if result.get("used_liquid_pool")
        else f" AVISO: volumen bajo ({volume if volume is not None else 'sin dato'}, < {DEFAULT_MIN_PREFERRED_VOLUME}) -- "
             f"ningun contrato en rango tenia mejor liquidez; el precio de seguimiento puede desviarse del real "
             f"si no esta listado en yfinance."
    )
    if result["in_target_range"]:
        return (
            f"Contrato {option_type.upper()} strike {strike} elegido por delta {delta:.3f} "
            f"(dentro del rango objetivo {DEFAULT_DELTA_RANGE[0]:.2f}-{DEFAULT_DELTA_RANGE[1]:.2f})."
            f"{liquidity_note}"
        )
    return (
        f"AVISO: ningun contrato {option_type.upper()} disponible tenia delta dentro de "
        f"{DEFAULT_DELTA_RANGE[0]:.2f}-{DEFAULT_DELTA_RANGE[1]:.2f}. Se eligio el mas cercano: "
        f"strike {strike} con delta {delta:.3f} (fuera de rango, usar con precaucion)."
    )
