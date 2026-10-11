#!/bin/bash
# Autopsia/peritaje profundo del sistema completo (2026-09-14, a pedido
# explicito del usuario tras 2 incidentes reales el mismo dia que los
# chequeos automaticos mas livianos no habrian detectado a tiempo por
# si solos). 4 veces por dia en horario de mercado, lunes a viernes.
#
# Diferencia con deep_health_check.sh (que sigue corriendo, esto NO lo
# reemplaza): ese es un chequeo FIJO y rapido (cada 10 min). Esta
# autopsia es mas profunda, corre un agente de IA que puede leer logs
# completos, cruzar informacion entre procesos, y detectar cosas raras
# que un script fijo no anticipa -- pero solo tiene permiso de corregir
# con acciones MECANICAS y seguras (reiniciar un servicio DE LA LISTA
# DEL PASO 1, arreglar un permiso, actualizar codigo con git pull).
# Nunca edita codigo de estrategia a ciegas sin supervision.
#
# 2026-10-07, bug real encontrado: esta autopsia reactivo sola el
# piloto de dinero real de Robinhood (bot-familiar-real-pilot.service)
# porque el servicio seguia 'enabled' aunque se habia DETENIDO a
# proposito -- el agente lo vio "caido" y lo reinicio como reparacion
# mecanica, sin saber que fue una decision deliberada. Arreglado:
# systemctl disable (no solo stop) en los servicios del piloto, crons
# del piloto eliminados, y el prompt de abajo ahora prohibe
# explicitamente tocar nada con 'real-pilot'/'pilot' en el nombre.
set -a
source /etc/bot-familiar/claude-code.env 2>/dev/null
set +a

LOG_FILE="/opt/bot-familiar/deep_autopsy.log"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo "=== $TIMESTAMP ===" >> "$LOG_FILE"

claude -p "Hace una autopsia/peritaje PROFUNDO del sistema completo de Bot Familiar en este servidor. Objetivo: encontrar cualquier cosa que este mal o a punto de romperse ANTES de que se convierta en un apagon como los 2 que ya pasaron el 2026-09-14 (permiso de ejecucion perdido en un git pull, y file descriptors agotados en gunicorn).

REGLA DURA, sin excepcion: el piloto de dinero real (Robinhood, carpeta /opt/bot-familiar-real-pilot) fue decomisionado a proposito el 2026-10-07 -- Robinhood ya no es el broker del proyecto (reemplazado por Tradier). Esa carpeta, sus servicios (bot-familiar-real-pilot, bot-familiar-pilot-exit-watch) y sus crons NO deben aparecer como 'servicio caido' ni repararse de ninguna forma -- NUNCA corras systemctl start/restart/enable sobre nada con 'real-pilot' o 'pilot' en el nombre, ni toques esa carpeta. Si la encontras corriendo de nuevo, es un bug (alguien o algo la reactivo) -- reportalo como problema, no la 'arregles' reiniciandola.

Revisa, en orden:
1. Estado de los servicios: \`systemctl status bot-familiar-web bot-familiar-entries bot-familiar-proximity bot-familiar-watchdog bot-familiar-exit-watch bot-familiar-shadow-stop bot-familiar-tradier-cycle --no-pager\`. Todos deben estar 'active (running)', sin reinicios sospechosos recientes. bot-familiar-exit-watch revisa solo salidas cada 30s (deberia tener actividad reciente en /opt/bot-familiar/data/exit_watch_loop.log en horario de mercado); bot-familiar-shadow-stop es SOLO observacion (nunca cierra nada real), revisa que /opt/bot-familiar/data/shadow_stop_watch.json se siga actualizando; bot-familiar-tradier-cycle corre el ciclo de la(s) cuenta(s) que usan datos de Tradier (ver data/tradier_only_users.json), deberia tener entradas 'para 1 cuenta(s)' recientes en data/multi_user_entry.log durante horario de mercado.
2. Salud HTTP real del panel: \`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/login\` debe dar 200.
3. Permisos de ejecucion de TODOS los scripts .sh en /opt/bot-familiar (\`find /opt/bot-familiar -name '*.sh' -not -perm -u+x\` -- si esto devuelve algo, son scripts SIN permiso de ejecucion, hay que arreglarlos con chmod +x).
4. File descriptors de gunicorn (\`ls /proc/<pid>/fd | wc -l\` contra el limite en \`/proc/<pid>/limits\` para cada worker de gunicorn) -- alerta si algun worker supera 40%.
5. Ultimas 30 lineas de /opt/bot-familiar/multi_user_entry.log, /opt/bot-familiar/tradier_cycle.log, /opt/bot-familiar/deep_health.log -- buscar tracebacks, errores repetidos, o silencios sospechosos (deberian tener actividad reciente en horario de mercado).
6. Espacio en disco (\`df -h /\`) -- alerta si supera 85% de uso.
7. Cuentas activas en bot-familiar (via python: \`from webapp.auth import list_all_users; from paper_trading.engine import get_settings, get_status\` sobre cada cuenta activa) -- chequeo rapido de que bot_enabled este en el estado esperado y no haya un circuit breaker disparado sin que el usuario lo sepa.
8. Motores diarios: despues de las 13:25 UTC en un dia de mercado, /opt/bot-familiar/data/daily_market_bias.json debe tener un 'updated_at' de HOY (revisa tambien daily_market_bias.log por si el cron fallo). Igual para daily_guide.json despues de las 09:00 UTC.

Para cada problema que encuentres, clasificalo en DOS categorias:
- MECANICO Y SEGURO de arreglar vos mismo AHORA (permiso de ejecucion faltante -> chmod +x; servicio de la lista del punto 1 caido o gunicorn con fd alto -> systemctl restart SOLO de esos nombres exactos; repo /opt/bot-familiar atras de origin -> git pull): arreglalo y anotalo como reparacion.
- CUALQUIER OTRA COSA (algo que requeriria editar codigo de estrategia, cambiar una regla de trading, tocar el piloto real decomisionado, o que no entendes del todo bien): NO lo toques, solo reportalo con el maximo detalle posible para que el usuario o yo lo revisemos despues. Nunca edites paper_trading/*.py, multi_user_entry.py, auto_entry.py, ni ningun archivo de estrategia -- eso esta explicitamente prohibido en esta autopsia automatica.

Al final, mandale al usuario UNA notificacion push (herramienta PushNotification) con un resumen corto en español: que revisaste, que encontraste (si algo), que reparaste solo (si algo), y que queda pendiente de revision humana (si algo). Si todo esta perfecto, un mensaje breve confirmandolo alcanza -- no inventes problemas que no existen." \
  --allowedTools "Bash Read Grep Glob PushNotification" \
  --permission-prompts none \
  --output-format text >> "$LOG_FILE" 2>&1

echo "" >> "$LOG_FILE"
