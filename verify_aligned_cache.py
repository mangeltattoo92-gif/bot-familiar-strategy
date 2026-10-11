#!/usr/bin/env python3
"""
Verifica el cache de historial alineado a las velas (2026-09-20): compara el
historial GUARDADO (el que usa el bot en ese momento) contra una descarga
fresca de Yahoo, sobre las ultimas velas CERRADAS. Si el cache guardado tiene
la ultima vela cerrada distinta o ausente en varios tickers, crea
data/aligned_cache_off (el bot vuelve solo al cache corto de 45 s en el
siguiente ciclo) y avisa por correo al administrador. Solo lectura + ese
archivo interruptor + un correo.

Uso: python verify_aligned_cache.py [--max 15] [--no-email]
"""
import argparse
import pickle
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import yfinance as yf  # noqa: E402

from paper_trading import bollinger_strategy as bs  # noqa: E402
from webapp.email_sender import send_alert_email  # noqa: E402

ALERT_TO = "miguelangelperezizquierdo160@gmail.com"
LOG = ROOT / "data" / "verify_aligned_cache.log"
LAST_BARS = 4
CRITICAL_FRACTION = 0.25  # si >=25% de los tickers comparados tiene la ultima vela cerrada distinta/ausente -> apagar
# 2026-09-21: con menos comparaciones que esto no se decide nada. El 11:30 am de hoy el verificador apago el cache
# por "4 de 4" -- pero corrio 21 s DESPUES del cierre de una vela y comparo archivos que el bot NO habria reutilizado
# (descargados antes del cierre): falsa alarma de horario, no un fallo del cache.
MIN_SAMPLES = 3


def _last_boundary(now: float) -> float:
    """Instante del ultimo cierre de vela de 15m (misma regla que bollinger_strategy._cache_is_fresh)."""
    step, offset = bs._ALIGNED_SPEC["15m"]
    return (now - offset) // step * step + offset


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def compare(sym: str, period: str, cache_path: Path):
    cached = pickle.load(open(cache_path, "rb"))
    fresh = yf.Ticker(sym).history(period=period, interval="15m", prepost=True)
    cached = bs._drop_unclosed_bar(cached.dropna(subset=["Open", "High", "Low", "Close"]), "15m")
    fresh = bs._drop_unclosed_bar(fresh.dropna(subset=["Open", "High", "Low", "Close"]), "15m")
    if fresh.empty or cached.empty:
        return None
    last_fresh = fresh.index[-1]
    critical = last_fresh not in cached.index
    minor = 0
    for ts in fresh.index[-LAST_BARS:]:
        if ts not in cached.index:
            continue
        a, b = cached.loc[ts], fresh.loc[ts]
        if any(abs(float(a[c]) - float(b[c])) > 1e-6 * max(abs(float(b[c])), 1.0) for c in ("Open", "High", "Low", "Close")):
            if ts == last_fresh:
                critical = True
            else:
                minor += 1
        elif abs(float(a["Volume"]) - float(b["Volume"])) > max(1000.0, 0.01 * float(b["Volume"])):
            minor += 1
    return critical, minor, str(last_fresh)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=15)
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--force", action="store_true", help="comparar aunque el mercado este cerrado (solo pruebas)")
    args = ap.parse_args()

    from datetime import time as dtime
    from zoneinfo import ZoneInfo

    et = datetime.now(ZoneInfo("America/New_York"))
    if not args.force and (et.weekday() >= 5 or not (dtime(9, 30) <= et.time() <= dtime(16, 0))):
        _log("Mercado cerrado -- no se verifica (los archivos guardados no reciben velas nuevas fuera de horario).")
        return 0

    files = sorted(bs._HIST_CACHE_DIR.glob("*_15m.pkl"), key=lambda p: p.stat().st_mtime, reverse=True)
    rows, skipped, not_reusable = [], 0, 0
    now = time.time()
    for p in files:
        if len(rows) >= args.max:
            break
        mtime = p.stat().st_mtime
        age = now - mtime
        if age < bs._HIST_CACHE_TTL or age > 1000:
            skipped += 1  # recien descargado, o tan viejo que el bot ya no lo reutiliza (pasaron mas de ~1 vela): no prueba el cache alineado
            continue
        # 2026-09-21: solo se juzga lo que el bot REALMENTE reutilizaria ahora: descargado despues del ultimo cierre de
        # vela + el retraso de confianza (_ALIGNED_TRUST_DELAY). Lo descargado antes ya es "viejo" para el bot.
        if mtime < _last_boundary(time.time()) + bs._ALIGNED_TRUST_DELAY:
            not_reusable += 1
            continue
        sym, period, _ = p.stem.rsplit("_", 2)
        boundary_before = _last_boundary(time.time())
        try:
            r = compare(sym, period, p)
        except Exception as e:
            _log(f"[{sym}] no se pudo comparar: {e!r}")
            continue
        if _last_boundary(time.time()) != boundary_before:
            not_reusable += 1  # cerro una vela mientras se descargaba: la comparacion ya no es valida
            continue
        if r is not None:
            rows.append((sym, *r, int(now - mtime)))
        time.sleep(1.0)

    crit = [r for r in rows if r[1]]
    minor = sum(r[2] for r in rows)
    verdict = "SIN DATOS PARA COMPARAR (mercado cerrado o cache nuevo)"
    turned_off = False
    if 0 < len(rows) < MIN_SAMPLES:
        verdict = f"MUESTRA INSUFICIENTE: solo {len(rows)} comparaciones validas (se necesitan {MIN_SAMPLES}) -- no se decide nada"
    elif rows:
        if len(crit) >= max(2, CRITICAL_FRACTION * len(rows)):
            bs._ALIGNED_OFF_FLAG.parent.mkdir(parents=True, exist_ok=True)
            bs._ALIGNED_OFF_FLAG.write_text(f"apagado automaticamente {datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8")
            turned_off = True
            verdict = f"PROBLEMA: {len(crit)} de {len(rows)} tickers con la ultima vela cerrada distinta/ausente -> cache alineado APAGADO automaticamente"
        else:
            verdict = f"OK: {len(rows) - len(crit)} de {len(rows)} tickers identicos en la ultima vela cerrada ({len(crit)} distintos, {minor} diferencias menores de volumen)"
    _log(verdict)
    body = verdict + "\n\n" + "\n".join(f"  {s}: ultima vela {ts} | {'DISTINTA/AUSENTE' if c else 'identica'} | dif. menores={m} | cache de hace {age}s" for s, c, m, ts, age in rows)
    body += f"\n\n(recien descargados o muy viejos omitidos: {skipped}; descargados antes del ultimo cierre de vela, que el bot no reutilizaria ahora: {not_reusable})"
    if not args.no_email:
        send_alert_email(ALERT_TO, "[Bot Familiar] Verificacion del cache alineado: " + ("PROBLEMA" if turned_off else "resultado"), body)
    return 1 if turned_off else 0


if __name__ == "__main__":
    sys.exit(main())
