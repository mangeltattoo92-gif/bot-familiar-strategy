#!/usr/bin/env python3
"""
Analisis de sesgo diario del mercado -- 2026-09-15, a pedido del
usuario: "vas hacer un analisis cuando habra el mercado del dia y vas a
tener en cuenta si es alcista o bajista en cada etf o compañia para
cuando vayas a operar sepas los riesgos a la hora de entrar y salir del
contrato".

Corre una vez por dia (cron, cerca de la apertura) y calcula la
tendencia DIARIA (get_trend() con intervalo '1d' -- la MISMA funcion ya
usada para la confirmacion de 1h, reusada sin cambios) de cada ticker
del watchlist compartido: alcista, bajista o lateral, segun el ultimo
cierre diario CONFIRMADO (la barra del dia en curso todavia no cuenta --
se descarta igual que en cualquier otro intervalo, ver
_drop_unclosed_bar dentro de get_trend).

Se usa en multi_user_entry.py (y el piloto real, via la misma funcion
compartida) para:
- ENTRADA: si la señal va en contra del sesgo diario (CALL en un ticker
  bajista, o PUT en uno alcista), se exige confianza alta -- mismo
  mecanismo que ya se usa para giro_sma20 y los tickers "cuidado" de
  daily_guide.py.
- SALIDA: se suma al respaldo de 1h que ya existia en
  run_exits_for_account (hourly_aligned) -- ahora una posicion solo se
  considera "respaldada" si TANTO la tendencia de 1h COMO la diaria van
  a favor, asi la regla de asegurar ganancia temprana
  (EARLY_LOCK_WITHOUT_1H_SUPPORT_PCT en bollinger_strategy.py) reacciona
  mas rapido cuando el marco de tiempo mas grande ya esta en contra.

Guarda en data/daily_market_bias.json:
  {"updated_at": iso, "tickers": {SYM: "alcista"|"bajista"|"lateral"|null}}

Uso: python daily_market_bias.py
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from paper_trading.bollinger_strategy import get_trend  # noqa: E402
from paper_trading.engine import get_settings  # noqa: E402

OUTPUT_PATH = ROOT / "data" / "daily_market_bias.json"


def build_bias() -> dict:
    settings = get_settings()
    symbols = list(dict.fromkeys(settings["watchlist"] + settings["fast_watchlist"]))
    tickers = {}
    for sym in symbols:
        try:
            tickers[sym] = get_trend(sym, interval="1d", period="1y")
        except Exception:
            tickers[sym] = None
    return {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tickers": tickers}


def save_bias(data: dict) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_bias() -> dict:
    """{"SYM": "alcista"|"bajista"|"lateral"} -- {} si el archivo no
    existe todavia (primer dia, o el cron de la manana no corrio), asi
    ningun ticker queda bloqueado por falta de datos."""
    if not OUTPUT_PATH.exists():
        return {}
    try:
        return json.loads(OUTPUT_PATH.read_text(encoding="utf-8")).get("tickers", {})
    except Exception:
        return {}


def main():
    data = build_bias()
    save_bias(data)
    counts: dict[str, int] = {}
    for v in data["tickers"].values():
        key = v or "sin_dato"
        counts[key] = counts.get(key, 0) + 1
    print(f"Sesgo diario calculado para {len(data['tickers'])} tickers: {counts}")


if __name__ == "__main__":
    main()
