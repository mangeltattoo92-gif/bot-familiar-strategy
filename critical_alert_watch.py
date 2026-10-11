#!/usr/bin/env python3
"""
Alertas por correo real, independientes de cualquier sesion de Claude Code
-- 2026-09-18, a pedido del usuario tras identificar una debilidad real:
"el monitoreo activo depende de que yo este corriendo en una sesion...
no hay alerta 24/7 independiente de mi". Los monitores de Claude Code
(Monitor tool) dejan de existir en cuanto termina la sesion -- este
script es un proceso de servidor de verdad (systemd), corre 24/7, y
manda un correo real (reusa email_sender.send_alert_email, mismo Gmail
SMTP ya probado en produccion) cuando aparece algo critico en los logs.

Que vigila (mismos patrones que se usaban a mano con el Monitor tool
durante la sesion del 2026-09-17): Traceback, ERROR:, Exception,
CIRCUIT BREAKER, reparacion automatica, colgado, no pude verificar.
Deliberadamente NO incluye "CIERRE" (cierres normales) -- eso es
actividad rutinaria, no algo que amerite un correo.

Estado (que tanto de cada log ya se reviso) se guarda en disco para que
un reinicio del servicio no vuelva a mandar alertas de lineas viejas ni
se salte lineas nuevas escritas mientras estaba parado.

Uso:
  python critical_alert_watch.py --once      # un solo chequeo (pruebas)
  python critical_alert_watch.py             # loop continuo, ver
    critical_alert_watch.service para produccion
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from webapp.email_sender import send_alert_email  # noqa: E402
from multi_user_entry import active_accounts  # noqa: E402
from paper_trading.engine import get_settings, get_status  # noqa: E402

LOG_PATHS = [
    Path("/opt/bot-familiar/data/multi_user_entry.log"),
    Path("/opt/bot-familiar-real-pilot/pilot.log"),
    Path("/opt/bot-familiar/deep_health.log"),
    Path("/opt/bot-familiar-real-pilot/health.log"),
    Path("/opt/bot-familiar/watchdog.log"),
]
ALERT_TO_EMAIL = "miguelangelperezizquierdo160@gmail.com"
CHECK_INTERVAL_SECONDS = 30
# Cooldown minimo entre correos, aunque sigan apareciendo lineas nuevas que
# califiquen -- evita saturar la bandeja de entrada si algo falla en bucle.
MIN_MINUTES_BETWEEN_EMAILS = 5

STATE_PATH = ROOT / "data" / "critical_alert_watch_state.txt"
LAST_EMAIL_PATH = ROOT / "data" / "critical_alert_watch_last_email.txt"
LOG_PATH = ROOT / "data" / "critical_alert_watch.log"
# Guarda el ultimo bot_enabled visto por cuenta -- 2026-09-18, tras descubrir
# que demo_familiar (apagada a proposito, de forma permanente) disparaba este
# correo cada ~5 min sin parar. Solo se alerta en la TRANSICION encendido->
# apagado, no mientras se mantiene apagada, para que una cuenta apagada por
# decision (demo/circuit breaker ya notificado) no sature el correo.
BOT_STATE_PATH = ROOT / "data" / "critical_alert_watch_bot_state.json"

PATTERN = re.compile(r"Traceback|ERROR:|Exception|CIRCUIT BREAKER|reparacion automatica|colgado|no pude verificar")


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _load_offsets() -> dict[str, int]:
    if not STATE_PATH.exists():
        return {}
    offsets = {}
    for line in STATE_PATH.read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        path, offset = line.split("\t", 1)
        try:
            offsets[path] = int(offset)
        except ValueError:
            continue
    return offsets


def _save_offsets(offsets: dict[str, int]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        "\n".join(f"{path}\t{offset}" for path, offset in offsets.items()), encoding="utf-8"
    )


def _new_matching_lines(offsets: dict[str, int]) -> list[str]:
    """Lee SOLO lo nuevo (desde el offset guardado) de cada log, y
    devuelve las lineas que matchean el patron critico. Si un log se
    rotó/achicó desde la ultima vez, arranca de nuevo desde el principio
    de ese archivo (no se rompe, solo puede repetir alguna linea vieja
    una unica vez)."""
    matched = []
    for path in LOG_PATHS:
        if not path.exists():
            continue
        key = str(path)
        size = path.stat().st_size
        start = offsets.get(key, size)  # primera vez: no re-mandar todo el historial viejo
        if size < start:
            start = 0
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(start)
            for line in f:
                if PATTERN.search(line):
                    matched.append(f"[{path.name}] {line.rstrip()}")
        offsets[key] = size
    return matched


def _proactive_health_issues() -> list[str]:
    """Chequeos activos, no solo reactivos a lo que ya quedo escrito en un
    log -- 2026-09-18, a pedido del usuario tras identificar que "varios
    bugs de ayer se descubrieron DESPUES de que ya habian afectado cuentas
    reales... el patron es reactivo". Esto no reemplaza la correccion de
    cada bug real (eso ya paso), pero agrega una red de seguridad para
    detectar sintomas conocidos (bot apagado sin que nadie se entere,
    posicion sin proteccion configurada) sin esperar a que alguien
    pregunte por que no hay operaciones."""
    issues = []
    try:
        accounts = active_accounts()
    except Exception as e:
        return [f"No se pudo leer la lista de cuentas activas: {e}"]

    try:
        bot_state = json.loads(BOT_STATE_PATH.read_text(encoding="utf-8")) if BOT_STATE_PATH.exists() else {}
    except (ValueError, OSError):
        bot_state = {}

    for username, db_path in accounts:
        try:
            settings = get_settings(db_path)
            bot_enabled = settings.get("bot_enabled", True)
            was_enabled = bot_state.get(username, True)
            if not bot_enabled and was_enabled:
                issues.append(f"Cuenta '{username}': el bot se APAGO (circuit breaker u otro motivo) -- "
                               f"no va a operar hasta que alguien lo reactive.")
            bot_state[username] = bot_enabled
            status = get_status(db_path=db_path)
            for p in status.get("positions", []):
                if p.get("profit_target_pct") is None or p.get("stop_loss_pct") is None:
                    issues.append(f"Cuenta '{username}': posicion {p['ticker']} SIN objetivo/stop-loss "
                                   f"configurado (profit_target_pct={p.get('profit_target_pct')}, "
                                   f"stop_loss_pct={p.get('stop_loss_pct')}) -- no se va a cerrar sola por esas reglas.")
        except Exception as e:
            issues.append(f"Cuenta '{username}': error al chequear su estado ({e}).")

    try:
        BOT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BOT_STATE_PATH.write_text(json.dumps(bot_state), encoding="utf-8")
    except OSError:
        pass
    return issues


def _minutes_since_last_email() -> float:
    if not LAST_EMAIL_PATH.exists():
        return float("inf")
    try:
        last = datetime.fromisoformat(LAST_EMAIL_PATH.read_text(encoding="utf-8").strip())
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - last).total_seconds() / 60


def run_once() -> int:
    offsets = _load_offsets()
    matched = _new_matching_lines(offsets)
    _save_offsets(offsets)
    issues = _proactive_health_issues()
    total = len(matched) + len(issues)
    if total == 0:
        return 0

    if _minutes_since_last_email() < MIN_MINUTES_BETWEEN_EMAILS:
        _log(f"{total} problema(s) nuevo(s) ({len(matched)} de logs, {len(issues)} de chequeo proactivo), "
             f"pero dentro del cooldown de {MIN_MINUTES_BETWEEN_EMAILS} min -- no se manda correo esta vez "
             f"(ya quedaron registrados).")
        return total

    sections = []
    if issues:
        sections.append("Chequeo proactivo (no espero a que aparezca un error en el log):\n" + "\n".join(issues))
    if matched:
        sections.append(
            f"Lineas criticas en los logs ({len(matched)}):\n" + "\n".join(matched[:20])
            + ("\n(... hay mas, ver critical_alert_watch.log en el servidor)" if len(matched) > 20 else "")
        )
    body = "\n\n".join(sections)
    sent = send_alert_email(ALERT_TO_EMAIL, "[Bot Familiar] Alerta critica del sistema", body)
    if sent:
        LAST_EMAIL_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_EMAIL_PATH.write_text(datetime.now(timezone.utc).isoformat(timespec="seconds"), encoding="utf-8")
        _log(f"Correo de alerta enviado -- {total} problema(s).")
    else:
        _log(f"FALLO el envio del correo de alerta -- {total} problema(s) sin notificar por email "
             f"(quedan en este log igual).")
    return total


def main():
    parser = argparse.ArgumentParser(description="Alertas por correo, independientes de sesion")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if args.once:
        n = run_once()
        print(f"{n} linea(s) critica(s) encontrada(s) este chequeo.")
        return

    _log(f"critical_alert_watch.py iniciado -- chequeo cada {CHECK_INTERVAL_SECONDS}s.")
    while True:
        try:
            run_once()
        except Exception as e:
            _log(f"[critical_alert_watch] ERROR interno: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
