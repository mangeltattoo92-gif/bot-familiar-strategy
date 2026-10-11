#!/usr/bin/env python3
"""Corre una vez por dia (ver systemd) y desconecta a cualquier cuenta
Metabot que sigue en plan Gratis mas de 7 dias despues de activarse, sin
haber pagado Pro -- a pedido explicito del usuario 2026-10-11 ("si no
hace ese pago en un plazo de 7 dias la misma plataforma lo saca
automaticamente del sistema").

Reutiliza EXACTAMENTE el mismo mecanismo que ya usa el admin para
desconectar una cuenta a mano (webapp.auth.disconnect_user -> status
'rejected', nunca borra nada, reactivable con el boton "Reconectar" que
ya existe en el panel de administrador). Nunca toca cuentas admin, ya
en Pro, ni con acceso aprobado a Bot Familiar (gratis siempre).

Uso: python trial_expiry_check.py   (lo levanta systemd, ver
bot-familiar-trial-expiry.service/.timer)
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from webapp.auth import expire_unpaid_trials  # noqa: E402

LOG_PATH = ROOT / "data" / "trial_expiry.log"


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> None:
    expired = expire_unpaid_trials(trial_days=7)
    if expired:
        _log(f"{len(expired)} cuenta(s) desconectada(s) por no pagar Pro en 7 dias: {', '.join(expired)}")
    else:
        _log("Nada que desconectar.")


if __name__ == "__main__":
    main()
