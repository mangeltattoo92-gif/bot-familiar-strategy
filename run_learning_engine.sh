#!/bin/bash
# Corre el motor de aprendizaje despues del cierre de mercado (analiza
# el dia completo ya cerrado) y le avisa al usuario los hallazgos por
# push. 2026-09-15, a pedido del usuario ("un sistema que aprenda y
# memorice... y ajuste sus propias reglas con el tiempo" -- ver
# learning_engine.py para el limite de seguridad: solo analiza y
# reporta, nunca edita codigo de estrategia solo).
set -a
source /etc/bot-familiar/claude-code.env 2>/dev/null
set +a

cd /opt/bot-familiar || exit 1
LOG_FILE="/opt/bot-familiar/learning_engine.log"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo "=== $TIMESTAMP ===" >> "$LOG_FILE"

OUTPUT=$(./venv/bin/python3 learning_engine.py 2>&1)
echo "$OUTPUT" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

claude -p "Mandale al usuario una notificacion push (herramienta PushNotification) con este resumen EXACTO tal cual, sin cambiar nada: 'Motor de aprendizaje -- $OUTPUT'. No hagas nada mas, no investigues nada mas, y sobre todo NO edites ningun archivo de codigo." \
  --allowedTools "PushNotification" \
  --permission-prompts none \
  --output-format text >> "$LOG_FILE" 2>&1
