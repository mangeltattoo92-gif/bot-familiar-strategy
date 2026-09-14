#!/usr/bin/env python3
"""
Escanea TRES estrategias para cada ticker de la watchlist principal Y de la
watchlist de entrada rapida (fast_watchlist) configuradas en Configuracion
de operativa. Solo lectura / analisis -- no coloca ninguna orden.

1) squeeze_breakout: Bandas de Bollinger (20,2) -- squeeze + ruptura con
   volumen alto, A FAVOR de la ruptura (continuacion).
2) gap_fade_apertura: si la vela de apertura (9:30 ET) amanece MUY lejos de
   una banda, se opera EN CONTRA de ese gap (reversion), no a favor.
3) giro_sma20: si la SMA20 (linea del medio de las bandas) venia con
   tendencia clara y gira bruscamente de direccion, se opera A FAVOR del
   nuevo giro.

Cada señal se confirma contra la tendencia de 1h para evitar el error de
entrar en contra de la tendencia de fondo. Si varias señales de ENTRADA
aparecen a la vez (de cualquiera de las tres estrategias, en tickers
distintos), se priorizan por confianza (alta/media primero) y, dentro de la
misma confianza, por fuerza de volumen (ratio mas alto = mas convincente).
Se marca cuales caben dentro del limite diario de operaciones que quede
disponible.

El usuario autorizo ejecutar (registrar en el simulador) todas las señales
de confianza alta/media que quepan, sin pedir aprobacion una por una --
pero SIEMPRE en modo simulado, nunca ordenes reales. Las señales de
confianza BAJA (contradicen la tendencia mayor) se muestran pero NO se
auto-ejecutan: quedan para revision manual.

Uso:
  python watchlist_scan.py
  python watchlist_scan.py --interval 5m
"""

import argparse
from concurrent.futures import ThreadPoolExecutor

from paper_trading.bollinger_strategy import NO_NEW_ENTRY_MINUTES_BEFORE_CLOSE, scan_all_signals, too_close_to_open_new_position
from paper_trading.engine import get_settings, get_trades_count_today

CONFIDENCE_ORDER = {"alta": 0, "media": 1, "baja": 2}
MAX_PARALLEL_REQUESTS = 8  # descargas de yfinance son I/O -- en paralelo, no compiten por CPU


def main():
    parser = argparse.ArgumentParser(description="Escanea la watchlist configurada con la estrategia Bollinger")
    parser.add_argument("--interval", default="15m", help="Ej: 5m, 15m, 1h, 1d (default 15m)")
    parser.add_argument("--period", default="1mo", help="Historial a descargar (default 1mo)")
    args = parser.parse_args()

    settings = get_settings()
    watchlist = settings["watchlist"]
    fast_watchlist = settings["fast_watchlist"]
    max_trades = settings["max_trades_per_day"]
    trades_today = get_trades_count_today()
    remaining_capacity = max(0, max_trades - trades_today)
    near_close = too_close_to_open_new_position()
    if near_close:
        print(f"AVISO: faltan <= {NO_NEW_ENTRY_MINUTES_BEFORE_CLOSE} min para el cierre de sesion (o el "
              f"mercado ya cerro) -- no se abren posiciones nuevas, solo se reportan las señales para registro.\n")

    # Se combinan ambas listas: la principal y la de entrada rapida se
    # escanean y se tratan igual (misma logica de auto-ejecucion por
    # confianza) -- la fast_watchlist solo amplia el universo de tickers.
    all_symbols = list(dict.fromkeys(watchlist + fast_watchlist))

    print(f"Watchlist principal: {', '.join(watchlist)}")
    print(f"Watchlist entrada rapida: {', '.join(fast_watchlist)}")
    print(f"Operaciones hoy: {trades_today}/{max_trades}  (capacidad restante: {remaining_capacity})\n")

    def _scan_one(symbol):
        try:
            return symbol, scan_all_signals(symbol, interval=args.interval, period=args.period), None
        except Exception as e:
            return symbol, None, e

    # Las descargas de yfinance son I/O (red), no CPU -- se paralelizan para
    # no esperar ~1-1.5s por ticker en serie (30 tickers en serie tardaban
    # ~100s; en paralelo el tiempo total se acerca al de un solo ticker).
    # scan_all_signals() ademas reutiliza una sola descarga de 15m/1h por
    # ticker para las dos estrategias, en vez de pedirla dos veces.
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_REQUESTS) as pool:
        results = list(pool.map(_scan_one, all_symbols))

    signals = []
    for symbol, strategy_results, error in results:
        if error is not None:
            print(f"[{symbol}] ERROR: {error}\n")
            continue

        entries = [r for r in strategy_results if r["signal"] != "none"]
        if entries:
            signals.extend(entries)
        elif any(r["exit_long_signal"] or r["exit_short_signal"] for r in strategy_results):
            print(f"[{symbol}] Sin señal de entrada, pero hay señal tecnica de SALIDA activa "
                  f"para posiciones abiertas de este simbolo.")

    if not signals:
        print("Ningun ticker de ninguna de las dos watchlists cumple las condiciones de entrada "
              "de ninguna de las tres estrategias (squeeze_breakout / gap_fade_apertura / giro_sma20) ahora mismo.")
        return

    # Prioridad: confianza multi-temporalidad primero, luego fuerza de volumen.
    signals.sort(key=lambda r: (CONFIDENCE_ORDER[r["confidence"]], -r["volume_ratio"]))

    autoexec_candidates = [r for r in signals if r["confidence"] != "baja"]
    manual_review = [r for r in signals if r["confidence"] == "baja"]

    print(f"\n{len(signals)} señal(es) de entrada detectada(s):\n")
    executed_count = 0
    tickers_used_this_cycle = set()
    for r in signals:
        if near_close:
            estado = f"NO EJECUTABLE -- muy cerca del cierre de sesion (regla de {NO_NEW_ENTRY_MINUTES_BEFORE_CLOSE} min)"
        elif r["symbol"] in tickers_used_this_cycle:
            estado = "OMITIDO -- otra estrategia con mayor prioridad ya toma este mismo ticker en este ciclo"
        elif r["confidence"] == "baja":
            estado = "REVISION MANUAL -- contradice tendencia mayor"
        else:
            executed_count += 1
            would_execute = executed_count <= remaining_capacity
            estado = "SE EJECUTARIA" if would_execute else "EXCEDE EL LIMITE DIARIO -- se omite"
            tickers_used_this_cycle.add(r["symbol"])

        print(f"[{estado}] {r['symbol']} ({r['strategy']}) -> {r['signal'].upper()}  "
              f"(confianza {r['confidence']}, volumen {r['volume_ratio']:.2f}x)")
        detail = r["confidence_detail"]
        if detail["aligned"]:
            print(f"    A favor: {', '.join(detail['aligned'])} en la misma tendencia")
        if detail["against"]:
            print(f"    En contra: {', '.join(detail['against'])} van en tendencia opuesta")
        print(f"    {r['band_tightness_explanation']}")
        print(f"    {r['volume_explanation']}")
        print(f"    {r['exit_plan_explanation']}")
        print()

    if len(autoexec_candidates) > remaining_capacity:
        omitted = len(autoexec_candidates) - remaining_capacity
        print(f"AVISO: {omitted} señal(es) de confianza alta/media no se ejecutarian por falta de "
              f"capacidad diaria. Ajusta el limite en Configuracion de operativa si quieres tomarlas igual.")
    if manual_review:
        print(f"AVISO: {len(manual_review)} señal(es) contradicen la tendencia mayor (1h/1d/1sem) -- "
              f"no se auto-ejecutan, requieren tu revision explicita.")


if __name__ == "__main__":
    main()
