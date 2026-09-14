#!/bin/bash
# Censor de salud FUNCIONAL de bot-familiar produccion (2026-09-14, a
# pedido del usuario tras 2 incidentes reales el mismo dia: el panel
# web quedo sin poder abrirse por agotamiento de file descriptors, y el
# piloto quedo muerto ~5h por perder el bit de ejecucion -- ninguno de
# los dos fue detectado por watchdog.py, que solo vigila "esta vivo el
# proceso", no "esta funcionando de verdad").
#
# Este script es COMPLEMENTARIO a watchdog.py (que sigue reiniciando
# procesos colgados/caidos) -- se enfoca en lo que watchdog.py NO
# cubre: salud HTTP real del panel, y file descriptors de gunicorn
# como alerta temprana antes de que se repita el agotamiento total.
set -a
source /etc/bot-familiar/claude-code.env 2>/dev/null
set +a

HEALTH_LOG="/opt/bot-familiar/deep_health.log"
PROBLEMS=()
REPAIRS=()

# 1. El panel web responde de verdad? (no solo "el proceso esta vivo"
#    -- gunicorn puede seguir corriendo con file descriptors agotados
#    y devolver 500 en todo, como paso hoy).
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://127.0.0.1:8000/login 2>/dev/null)
if [ "$HTTP_CODE" != "200" ]; then
    systemctl restart bot-familiar-web.service
    sleep 3
    HTTP_CODE_RETRY=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://127.0.0.1:8000/login 2>/dev/null)
    if [ "$HTTP_CODE_RETRY" = "200" ]; then
        REPAIRS+=("El panel web devolvia HTTP $HTTP_CODE en vez de 200 -- reiniciado, ahora responde bien.")
    else
        PROBLEMS+=("El panel web devuelve HTTP $HTTP_CODE (reintentado tras reiniciar: $HTTP_CODE_RETRY) -- necesita revision manual.")
    fi
fi

# 2. File descriptors de gunicorn -- alerta temprana ANTES de llegar al
#    limite (65536 desde el fix de hoy). Reinicio preventivo a partir
#    del 50% de uso: mas barato reiniciar un worker liviano ahora que
#    esperar a que vuelva a agotarse del todo.
for PID in $(pgrep -f "gunicorn.*webapp.app:app" 2>/dev/null); do
    FD_COUNT=$(ls "/proc/$PID/fd" 2>/dev/null | wc -l)
    FD_LIMIT=$(awk '/Max open files/ {print $4}' "/proc/$PID/limits" 2>/dev/null)
    if [ -n "$FD_COUNT" ] && [ -n "$FD_LIMIT" ] && [ "$FD_LIMIT" -gt 0 ]; then
        FD_PCT=$((FD_COUNT * 100 / FD_LIMIT))
        if [ "$FD_PCT" -ge 50 ]; then
            systemctl restart bot-familiar-web.service
            REPAIRS+=("Worker de gunicorn (PID $PID) tenia $FD_COUNT/$FD_LIMIT file descriptors (${FD_PCT}%) -- reiniciado preventivamente antes de agotarse.")
            break  # un restart ya recicla todos los workers, no hace falta seguir chequeando
        fi
    fi
done

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
if [ ${#REPAIRS[@]} -gt 0 ]; then
    echo "$TIMESTAMP -- ${#REPAIRS[@]} reparacion(es) automatica(s):" >> "$HEALTH_LOG"
    for r in "${REPAIRS[@]}"; do
        echo "  - $r" >> "$HEALTH_LOG"
    done
fi
if [ ${#PROBLEMS[@]} -eq 0 ] && [ ${#REPAIRS[@]} -eq 0 ]; then
    echo "$TIMESTAMP -- OK, sin problemas." >> "$HEALTH_LOG"
elif [ ${#PROBLEMS[@]} -gt 0 ]; then
    echo "$TIMESTAMP -- ${#PROBLEMS[@]} problema(s):" >> "$HEALTH_LOG"
    for p in "${PROBLEMS[@]}"; do
        echo "  - $p" >> "$HEALTH_LOG"
    done
    PROBLEM_TEXT=$(printf '%s\n' "${PROBLEMS[@]}")
    claude -p "Mandale al usuario una notificacion push con este resumen EXACTO (no lo cambies, no agregues nada): 'Bot Familiar produccion -- problema detectado: $PROBLEM_TEXT'. Usa la herramienta PushNotification. No hagas nada mas." \
      --allowedTools "PushNotification" \
      --permission-prompts none \
      --output-format text >> "$HEALTH_LOG" 2>&1
fi
if [ ${#REPAIRS[@]} -gt 0 ]; then
    REPAIR_TEXT=$(printf '%s\n' "${REPAIRS[@]}")
    claude -p "Mandale al usuario una notificacion push con este resumen EXACTO (no lo cambies, no agregues nada): 'Bot Familiar produccion -- reparacion automatica: $REPAIR_TEXT'. Usa la herramienta PushNotification. No hagas nada mas." \
      --allowedTools "PushNotification" \
      --permission-prompts none \
      --output-format text >> "$HEALTH_LOG" 2>&1
fi
