#!/usr/bin/env python3
"""
Respaldo de seguridad para el day trading: si alguna posicion sigue abierta
al cambiar de dia (paso de las 12am), se cierra automaticamente al precio
de mercado actual, sin importar ganancia o perdida.

Esto NO deberia hacer falta en condiciones normales -- las reglas de salida
(objetivo, stop-loss, cierre de sesion a las 15:45 ET) ya deberian haber
cerrado todo antes del cierre de mercado. Este script es la ultima red de
seguridad por si el monitoreo automatico fallo o estuvo apagado.

Uso:
  python enforce_no_overnight.py            # cierra si corresponde
  python enforce_no_overnight.py --dry-run  # solo muestra que haria
"""

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from paper_trading.engine import get_status, get_settings
from webapp import market_data

MARKET_TZ = ZoneInfo("America/New_York")


def main():
    parser = argparse.ArgumentParser(description="Cierra posiciones que quedaron abiertas de un dia anterior")
    parser.add_argument("--dry-run", action="store_true", help="Solo mostrar, no cerrar de verdad")
    args = parser.parse_args()

    today_et = datetime.now(MARKET_TZ).date()
    status = get_status()

    if not status["positions"]:
        print("Sin posiciones abiertas -- nada que revisar.")
        return

    overnight = []
    for p in status["positions"]:
        if not p.get("opened_at"):
            continue
        opened_date = datetime.fromisoformat(p["opened_at"]).astimezone(MARKET_TZ).date()
        if opened_date < today_et:
            overnight.append(p)

    if not overnight:
        print(f"Todas las posiciones abiertas son de hoy ({today_et}) -- nada que forzar.")
        return

    print(f"AVISO: {len(overnight)} posicion(es) quedaron abiertas de un dia anterior "
          f"(viola la regla de day trading, no overnight):")

    for p in overnight:
        label = f"{p['ticker']} {p['option_details']['option_type'].upper()} ${p['option_details']['strike']}" \
            if p["asset_type"] == "option" else p["ticker"]
        opened_date = datetime.fromisoformat(p["opened_at"]).astimezone(MARKET_TZ).date()
        print(f"  - {label}: abierta el {opened_date}, cantidad {p['quantity']}, "
              f"precio entrada {p['avg_cost']}, precio actual {p['market_price']}")

        if args.dry_run:
            print("    (--dry-run: no se cerro)")
            continue

        od = p.get("option_details")
        if p["asset_type"] == "option" and od:
            current_price = market_data.get_option_price(
                p["ticker"], od["expiration"], od["strike"], od["option_type"]
            )
        else:
            current_price = market_data.get_quote(p["ticker"])

        if current_price is None:
            current_price = p["market_price"]  # ultimo precio conocido como respaldo

        import subprocess
        cmd = [
            "python", "paper_trade.py", "sell",
            "--ticker", p["ticker"],
            "--asset-type", p["asset_type"],
            "--quantity", str(p["quantity"]),
            "--price", str(current_price),
            "--reason", (
                f"Cierre forzado automatico: la posicion quedo abierta desde {opened_date} "
                f"hasta despues de medianoche. Day trading no permite mantener posiciones "
                f"overnight -- se cierra sin importar ganancia/perdida como respaldo de "
                f"seguridad (las reglas normales de salida deberian haber cerrado esto antes)."
            ),
        ]
        if p["asset_type"] == "option":
            cmd += [
                "--option-type", od["option_type"],
                "--strike", str(od["strike"]),
                "--expiration", od["expiration"],
                "--multiplier", str(p["multiplier"]),
            ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        print(result.stdout)
        if result.returncode != 0:
            print("ERROR cerrando la posicion:", result.stderr)


if __name__ == "__main__":
    main()
