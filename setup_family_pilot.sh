#!/bin/bash
# Crea la infraestructura para el piloto de dinero real de UN familiar,
# reusando exactamente el mismo patron que ya funciona para el piloto del
# admin (/opt/bot-familiar-real-pilot) -- misma logica, mismo repo
# compartido, mismo cron cada 5 min -- pero aislado por persona.
#
# Aislamiento clave: cada familiar tiene su PROPIO $HOME virtual
# (claude-home/) para que su token de Claude Code y su conector MCP de
# Robinhood vivan en ~/.claude.json DENTRO de esa carpeta, sin pisar ni
# ver el del admin ni el de otros familiares (confirmado 2026-09-14:
# mcpServers se guarda en el nivel superior de ~/.claude.json, atado al
# $HOME de quien corre `claude`, no al token activo).
#
# Este script SOLO arma la infraestructura -- NO activa nada real
# (no registra cron, no pide token). El ultimo paso (que el familiar
# genere su propio token y autorice su propio Robinhood) se hace aparte,
# a mano, con esa persona presente -- nunca copiando credenciales de
# otro. Ver activate_family_pilot.sh para el paso final.
#
# Uso: ./setup_family_pilot.sh <username>
set -euo pipefail

USERNAME="${1:?Uso: setup_family_pilot.sh <username>}"
BASE="/opt/bot-familiar-family-pilots/${USERNAME}"
REPO_URL="https://github.com/mangeltattoo92-gif/bot-familiar-strategy.git"

if [ -d "$BASE" ]; then
    echo "Ya existe $BASE -- no se pisa nada. Borralo a mano primero si queres empezar de cero."
    exit 1
fi

echo "Creando infraestructura para '$USERNAME' en $BASE ..."
mkdir -p "$BASE/claude-home"
git clone "$REPO_URL" "$BASE/repo"

cd "$BASE/repo"
python3 -m venv venv
./venv/bin/pip install --quiet -r requirements.txt --ignore-installed blinker

mkdir -p "$BASE/repo/data"
./venv/bin/python3 paper_trade.py init --balance 10000

# Personaliza el prompt del piloto con el nombre de esta persona -- misma
# estructura que pilot_prompt.txt del admin, con REGLA ABSOLUTA identica
# (nunca ordenes reales, solo lectura de Robinhood para verificar precio).
sed "s/PLACEHOLDER_USERNAME/${USERNAME}/g" > "$BASE/repo/pilot_prompt.txt" <<'PROMPT_EOF'
Sos parte de "Bot Familiar". Esta corrida es el PILOTO de dinero real de PLACEHOLDER_USERNAME, con el conector de Robinhood de PLACEHOLDER_USERNAME (su cuenta Agentic real) conectado SOLO para verificar precios/griegas reales -- nunca para ejecutar.

REGLA ABSOLUTA: NUNCA llames a place_option_order, place_equity_order, ni ninguna herramienta de Robinhood que coloque una orden real. Las unicas herramientas de Robinhood permitidas son de solo lectura: get_accounts, get_portfolio, get_option_chains, get_option_quotes, get_equity_quotes. Todo lo que se registra va a la CUENTA DE PAPEL local (paper_trading.db, dinero simulado), nunca a Robinhood real.

Estas en /opt/bot-familiar-family-pilots/PLACEHOLDER_USERNAME/repo (venv en ./venv). Primero: `git pull`.

## Paso 1 -- Correr el ciclo deterministico

Corre: `./venv/bin/python3 pilot_cycle.py`

Este script ya hace todo el trabajo mecanico (cerrar posiciones que correspondan, chequear circuit breaker, escanear las tickers, calcular tamano por riesgo) y te devuelve un JSON. Leelo con cuidado, tiene 4 formas posibles:
- `closed_positions_count` > 0: ya se cerraron posiciones solas -- solo reportalo.
- `circuit_breaker_active: true`: no se buscan entradas este ciclo, reportalo.
- `new_signal: null` con algo en `notes`: sin senal ejecutable este ciclo -- reportalo, termina.
- `new_signal` con datos: hay un candidato de ENTRADA listo para verificar y registrar -- segui al Paso 2.

## Paso 2 -- Verificar con Robinhood real y registrar la entrada (SOLO si hay new_signal)

1. Con `mcp__robinhood-trading__get_option_chains`, busca el mismo contrato exacto: mismo ticker, strike, vencimiento, tipo que viene en `new_signal`.
2. Compara el ask real de Robinhood contra el de `new_signal.ask` -- si difieren mucho, usa el de Robinhood, decilo en el reporte.
3. Anota el delta y theta reales de Robinhood para ese contrato.
4. Llama a `record_trade()` de `paper_trading.engine` (script corto Python, igual que pilot_cycle.py) con el precio ASK real verificado.

## Paso 3 -- Resumen final

Reporta en espanol, corto y claro: que se cerro, que se abrio, estado del circuit breaker, notas relevantes. Si no paso nada, una linea alcanza.

Ciclo automatico sin supervision humana en el momento.
PROMPT_EOF

# pilot_cycle.py y los scripts de cron son IDENTICOS al patron del admin
# -- se copian del repo compartido, no se reescriben aca.
cp "$BASE/repo/pilot_cycle.py" "$BASE/repo/pilot_cycle.py" 2>/dev/null || true

cat > "$BASE/run_pilot.sh" <<EOF
#!/bin/bash
# Corre el diagnostico del piloto de dinero real de ${USERNAME} (SOLO
# lectura, nunca coloca ordenes reales -- ver pilot_prompt.txt).
LOCK_FILE="$BASE/pilot.lock"
exec 200>"\$LOCK_FILE"
flock -n 200 || { echo "\$(date -u +%Y-%m-%dT%H:%M:%SZ) -- corrida anterior todavia activa, se salta" >> "$BASE/pilot.log"; exit 0; }

export HOME="$BASE/claude-home"
set -a
source "/etc/bot-familiar/family/${USERNAME}/claude-code.env"
set +a

cd "$BASE/repo" || exit 1
TIMESTAMP=\$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo "=== \$TIMESTAMP ===" >> "$BASE/pilot.log"

claude -p "\$(cat pilot_prompt.txt)" \\
  --allowedTools "Bash Read Write Edit Glob Grep mcp__robinhood-trading__get_accounts mcp__robinhood-trading__get_portfolio mcp__robinhood-trading__get_option_chains mcp__robinhood-trading__get_option_quotes mcp__robinhood-trading__get_equity_quotes" \\
  --permission-prompts none \\
  --output-format text \\
  >> "$BASE/pilot.log" 2>&1
echo "" >> "$BASE/pilot.log"
EOF
chmod +x "$BASE/run_pilot.sh"

mkdir -p "/etc/bot-familiar/family/${USERNAME}"
if [ ! -f "/etc/bot-familiar/family/${USERNAME}/claude-code.env" ]; then
    echo "# CLAUDE_CODE_OAUTH_TOKEN=<pegar aca el token de ${USERNAME}, generado por ${USERNAME} mismo>" \
        > "/etc/bot-familiar/family/${USERNAME}/claude-code.env"
    chmod 600 "/etc/bot-familiar/family/${USERNAME}/claude-code.env"
fi

echo ""
echo "Listo. Infraestructura creada para '${USERNAME}':"
echo "  - Repo + venv + cuenta de papel (\$10,000): $BASE/repo"
echo "  - Config de Claude aislada (su propio \$HOME virtual): $BASE/claude-home"
echo "  - Archivo de token (vacio, a llenar): /etc/bot-familiar/family/${USERNAME}/claude-code.env"
echo "  - Script de corrida: $BASE/run_pilot.sh (NO tiene cron todavia)"
echo ""
echo "Falta (con ${USERNAME} presente, nunca a distancia con su credencial):"
echo "  1. Que ${USERNAME} genere su token: HOME=$BASE/claude-home claude setup-token"
echo "  2. Guardar ese token en /etc/bot-familiar/family/${USERNAME}/claude-code.env"
echo "  3. Que ${USERNAME} conecte su Robinhood: HOME=$BASE/claude-home claude mcp add --transport http --scope user robinhood-trading https://agent.robinhood.com/mcp/trading"
echo "  4. Correr ./activate_family_pilot.sh ${USERNAME} para agregar el cron"
