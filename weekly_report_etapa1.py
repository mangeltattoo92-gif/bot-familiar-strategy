#!/usr/bin/env python3
"""
Informe de la ETAPA 1 (giro_sma20 con confianza media permitido, 2026-09-18) --
lo que se prometio: medir en vivo ~5 sesiones y dar la cifra real, comparada con la
semana anterior (14 al 18 de septiembre). Solo lectura; manda UN correo.

Uso:
  python weekly_report_etapa1.py                     # desde 2026-09-21 hasta hoy, con correo
  python weekly_report_etapa1.py --start 2026-09-14 --end 2026-09-18 --no-email   # prueba
"""
import argparse
import glob
import re
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from paper_trading.engine import get_status  # noqa: E402
from webapp.email_sender import send_alert_email  # noqa: E402

ALERT_TO_EMAIL = "miguelangelperezizquierdo160@gmail.com"
BASELINE_EQUITY = {"Familymaykel": 9730, "ROYENZO53": 9730, "camiloebmas": 9662, "jochoa": 9724, "miguel perez": 9768, "wenlg1214": 9732}
BEFORE = "SEMANA ANTERIOR (14 al 18 sep): cuentas -2.76%, operaciones -2.55% sobre lo invertido, sin giro_sma20 media"
LOG = ROOT / "data" / "multi_user_entry.log"


def closed_trades(start: str, end: str):
    rows = []
    for db in sorted(glob.glob(str(ROOT / "data" / "users" / "*" / "paper_trading.db"))):
        user = Path(db).parent.name
        if user == "demo_familiar":
            continue
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        opened = {}
        for t in conn.execute("SELECT * FROM trades WHERE asset_type='option' ORDER BY id"):
            key = (t["ticker"], t["option_details"])
            if t["side"] == "buy":
                opened.setdefault(key, []).append(t)
            elif opened.get(key):
                b = opened[key].pop(0)
                day = b["timestamp"][:10]
                if not (start <= day <= end):
                    continue
                cost = -b["cash_effect"]
                m = re.search(r"confianza (\w+)", b["reason"] or "")
                rows.append(dict(user=user, tk=b["ticker"], day=day, strat=b["entry_strategy"] or "?", conf=m.group(1) if m else "?",
                                 cost=cost, pnl=t["cash_effect"] - cost, minute=b["timestamp"][:16]))
        conn.close()
    return rows


def unique_signals(rows):
    g = defaultdict(list)
    for r in rows:
        g[(r["tk"], r["minute"], r["strat"], r["conf"])].append(r)
    return [dict(strat=k[2], conf=k[3], ret=st.mean(x["pnl"] / x["cost"] * 100 for x in v), day=v[0]["day"]) for k, v in g.items() if v[0]["cost"] > 0]


def build_report(start: str, end: str) -> str:
    rows = closed_trades(start, end)
    days = sorted({r["day"] for r in rows})
    out = [f"INFORME ETAPA 1 (giro_sma20 con confianza media) -- periodo {start} a {end}", f"Sesiones con operaciones: {len(days)} ({', '.join(days) or 'ninguna'})", ""]
    out.append("CUENTAS (valor actual frente al cierre del 18-sep)")
    tot0 = tot1 = 0.0
    for user, base in BASELINE_EQUITY.items():
        try:
            s = get_status(db_path=ROOT / "data" / "users" / user / "paper_trading.db")
            val = s.get("total_value") or s.get("account_value") or (s["cash_balance"] + s.get("positions_value", 0))
        except Exception as e:
            out.append(f"  {user}: no se pudo leer ({e})")
            continue
        tot0 += base
        tot1 += val
        out.append(f"  {user:<14} ${base:,.0f} -> ${val:,.0f} ({(val / base - 1) * 100:+.2f}%)")
    if tot0:
        out.append(f"  TOTAL 6 cuentas: ${tot0:,.0f} -> ${tot1:,.0f} ({(tot1 / tot0 - 1) * 100:+.2f}%, {tot1 - tot0:+,.0f} dolares)")
    out.append("")
    if rows:
        cost = sum(r["cost"] for r in rows)
        pnl = sum(r["pnl"] for r in rows)
        out.append(f"OPERACIONES CERRADAS: {len(rows)} | invertido ${cost:,.0f} | P&L ${pnl:+,.0f} ({pnl / cost * 100:+.2f}% sobre lo invertido)")
        for d in days:
            L = [r for r in rows if r["day"] == d]
            c, p = sum(r["cost"] for r in L), sum(r["pnl"] for r in L)
            out.append(f"  {d}: {len(L)} cierres, ${p:+,.0f} ({p / c * 100:+.2f}%)")
        big = [r for r in rows if r["cost"] > 1000]
        out.append(f"Contratos por encima de $1,000 (tope 10%): {len(big)} (debe ser 0)")
        sig = unique_signals(rows)
        out.append("")
        out.append("POR SEÑAL (una fila por señal, promedio entre cuentas):")
        for label, sel in (("giro_sma20 confianza MEDIA (lo nuevo)", [s for s in sig if s["strat"] == "giro_sma20" and s["conf"] == "media"]),
                           ("giro_sma20 confianza ALTA", [s for s in sig if s["strat"] == "giro_sma20" and s["conf"] == "alta"]),
                           ("squeeze_breakout (todas)", [s for s in sig if s["strat"] == "squeeze_breakout"]),
                           ("TODAS", sig)):
            if sel:
                r = [s["ret"] for s in sel]
                out.append(f"  {label:<38} n={len(r):>3} media={st.mean(r):+.2f}% aciertos={100 * sum(a > 0 for a in r) / len(r):.0f}%")
            else:
                out.append(f"  {label:<38} sin señales")
        worst = min(((d, sum(r["pnl"] for r in rows if r["day"] == d)) for d in days), key=lambda x: x[1])
        gm = [s["ret"] for s in sig if s["strat"] == "giro_sma20" and s["conf"] == "media"]
        ga = [s["ret"] for s in sig if s["strat"] == "giro_sma20" and s["conf"] == "alta"]
        out.append("")
        out.append("CRITERIOS DE REVERSION:")
        out.append(f"  - Peor dia: {worst[0]} con ${worst[1]:+,.0f} en total ({worst[1] / 60000 * 100:+.2f}% del capital de las 6 cuentas); umbral de alerta: -3% de una cuenta")
        if gm and ga:
            out.append(f"  - giro media {st.mean(gm):+.2f}% contra giro alta {st.mean(ga):+.2f}% -> " + ("media PEOR que alta: revisar" if st.mean(gm) < st.mean(ga) - 3 else "sin señal clara de reversion"))
        elif gm:
            out.append(f"  - giro media {st.mean(gm):+.2f}% (sin giro alta en el periodo para comparar)")
    else:
        out.append("Sin operaciones cerradas en el periodo.")
    breakers = 0
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            if "CIRCUIT BREAKER" in line and start <= line[1:11] <= end:
                breakers += 1
    out += ["", f"Circuit breakers disparados en el periodo: {breakers}", "", BEFORE]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-09-21")
    ap.add_argument("--end", default=datetime.now(timezone.utc).date().isoformat())
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args()
    body = build_report(args.start, args.end)
    print(body)
    if not args.no_email:
        ok = send_alert_email(ALERT_TO_EMAIL, f"[Bot Familiar] Informe etapa 1 ({args.start} a {args.end})", body)
        print("correo enviado:", ok)


if __name__ == "__main__":
    main()
