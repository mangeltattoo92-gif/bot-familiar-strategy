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
