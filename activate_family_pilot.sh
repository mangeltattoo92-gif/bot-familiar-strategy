#!/bin/bash
# Paso final: agrega el cron de este familiar SOLO despues de confirmar
# que su token y su conector de Robinhood ya estan guardados de verdad
# (no agrega nada a ciegas -- evita un cron corriendo sin credenciales
# real generando ruido/errores cada 5 min).
#
# Uso: ./activate_family_pilot.sh <username>
set -euo pipefail

USERNAME="${1:?Uso: activate_family_pilot.sh <username>}"
BASE="/opt/bot-familiar-family-pilots/${USERNAME}"
ENV_FILE="/etc/bot-familiar/family/${USERNAME}/claude-code.env"

if [ ! -d "$BASE" ]; then
    echo "No existe $BASE -- corre setup_family_pilot.sh ${USERNAME} primero."
    exit 1
fi

if ! grep -q "^CLAUDE_CODE_OAUTH_TOKEN=sk-ant-" "$ENV_FILE" 2>/dev/null; then
    echo "ERROR: $ENV_FILE todavia no tiene un token real guardado (linea CLAUDE_CODE_OAUTH_TOKEN=sk-ant-...)."
    echo "Generalo con ${USERNAME} presente: HOME=$BASE/claude-home claude setup-token"
    exit 1
fi

set -a
source "$ENV_FILE"
set +a
export HOME="$BASE/claude-home"

if ! claude auth status 2>&1 | grep -q '"loggedIn": true'; then
    echo "ERROR: el token guardado no autentica correctamente. Revisalo."
    exit 1
fi

if ! claude mcp list 2>&1 | grep -q "robinhood-trading.*Connected"; then
    echo "ERROR: el conector de Robinhood de ${USERNAME} todavia no esta conectado."
    echo "Conectalo con ${USERNAME} presente: HOME=$BASE/claude-home claude mcp add --transport http --scope user robinhood-trading https://agent.robinhood.com/mcp/trading"
    exit 1
fi

CRON_LINE="2-59/5 13-19 * * 1-5 $BASE/run_pilot.sh"
( crontab -l 2>/dev/null | grep -vF "$BASE/run_pilot.sh" ; echo "$CRON_LINE" ) | crontab -

echo "Listo -- cron activado para ${USERNAME}, corre cada 5 min en horario de mercado."
echo "Linea agregada: $CRON_LINE"
