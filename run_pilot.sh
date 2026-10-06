#!/bin/bash
# Piloto de dinero real, corre cada minuto en horario de mercado (cron).
#
# El ciclo deterministico (senales y salidas del papel, sin tokens) corre
# siempre. El agente de Claude solo se llama cuando hay una compra o una
# venta que ejecutar, o cada 5 minutos para refrescar el panel.
#
# Lock con flock: si una corrida anterior todavia tiene el agente activo,
# esta se salta en vez de pisarla -- evita dos corridas tocando la misma
# cuenta real al mismo tiempo.
LOCK_FILE="/opt/bot-familiar-real-pilot/pilot.lock"
LOG_FILE="/opt/bot-familiar-real-pilot/pilot.log"
exec 200>"$LOCK_FILE"
flock -n 200 || { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) -- corrida anterior todavia activa, se salta" >> "$LOG_FILE"; exit 0; }

set -a
source /etc/bot-familiar/claude-code.env
set +a

cd /opt/bot-familiar-real-pilot || exit 1

git pull -q origin main >> "$LOG_FILE" 2>&1 || echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) -- git pull fallo, sigo con el codigo local" >> "$LOG_FILE"

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
START_EPOCH=$(date -u +%s)
./venv/bin/python3 pilot_cycle.py > cycle_result.json 2>> "$LOG_FILE" || { echo "$TIMESTAMP -- pilot_cycle fallo" >> "$LOG_FILE"; exit 1; }
DURATION=$(( $(date -u +%s) - START_EPOCH ))
echo "$TIMESTAMP -- ciclo de python: ${DURATION}s" >> "$LOG_FILE"
if [ "$DURATION" -gt 15 ]; then echo "$TIMESTAMP -- AVISO: ciclo de ${DURATION}s, supera los 15 segundos" >> "$LOG_FILE"; fi

NEEDS_AGENT=$(./venv/bin/python3 -c "import json; d=json.load(open('cycle_result.json')); print(1 if (d.get('new_signal') or d.get('closed_positions')) else 0)")
MINUTE=$((10#$(date -u +%M)))
if [ "$NEEDS_AGENT" != "1" ] && [ $((MINUTE % 5)) -ne 0 ]; then
  exit 0
fi

if [ "$NEEDS_AGENT" = "1" ]; then MOTIVO="accion"; else MOTIVO="refresco"; fi
echo "=== $TIMESTAMP (agente: $MOTIVO) ===" >> "$LOG_FILE"
if [ "$NEEDS_AGENT" = "1" ]; then date -u +%s > last_action.txt; fi

claude -p "$(cat pilot_prompt.txt)" \
  --allowedTools "Bash Read Write Edit Glob Grep mcp__robinhood-trading__get_accounts mcp__robinhood-trading__get_portfolio mcp__robinhood-trading__get_option_chains mcp__robinhood-trading__get_option_quotes mcp__robinhood-trading__get_option_instruments mcp__robinhood-trading__get_equity_quotes mcp__robinhood-trading__place_option_order mcp__robinhood-trading__get_option_orders mcp__robinhood-trading__get_option_positions" \
  --permission-prompts none \
  --output-format text \
  >> "$LOG_FILE" 2>&1

echo "" >> "$LOG_FILE"
