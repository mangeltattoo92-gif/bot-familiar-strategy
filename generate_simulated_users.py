#!/usr/bin/env python3
"""
Genera N usuarios simulados con capital variado (2026-10-05, a pedido
explicito: "prepara para agregar los 500 usuarios simulados con cuentas
de diferentes montos, quiero ver como funciona mañana el sistema, si
traba o no y si yfinance se lentiza").

Cada cuenta: username "simuser0001".."simuser0500" (prefijo facil de
filtrar/borrar despues), NO admin, familiar_access='none' (quedan del
lado Metabot, no se mezclan con las cuentas familiares reales), status
'active' (entran directo al loop real sin pasar por aprobacion manual
500 veces), y un paper_trading.db propio con initial_balance variado:
  - 100 cuentas "capital muy bajo"   ($50 - $500)
  - 150 cuentas "capital bajo"        ($500 - $2,000)
  - 150 cuentas "capital medio"       ($2,000 - $10,000)
  - 100 cuentas "capital alto"        ($10,000 - $100,000)
A proposito un rango amplio -- las de capital muy bajo son las que
deberian activar la lista por capital (default_capital_list_enabled)
cuando se las prenda a mano o se decida activarla por defecto para
testear esa regla con casos reales de poco capital.

NO se toca is_admin, NO hay vinculo con el piloto de dinero real (eso es
una cuenta aparte, Robinhood de Miguel, otro mecanismo por completo).

Uso (en el servidor, como el usuario del servicio real para que los
archivos queden con los permisos correctos):
  sudo -u botfam venv/bin/python3 generate_simulated_users.py --count 500
  sudo -u botfam venv/bin/python3 generate_simulated_users.py --cleanup   # borra todo lo generado
"""
import argparse
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webapp.auth import register_user, DEFAULT_USERS_DB
from paper_trading.engine import init_db

PREFIX = "simuser"
USERS_DATA_DIR = Path(__file__).resolve().parent / "data" / "users"

CAPITAL_TIERS = [
    (0.20, 50, 500),       # 20% muy bajo capital -- clave para probar la lista por capital
    (0.30, 500, 2000),     # 30% bajo
    (0.30, 2000, 10000),   # 30% medio
    (0.20, 10000, 100000), # 20% alto
]


def _pick_balance(rng: random.Random) -> float:
    roll = rng.random()
    acc = 0.0
    for weight, lo, hi in CAPITAL_TIERS:
        acc += weight
        if roll <= acc:
            return round(rng.uniform(lo, hi), 2)
    return round(rng.uniform(*CAPITAL_TIERS[-1][1:]), 2)


def generate(count: int, seed: int = 20261005):
    rng = random.Random(seed)
    conn = sqlite3.connect(DEFAULT_USERS_DB)
    cur = conn.cursor()
    created = 0
    for i in range(1, count + 1):
        username = f"{PREFIX}{i:04d}"
        email = f"{username}@sim.local"
        cur.execute("SELECT 1 FROM users WHERE username = ?", (username,))
        if cur.fetchone():
            continue  # ya existe (reintento), no duplicar
        try:
            register_user(username, email, "SimPassword2026!", terms_accepted=True)
        except ValueError:
            continue
        # activar directo (sin pasar por el flujo de aprobacion/codigo 500 veces)
        cur.execute("UPDATE users SET status = 'active', activated_at = datetime('now') WHERE username = ?", (username,))
        conn.commit()

        balance = _pick_balance(rng)
        db_path = USERS_DATA_DIR / username / "paper_trading.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        init_db(balance, db_path=db_path)
        created += 1
        if created % 50 == 0:
            print(f"  {created}/{count} creadas...", flush=True)
    conn.close()
    print(f"Listo: {created} cuentas simuladas nuevas (capital entre ${CAPITAL_TIERS[0][1]} y ${CAPITAL_TIERS[-1][2]}).")


def cleanup():
    import shutil
    conn = sqlite3.connect(DEFAULT_USERS_DB)
    cur = conn.cursor()
    cur.execute("SELECT username FROM users WHERE username LIKE ?", (f"{PREFIX}%",))
    usernames = [r[0] for r in cur.fetchall()]
    cur.execute("DELETE FROM users WHERE username LIKE ?", (f"{PREFIX}%",))
    conn.commit()
    conn.close()
    for u in usernames:
        d = USERS_DATA_DIR / u
        if d.exists():
            shutil.rmtree(d)
    print(f"Borradas {len(usernames)} cuentas simuladas y sus datos.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=500)
    ap.add_argument("--cleanup", action="store_true")
    args = ap.parse_args()
    if args.cleanup:
        cleanup()
    else:
        generate(args.count)
