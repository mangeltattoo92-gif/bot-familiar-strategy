#!/bin/bash
# Vigia de casi-senales para el piloto de dinero real -- SOLO informativo,
# nunca opera ni coloca ordenes. Corre directo en Python (no necesita un
# agente de Claude), mucho mas liviano que run_pilot.sh.
cd /opt/bot-familiar-real-pilot || exit 1
./venv/bin/python3 proximity_watch.py --once >> /opt/bot-familiar-real-pilot/proximity.log 2>&1
