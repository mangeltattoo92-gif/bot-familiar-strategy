#!/bin/bash
# Censor de salud del piloto de dinero real -- version liviana de
# watchdog.py para este piloto basado en cron (no procesos persistentes,
# asi que no hay nada que "reiniciar"; solo detecta y avisa problemas).
set -a
source /etc/bot-familiar/claude-code.env
set +a

cd /opt/bot-familiar-real-pilot || exit 1

HEALTH_LOG="/opt/bot-familiar-real-pilot/health.log"
PROBLEMS=()
REPAIRS=()

# 0. Auto-reparacion del bit de ejecucion (2026-09-14, bug real: un
#    commit hecho desde Windows no preserva el bit +x de Linux -- el
#    `git pull` del propio ciclo del piloto piso los scripts locales
#    con la version del repo SIN +x, y el cron los invocaba directo
#    (no via `bash script.sh`) asi que fallaron en silencio durante
#    ~5 horas en horario de mercado sin dejar ningun rastro. Con las
#    entradas del cron ahora usando `bash script.sh` explicito esto ya
#    no puede volver a tumbar el cron -- pero se repara aca igual, de
#    resguardo, por si alguna vez se invoca directo de nuevo.
for script in run_pilot.sh pilot_health_check.sh run_proximity.sh pilot_cycle.py; do
    if [ -f "$script" ] && [ ! -x "$script" ]; then
        chmod +x "$script"
        REPAIRS+=("$script no tenia permiso de ejecucion -- reparado solo (chmod +x).")
    fi
done

# 1. El log de entradas creciendo? SOLO durante horario de mercado
#    (13-19 UTC, lun-vie) -- fuera de ese horario el cron ni corre, asi
#    que no actualizarse ahi es normal, no un problema (bug real
#    encontrado en la primera version de este script: marcaba "problema"
#    aunque el mercado estuviera cerrado).
HOUR_UTC=$(date -u +%H | sed 's/^0//')
DOW_UTC=$(date -u +%u)  # 1=lunes .. 7=domingo
MARKET_WINDOW=false
if [ "$DOW_UTC" -ge 1 ] && [ "$DOW_UTC" -le 5 ] && [ "$HOUR_UTC" -ge 13 ] && [ "$HOUR_UTC" -le 19 ]; then
    MARKET_WINDOW=true
fi

if [ "$MARKET_WINDOW" = true ]; then
    if [ -f pilot.log ]; then
        LAST_MOD=$(($(date +%s) - $(stat -c %Y pilot.log)))
        if [ "$LAST_MOD" -gt 600 ]; then
            PROBLEMS+=("pilot.log no se actualiza hace $((LAST_MOD / 60)) min (estamos en horario de mercado) -- el cron de entradas puede estar fallando.")
        fi
    else
        PROBLEMS+=("pilot.log no existe todavia, y ya estamos en horario de mercado.")
    fi
fi

# 2. Errores tecnicos REALES en las ultimas 20 lineas del log de
#    entradas -- patron mas especifico que un simple "error" en
#    cualquier lado (bug real encontrado: "error_log" es un nombre de
#    tabla legitimo que menciona el propio reporte de diagnostico, no
#    un error de verdad).
if [ -f pilot.log ] && tail -20 pilot.log | grep -qE "Traceback|ERROR:|^Error:|Exception:|no se pudo|fall[oó] (la|el)"; then
    PROBLEMS+=("Se detecto texto de error real en las ultimas lineas de pilot.log -- revisar manualmente.")
fi

# 3. Conector de Robinhood sigue autenticado? Con 1 reintento antes de
#    declarar problema -- un chequeo de salud de red puede fallar por
#    un hipo transitorio, no hace falta alarmar por eso solo.
robinhood_ok() {
    claude mcp list 2>&1 | grep -q "robinhood-trading.*Connected"
}
if ! robinhood_ok; then
    sleep 5
    if ! robinhood_ok; then
        PROBLEMS+=("El conector de Robinhood no aparece conectado (confirmado 2 veces) -- puede haber vencido la autorizacion.")
    fi
fi

# 4. Token de Claude Code sigue valido?
if ! claude auth status 2>&1 | grep -q '"loggedIn": true'; then
    PROBLEMS+=("Claude Code ya NO esta autenticado en el servidor -- el token puede haber vencido o fue revocado.")
fi

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
if [ ${#REPAIRS[@]} -gt 0 ]; then
    echo "$TIMESTAMP -- ${#REPAIRS[@]} reparacion(es) automatica(s):" >> "$HEALTH_LOG"
    for r in "${REPAIRS[@]}"; do
        echo "  - $r" >> "$HEALTH_LOG"
    done
fi
if [ ${#PROBLEMS[@]} -eq 0 ] && [ ${#REPAIRS[@]} -eq 0 ]; then
    echo "$TIMESTAMP -- OK, sin problemas." >> "$HEALTH_LOG"
elif [ ${#PROBLEMS[@]} -eq 0 ]; then
    echo "$TIMESTAMP -- OK, sin problemas (con reparacion automatica arriba)." >> "$HEALTH_LOG"
else
    echo "$TIMESTAMP -- ${#PROBLEMS[@]} problema(s):" >> "$HEALTH_LOG"
    for p in "${PROBLEMS[@]}"; do
        echo "  - $p" >> "$HEALTH_LOG"
    done
    PROBLEM_TEXT=$(printf '%s\n' "${PROBLEMS[@]}")
    claude -p "Mandale al usuario una notificacion push con este resumen EXACTO (no lo cambies, no agregues nada): 'Piloto de dinero real -- problema detectado: $PROBLEM_TEXT'. Usa la herramienta PushNotification. No hagas nada mas." \
      --allowedTools "PushNotification" \
      --permission-prompts none \
      --output-format text >> "$HEALTH_LOG" 2>&1
fi
if [ ${#REPAIRS[@]} -gt 0 ]; then
    REPAIR_TEXT=$(printf '%s\n' "${REPAIRS[@]}")
    claude -p "Mandale al usuario una notificacion push con este resumen EXACTO (no lo cambies, no agregues nada): 'Piloto de dinero real -- reparacion automatica: $REPAIR_TEXT'. Usa la herramienta PushNotification. No hagas nada mas." \
      --allowedTools "PushNotification" \
      --permission-prompts none \
      --output-format text >> "$HEALTH_LOG" 2>&1
fi
