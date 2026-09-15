#!/bin/bash
# Corre la guia diaria completa (los 45 tickers, backtest real de 60
# dias cada uno) antes de la apertura, y le avisa al usuario un resumen
# por push. 2026-09-15, a pedido del usuario ("guia segura en el day
# trading diario"). Ver daily_guide.py para el detalle del veredicto.
set -a
source /etc/bot-familiar/claude-code.env 2>/dev/null
set +a

cd /opt/bot-familiar || exit 1
LOG_FILE="/opt/bot-familiar/daily_guide.log"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo "=== $TIMESTAMP ===" >> "$LOG_FILE"

OUTPUT=$(./venv/bin/python3 daily_guide.py 2>&1)
echo "$OUTPUT" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

claude -p "Mandale al usuario una notificacion push (herramienta PushNotification) con este resumen EXACTO tal cual, sin cambiar nada: 'Guia diaria lista -- $OUTPUT'. No hagas nada mas, no investigues nada mas." \
  --allowedTools "PushNotification" \
  --permission-prompts none \
  --output-format text >> "$LOG_FILE" 2>&1
