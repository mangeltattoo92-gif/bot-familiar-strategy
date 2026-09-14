#!/bin/bash
# Autopsia/peritaje profundo del sistema completo (2026-09-14, a pedido
# explicito del usuario tras 2 incidentes reales el mismo dia que los
# chequeos automaticos mas livianos no habrian detectado a tiempo por
# si solos). 4 veces por dia en horario de mercado, lunes a viernes.
#
# Diferencia con pilot_health_check.sh / deep_health_check.sh (que
# siguen corriendo, esto NO los reemplaza): esos son chequeos FIJOS y
# rapidos (cada 10-15 min). Esta autopsia es mas profunda, corre un
# agente de IA que puede leer logs completos, cruzar informacion entre
# procesos, y detectar cosas raras que un script fijo no anticipa --
# pero solo tiene permiso de corregir con acciones MECANICAS y seguras
# (reiniciar un servicio, arreglar un permiso, actualizar codigo con
# git pull). Nunca edita codigo de estrategia a ciegas sin supervision.
set -a
source /etc/bot-familiar/claude-code.env 2>/dev/null
set +a

LOG_FILE="/opt/bot-familiar/deep_autopsy.log"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo "=== $TIMESTAMP ===" >> "$LOG_FILE"

claude -p "Hace una autopsia/peritaje PROFUNDO del sistema completo de Bot Familiar (produccion en este servidor + el piloto de dinero real). Objetivo: encontrar cualquier cosa que este mal o a punto de romperse ANTES de que se convierta en un apagon como los 2 que ya pasaron hoy (permiso de ejecucion perdido en un git pull, y file descriptors agotados en gunicorn).

Revisa, en orden:
1. Estado de los servicios: \`systemctl status bot-familiar-web bot-familiar-entries bot-familiar-proximity bot-familiar-watchdog --no-pager\`. Todos deben estar 'active (running)', sin reinicios sospechosos recientes.
2. Salud HTTP real del panel: \`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/login\` debe dar 200.
3. Permisos de ejecucion de TODOS los scripts .sh en /opt/bot-familiar y /opt/bot-familiar-real-pilot (\`find /opt/bot-familiar /opt/bot-familiar-real-pilot -name '*.sh' -not -perm -u+x\` -- si esto devuelve algo, son scripts SIN permiso de ejecucion, hay que arreglarlos con chmod +x).
4. File descriptors de gunicorn (\`ls /proc/<pid>/fd | wc -l\` contra el limite en \`/proc/<pid>/limits\` para cada worker de gunicorn) -- alerta si algun worker supera 40%.
5. Ultimas 30 lineas de /opt/bot-familiar/multi_user_entry.log, /opt/bot-familiar-real-pilot/pilot.log, /opt/bot-familiar-real-pilot/health.log, /opt/bot-familiar/deep_health.log -- buscar tracebacks, errores repetidos, o silencios sospechosos (deberian tener actividad reciente en horario de mercado).
6. Estado de git en /opt/bot-familiar-real-pilot (\`git status\`, \`git log -1\`) -- si esta atras de origin/main, correr git pull. Si hay cambios locales sin commitear que no deberian estar ahi, reportalo, NO los descartes sin avisar.
7. Espacio en disco (\`df -h /\`) -- alerta si supera 85% de uso.
8. Cuentas activas en bot-familiar (via python: \`from webapp.auth import list_all_users; from paper_trading.engine import get_settings, get_status\` sobre cada cuenta activa) -- chequeo rapido de que bot_enabled este en el estado esperado y no haya un circuit breaker disparado sin que el usuario lo sepa.

Para cada problema que encuentres, clasificalo en DOS categorias:
- MECANICO Y SEGURO de arreglar vos mismo AHORA (permiso de ejecucion faltante -> chmod +x; servicio caido o gunicorn con fd alto -> systemctl restart; repo atras de origin -> git pull): arreglalo y anotalo como reparacion.
- CUALQUIER OTRA COSA (algo que requeriria editar codigo de estrategia, cambiar una regla de trading, o que no entendes del todo bien): NO lo toques, solo reportalo con el maximo detalle posible para que el usuario o yo lo revisemos despues. Nunca edites paper_trading/*.py, multi_user_entry.py, auto_entry.py, ni ningun archivo de estrategia -- eso esta explicitamente prohibido en esta autopsia automatica.

Al final, mandale al usuario UNA notificacion push (herramienta PushNotification) con un resumen corto en español: que revisaste, que encontraste (si algo), que reparaste solo (si algo), y que queda pendiente de revision humana (si algo). Si todo esta perfecto, un mensaje breve confirmandolo alcanza -- no inventes problemas que no existen." \
  --allowedTools "Bash Read Grep Glob PushNotification" \
  --permission-prompts none \
  --output-format text >> "$LOG_FILE" 2>&1

echo "" >> "$LOG_FILE"
