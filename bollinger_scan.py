#!/usr/bin/env python3
"""
Escanea la senal de la estrategia de Bandas de Bollinger (20,2) para un
simbolo. Solo lectura / analisis -- no coloca ninguna orden.

Ejemplos:
  python bollinger_scan.py SPY
  python bollinger_scan.py TSLA --interval 5m --period 5d
"""

import argparse

from paper_trading.bollinger_strategy import scan_all_signals


def main():
    parser = argparse.ArgumentParser(description="Escaner de senal Bollinger (20,2)")
    parser.add_argument("symbol")
    parser.add_argument("--interval", default="15m", help="Ej: 5m, 15m, 1h, 1d (default 15m)")
    parser.add_argument("--period", default="1mo", help="Historial a descargar (default 1mo)")
    args = parser.parse_args()

    results = scan_all_signals(args.symbol, interval=args.interval, period=args.period)
    r = next(res for res in results if res["strategy"] == "squeeze_breakout")
    gap_fade = next(res for res in results if res["strategy"] == "gap_fade_apertura")
    sma_turn = next(res for res in results if res["strategy"] == "giro_sma20")

    print(f"Simbolo: {r['symbol']}  |  Intervalo: {r['interval']}  |  Ultima barra: {r['timestamp']}")
    print(f"Precio: {r['last_close']:.2f}   Banda sup: {r['upper_band']:.2f}   Media: {r['sma']:.2f}   Banda inf: {r['lower_band']:.2f}")
    print()
    print("Tendencia por temporalidad:")
    for tf, trend in r["mtf_trends"].items():
        print(f"  {tf:4s}: {trend or 'sin datos suficientes'}")
    print()
    print(f"SENAL: {r['signal'].upper()}")
    if r["signal"] == "buy_call":
        print("  -> Squeeze + ruptura al alza con volumen alto: candidata a COMPRAR CALL")
    elif r["signal"] == "buy_put":
        print("  -> Squeeze + ruptura a la baja con volumen alto: candidata a COMPRAR PUT")
    else:
        print("  -> No se cumplen las 3 condiciones (squeeze + ruptura + volumen) todavia")
    if r["signal"] != "none":
        detail = r["confidence_detail"]
        print(f"  Confianza (vs tendencia mayor 1h/1d/1sem): {r['confidence'].upper()}")
        if detail["aligned"]:
            print(f"    A favor: {', '.join(detail['aligned'])}")
        if detail["against"]:
            print(f"    En CONTRA: {', '.join(detail['against'])} -- entrar aqui va contra la tendencia de fondo")
        if r["confidence"] == "baja":
            print("    RECOMENDACION: revisar manualmente antes de tomarla, no auto-ejecutar.")
    print()
    print("Explicacion (para el campo 'razon' del registro):")
    print(f"  1) {r['band_tightness_explanation']}")
    print(f"  2) {r['volume_explanation']}")
    print()
    print("Plan de salida si se toma esta senal:")
    print(f"  - Objetivo de ganancia: +{r['profit_target_pct']*100:.0f}% sobre la prima (volatilidad de entrada: {r['volatility_strength']})")
    print(f"  - Stop-loss: -{r['stop_loss_pct']*100:.0f}% sobre la prima si el mercado va en contra")
    print("  - O cierre por senal tecnica contraria, o al llegar el cierre de sesion (day trading)")
    print()
    if r["exit_long_signal"] or r["exit_short_signal"]:
        print("AVISO: hay senal tecnica de SALIDA activa ahora mismo:")
        if r["exit_long_signal"]:
            print("  - Cierra posiciones LARGAS / CALLS abiertas (cruce bajo la media o ruptura a la baja con volumen)")
        if r["exit_short_signal"]:
            print("  - Cierra posiciones CORTAS / PUTS abiertas (cruce sobre la media o ruptura al alza con volumen)")
    if r["force_eod_exit"]:
        print("AVISO: estamos cerca del cierre de sesion -- por regla de day trading, cierra cualquier posicion abierta de esta estrategia (no se mantiene overnight).")

    print()
    print("--- Estrategia 2: reversion de gap de apertura (9:30 ET) ---")
    print(f"SENAL: {gap_fade['signal'].upper()}")
    if gap_fade["signal"] == "buy_put":
        print("  -> Vela de apertura ALCISTA muy lejos de la banda superior: candidata a COMPRAR PUT (fade)")
    elif gap_fade["signal"] == "buy_call":
        print("  -> Vela de apertura BAJISTA muy lejos de la banda inferior: candidata a COMPRAR CALL (fade)")
    elif not gap_fade["is_opening_bar"]:
        print("  -> La ultima barra cerrada no es la de apertura (9:30 ET) de hoy -- esta estrategia solo evalua esa barra")
    else:
        print("  -> Es la barra de apertura, pero no quedo lo bastante lejos de ninguna banda")
    if gap_fade["signal"] != "none":
        detail = gap_fade["confidence_detail"]
        print(f"  Confianza (vs tendencia 1h): {gap_fade['confidence'].upper()}")
        if detail["aligned"]:
            print(f"    A favor: {', '.join(detail['aligned'])}")
        if detail["against"]:
            print(f"    En CONTRA: {', '.join(detail['against'])}")
    print(f"  {gap_fade['band_tightness_explanation']}")

    print()
    print("--- Estrategia 3: giro pronunciado de la SMA20 ---")
    print(f"SENAL: {sma_turn['signal'].upper()}")
    if sma_turn["signal"] == "buy_put":
        print("  -> SMA20 venia subiendo y giro bruscamente a bajar: candidata a COMPRAR PUT (a favor del giro)")
    elif sma_turn["signal"] == "buy_call":
        print("  -> SMA20 venia bajando y giro bruscamente a subir: candidata a COMPRAR CALL (a favor del giro)")
    else:
        print("  -> No hay un giro pronunciado de la SMA20 en la barra actual")
    if sma_turn["signal"] != "none":
        detail = sma_turn["confidence_detail"]
        print(f"  Confianza (vs tendencia 1h): {sma_turn['confidence'].upper()}")
        if detail["aligned"]:
            print(f"    A favor: {', '.join(detail['aligned'])}")
        if detail["against"]:
            print(f"    En CONTRA: {', '.join(detail['against'])}")
    print(f"  {sma_turn['band_tightness_explanation']}")


if __name__ == "__main__":
    main()
