#!/bin/bash
# Corre el diagnostico del piloto de dinero real (SOLO lectura, nunca
# coloca ordenes reales -- ver pilot_prompt.txt). Pensado para cron.
#
# Lock con flock: si una corrida anterior todavia esta activa (un ciclo
# tarda ~2-3 min via agente de Claude, mas lento que el bucle liviano
# de Python que ya usa el bot), esta corrida nueva se salta en vez de
# pisarla -- evita 2 corridas simultaneas tocando la misma cuenta real.
LOCK_FILE="/opt/bot-familiar-real-pilot/pilot.lock"
exec 200>"$LOCK_FILE"
flock -n 200 || { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) -- corrida anterior todavia activa, se salta este ciclo" >> /opt/bot-familiar-real-pilot/pilot.log; exit 0; }

set -a
source /etc/bot-familiar/claude-code.env
set +a

cd /opt/bot-familiar-real-pilot || exit 1

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
LOG_FILE="/opt/bot-familiar-real-pilot/pilot.log"

echo "=== $TIMESTAMP ===" >> "$LOG_FILE"

claude -p "$(cat pilot_prompt.txt)" \
  --allowedTools "Bash Read Write Edit Glob Grep mcp__robinhood-trading__get_accounts mcp__robinhood-trading__get_portfolio mcp__robinhood-trading__get_option_chains mcp__robinhood-trading__get_option_quotes mcp__robinhood-trading__get_option_instruments mcp__robinhood-trading__get_equity_quotes" \
  --permission-prompts none \
  --output-format text \
  >> "$LOG_FILE" 2>&1

echo "" >> "$LOG_FILE"
