"""
Calendario de dias habiles del NYSE (festivos + cierres anticipados), via
pandas_market_calendars -- para que el sistema sepa cuando el mercado esta
realmente cerrado aunque sea un dia de semana (ej. Dia de Accion de
Gracias), y cuando cierra mas temprano (ej. vispera de Navidad, 13:00 ET
en vez de 16:00 ET).

Todo cacheado por año -- el calculo del calendario completo de un año tarda
~0.1s, cachearlo evita recalcularlo en cada llamada.
"""

from datetime import date, time as dtime
from functools import lru_cache

import pandas_market_calendars as mcal

MARKET_CLOSE_TIME_REGULAR = dtime(16, 0)
MARKET_CLOSE_TIME_EARLY = dtime(13, 0)

_NYSE = mcal.get_calendar("NYSE")


@lru_cache(maxsize=8)
def _year_schedule(year: int):
    sched = _NYSE.schedule(start_date=f"{year}-01-01", end_date=f"{year}-12-31")
    trading_days = set(sched.index.date)
    early_closes = set(_NYSE.early_closes(sched).index.date)
    return trading_days, early_closes


def is_trading_day(d: date) -> bool:
    """True si el NYSE opera ese dia (False en fin de semana Y en festivos)."""
    trading_days, _ = _year_schedule(d.year)
    return d in trading_days


def is_early_close(d: date) -> bool:
    """True si ese dia el mercado cierra temprano (ej. vispera de Navidad)."""
    _, early_closes = _year_schedule(d.year)
    return d in early_closes


def market_close_time(d: date) -> dtime:
    """Hora de cierre regular de ese dia (13:00 ET en dias de cierre
    anticipado, 16:00 ET en un dia normal de operacion -- no valida si `d`
    es realmente un dia de mercado, usar is_trading_day() para eso)."""
    return MARKET_CLOSE_TIME_EARLY if is_early_close(d) else MARKET_CLOSE_TIME_REGULAR


def holidays_in_year(year: int) -> list[date]:
    """Festivos del NYSE en `year` (dias de semana en que el mercado no abre)."""
    import pandas as pd
    trading_days, _ = _year_schedule(year)
    all_weekdays = [d.date() for d in pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="D") if d.weekday() < 5]
    return sorted(d for d in all_weekdays if d not in trading_days)
