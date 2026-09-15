#!/bin/bash
# Corre el sesgo diario de mercado (alcista/bajista/lateral por ticker,
# segun tendencia diaria confirmada) justo antes de la apertura, y le
# avisa al usuario un resumen por push. 2026-09-15, a pedido del
# usuario ("vas a tener en cuenta si es alcista o bajista en cada etf o
# compañia... sepas los riesgos a la hora de entrar y salir"). Ver
# daily_market_bias.py para el detalle.
set -a
source /etc/bot-familiar/claude-code.env 2>/dev/null
set +a

cd /opt/bot-familiar || exit 1
LOG_FILE="/opt/bot-familiar/daily_market_bias.log"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo "=== $TIMESTAMP ===" >> "$LOG_FILE"

OUTPUT=$(./venv/bin/python3 daily_market_bias.py 2>&1)
echo "$OUTPUT" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

claude -p "Mandale al usuario una notificacion push (herramienta PushNotification) con este resumen EXACTO tal cual, sin cambiar nada: 'Sesgo diario listo -- $OUTPUT'. No hagas nada mas, no investigues nada mas." \
  --allowedTools "PushNotification" \
  --permission-prompts none \
  --output-format text >> "$LOG_FILE" 2>&1
