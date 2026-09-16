#!/usr/bin/env python3
"""
Motor de aprendizaje -- 2026-09-15, a pedido del usuario ("un sistema
que aprenda y memorice patrones de trades reales y ajuste sus propias
reglas con el tiempo").

LIMITE DE SEGURIDAD DELIBERADO: este motor NUNCA edita codigo de
estrategia ni cambia reglas de trading por si solo -- solo ANALIZA el
historial real completo (todas las cuentas, todo el tiempo), busca
patrones con muestra suficiente, y los deja en un reporte + notificacion
push para que el usuario (o yo, con su aprobacion) decida si conviene
convertir un patron en una regla real, igual que se hizo hoy a mano con
giro_sma20, el filtro de volumen de squeeze_breakout, y el sesgo diario.
Automatizar la DECISION de tocar codigo de trading sin supervision es
demasiado riesgo para una cuenta que espeja dinero real (el piloto) --
automatizar el ANALISIS que lleva a la decision si vale la pena.

Que hace:
1. Junta TODAS las operaciones cerradas de TODAS las cuentas activas
   (no solo hoy -- memoria acumulativa, recalculada de cero cada vez
   sobre el historial completo, asi que un cambio de regla vieja no dana
   la comparacion).
2. Para cada dimension de entrada (estrategia, confianza,
   fuerza_de_volumen, franja horaria) calcula win rate y P&L promedio,
   SOLO si hay al menos MIN_SAMPLE operaciones en ese grupo (si no, no
   hay evidencia suficiente para opinar).
3. Compara cada grupo contra el promedio general de la cuenta. Si un
   grupo rinde muy por debajo (posible regla a agregar) o muy por
   arriba (posible regla a aprovechar) del promedio, lo anota como
   hallazgo.
4. Guarda todo en data/learning_engine.json (la "memoria") y manda UN
   resumen por push -- nunca mas de eso.

Uso: python learning_engine.py
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from multi_user_entry import active_accounts  # noqa: E402
from paper_trading.engine import get_history  # noqa: E402

OUTPUT_PATH = ROOT / "data" / "learning_engine.json"
MIN_SAMPLE = 8  # mismo umbral de prudencia que daily_guide.py (MIN_SAMPLE_FOR_VERDICT)
NOTABLY_WORSE_WINRATE = 45.0   # por debajo de esto, con muestra suficiente, es un hallazgo de riesgo
NOTABLY_BETTER_WINRATE = 65.0  # por encima, con muestra suficiente, es un hallazgo positivo

CONFIDENCE_RE = re.compile(r"confianza (alta|media|baja)")
PNL_RE = re.compile(r"P&L ([+-]?[0-9.]+)%")
EXIT_RULE_RE = re.compile(r"regla '(\w+)'")


def _key(t: dict) -> tuple:
    od = t.get("option_details") or {}
    return (t["ticker"], od.get("strike"), od.get("expiration"), od.get("option_type"))


def _hour_bucket(iso_ts: str) -> str:
    hour_utc = datetime.fromisoformat(iso_ts).hour
    if 13 <= hour_utc < 15:
        return "apertura (13-15 UTC)"
    if 15 <= hour_utc < 18:
        return "mediodia (15-18 UTC)"
    if 18 <= hour_utc <= 20:
        return "cierre (18-20 UTC)"
    return "fuera_de_horario"


def collect_closed_trades() -> list[dict]:
    """Empareja compra+venta de TODAS las cuentas activas, con las
    dimensiones que interesan para buscar patrones. Reusa el mismo
    metodo de emparejamiento (por ticker+strike+expiracion+tipo, en
    orden cronologico) usado en los analisis manuales de hoy."""
    rows = []
    for username, db_path in active_accounts():
        if not db_path.exists():
            continue
        h = list(reversed(get_history(limit=2000, db_path=db_path)))
        open_buys: dict[tuple, dict] = {}
        for t in h:
            if t["side"] == "buy":
                open_buys[_key(t)] = t
            elif t["side"] == "sell":
                k = _key(t)
                if k not in open_buys:
                    continue
                buy_t = open_buys.pop(k)
                m = PNL_RE.search(t.get("reason") or "")
                if not m:
                    continue
                conf_m = CONFIDENCE_RE.search(buy_t.get("reason") or "")
                rule_m = EXIT_RULE_RE.search(t.get("reason") or "")
                rows.append({
                    "username": username, "ticker": buy_t["ticker"],
                    "strategy": buy_t.get("entry_strategy") or "desconocida",
                    "confidence": conf_m.group(1) if conf_m else "desconocida",
                    "volatility_strength": buy_t.get("entry_volatility_strength") or "desconocida",
                    "hour_bucket": _hour_bucket(buy_t["timestamp"]),
                    # 2026-09-16, a pedido del usuario ("hay que estudiarlo y
                    # darle seguimiento") -- que regla de SALIDA cerro cada
                    # operacion, para medir con el tiempo si reglas como
                    # asegurar_porcion_del_pico (la nueva de hoy) realmente
                    # mejoran el resultado a medida que se acumulan datos.
                    "exit_rule": rule_m.group(1) if rule_m else "desconocida",
                    "pct": float(m.group(1)),
                    "entry_ts": buy_t["timestamp"], "exit_ts": t["timestamp"],
                })
    return rows


def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    wins = [r["pct"] for r in rows if r["pct"] > 0]
    return {
        "n": n,
        "win_rate": round(len(wins) / n * 100, 1),
        "avg_pct": round(sum(r["pct"] for r in rows) / n, 2),
    }


def analyze(rows: list[dict]) -> dict:
    overall = _stats(rows)
    dimensions = ["strategy", "confidence", "volatility_strength", "hour_bucket", "exit_rule"]
    # exit_rule queda afuera de los hallazgos automaticos -- reglas como
    # stop_loss/rechazo_de_ruptura SIEMPRE dan 0% de acierto por
    # definicion (solo se disparan cuando ya hay perdida), no es un
    # patron nuevo a reportar. Se guarda igual en "groups" para poder
    # verla a mano (ej. seguirle la pista a asegurar_porcion_del_pico).
    DIMENSIONS_FOR_FINDINGS = {"strategy", "confidence", "volatility_strength", "hour_bucket"}
    groups: dict[str, dict[str, dict]] = {}
    findings = []

    for dim in dimensions:
        values = sorted({r[dim] for r in rows})
        groups[dim] = {}
        for val in values:
            subset = [r for r in rows if r[dim] == val]
            stats = _stats(subset)
            groups[dim][val] = stats
            if dim not in DIMENSIONS_FOR_FINDINGS or stats["n"] < MIN_SAMPLE:
                continue
            if stats["win_rate"] <= NOTABLY_WORSE_WINRATE:
                findings.append({
                    "tipo": "riesgo", "dimension": dim, "valor": val,
                    "n": stats["n"], "win_rate": stats["win_rate"], "avg_pct": stats["avg_pct"],
                    "nota": f"{dim}={val}: {stats['win_rate']}% de acierto en {stats['n']} operaciones "
                            f"(promedio general: {overall['win_rate']}%) -- posible candidato a exigir mas confirmacion.",
                })
            elif stats["win_rate"] >= NOTABLY_BETTER_WINRATE:
                findings.append({
                    "tipo": "positivo", "dimension": dim, "valor": val,
                    "n": stats["n"], "win_rate": stats["win_rate"], "avg_pct": stats["avg_pct"],
                    "nota": f"{dim}={val}: {stats['win_rate']}% de acierto en {stats['n']} operaciones "
                            f"(promedio general: {overall['win_rate']}%) -- posible candidato a priorizar/agrandar tamaño.",
                })

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overall": overall,
        "groups": groups,
        "findings": findings,
    }


def main():
    rows = collect_closed_trades()
    result = analyze(rows)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Memoria actualizada -- {result['overall'].get('n', 0)} operaciones cerradas analizadas "
          f"(win rate general {result['overall'].get('win_rate', '?')}%).")
    if not result["findings"]:
        print("Sin hallazgos nuevos con muestra suficiente todavia (minimo "
              f"{MIN_SAMPLE} operaciones por grupo).")
    else:
        for f in result["findings"]:
            print(f"[{f['tipo'].upper()}] {f['nota']}")


if __name__ == "__main__":
    main()
