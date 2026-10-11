#!/usr/bin/env python3
"""
Chequeo de noticias reales en tiempo real -- 2026-09-15, a pedido del
usuario. Usa el feed de noticias real de Yahoo Finance (via yfinance,
la MISMA fuente de datos que ya usa todo el sistema para precios) --
NO inventa ni "investiga" nada por su cuenta, solo lee los titulares
reales que Yahoo Finance ya tiene publicados para cada ticker.

Se llama en tiempo real (no cacheado) justo antes de decidir una
entrada: si un ticker tiene una noticia real publicada en las ultimas
NEWS_LOOKBACK_HOURS, esa entrada es mas riesgosa (el precio puede
moverse por la noticia, no por la señal tecnica) -- se exige confianza
alta, mismo mecanismo que los demas filtros de hoy (giro_sma20, volumen
de squeeze_breakout, sesgo diario). Solo se llama para señales que
todavia NO son confianza alta (si ya es alta, no hace falta chequear).
"""
from datetime import datetime, timedelta, timezone

import yfinance as yf

NEWS_LOOKBACK_HOURS = 6


def has_recent_news(symbol: str, hours: int = NEWS_LOOKBACK_HOURS) -> tuple[bool, str | None]:
    """(True, titular) si hay una noticia real de Yahoo Finance publicada
    en las ultimas `hours` horas para `symbol`. (False, None) si no hay
    noticias, o si el feed no responde (nunca bloquea una entrada por un
    error de red -- solo por evidencia real de que SI hay noticia)."""
    try:
        items = yf.Ticker(symbol).news
    except Exception:
        return False, None
    if not items:
        return False, None

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent: list[tuple[datetime, str]] = []
    for item in items:
        content = item.get("content", item)
        title = content.get("title")
        pub_raw = content.get("pubDate") or content.get("displayTime")
        if not title or not pub_raw:
            continue
        try:
            pub_dt = datetime.fromisoformat(str(pub_raw).replace("Z", "+00:00"))
        except Exception:
            continue
        if pub_dt >= cutoff:
            recent.append((pub_dt, title))

    if not recent:
        return False, None
    recent.sort(reverse=True)
    return True, recent[0][1]


# 2026-10-01, a pedido del usuario ("no estas pensando como trader... tienes
# que funsionar mejor... puedes tener mas informasion" -> "si" -> "hazlo con
# todas las companias"): has_recent_news() solo detecta SI hay noticia, nunca
# si es buena o mala -- un titular real puede CONTRADECIR la señal tecnica
# (ej. MRNA con sesgo diario "alcista" pero un titular real diciendo "es
# momento de un movimiento bajista"). Clasificacion por palabras clave
# financieras reales -- NO es una IA leyendo con criterio, es un diccionario
# fijo. Deliberadamente conservador: "neutral" (no bloquea nada) salvo que
# el titular tenga una palabra clara de una sola lista, nunca ambas listas a
# la vez (titular mixto/ambiguo -> neutral, nunca se adivina).
_BULLISH_WORDS = {
    "jumps", "jump", "surges", "surge", "soars", "soar", "rallies", "rally",
    "beats", "beat", "upgrade", "upgraded", "outperform", "bullish",
    "gains", "climbs", "climb", "rises", "rise", "record high", "wins",
    "win", "retreat", "strong", "exploded", "explode", "accelerat",
}
_BEARISH_WORDS = {
    "falls", "fall", "drops", "drop", "plunges", "plunge", "sinks", "sink",
    "downgrade", "downgraded", "underperform", "bearish", "cuts", "cut",
    "misses", "miss", "warns", "warn", "slips", "slip", "slides", "slide",
    "sell-off", "selloff", "lawsuit", "investigation", "recall", "probe",
}


def news_sentiment(symbol: str, hours: int = NEWS_LOOKBACK_HOURS) -> str:
    """'alcista' / 'bajista' / 'neutral' segun el titular real mas reciente
    de `symbol` en las ultimas `hours` horas. 'neutral' si no hay noticia
    reciente, si el feed falla, o si el titular no tiene ninguna palabra
    clara de las listas (nunca se fuerza una lectura ambigua). NO es
    analisis de IA en vivo -- es un diccionario fijo de palabras clave
    financieras en ingles (Yahoo Finance publica en ingles), pensado para
    detectar el caso CLARO (titular obviamente alcista o bajista), no para
    matices."""
    has_news, title = has_recent_news(symbol, hours=hours)
    if not has_news or not title:
        return "neutral"
    title_lower = title.lower()
    is_bullish = any(w in title_lower for w in _BULLISH_WORDS)
    is_bearish = any(w in title_lower for w in _BEARISH_WORDS)
    if is_bullish and not is_bearish:
        return "alcista"
    if is_bearish and not is_bullish:
        return "bajista"
    return "neutral"  # ninguna palabra clara, o las dos a la vez (titular mixto) -- no se adivina
