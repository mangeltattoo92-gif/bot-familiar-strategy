#!/usr/bin/env python3
"""Arranca el panel Bot Familiar en http://127.0.0.1:5001

Puerto 5001, NO 5000 (bug real encontrado 2026-09-10/11): trading-bot
YA usa el 5000 en la misma PC durante desarrollo -- dos Flask corriendo
en el mismo puerto no dan error visible en Windows, el segundo arranca
"silencioso" pero nunca recibe trafico real (todo el trafico va al
primero que agarro el puerto). Se detecto porque bot-familiar nunca
logueaba ningun request pese a estar "corriendo"."""

from paper_trading.singleton_lock import acquire_single_instance_lock
from webapp.app import app

if __name__ == "__main__":
    acquire_single_instance_lock("run_dashboard")
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
