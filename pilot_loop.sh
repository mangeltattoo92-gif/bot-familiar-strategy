#!/bin/bash
# Bucle del piloto de dinero real: corre run_pilot.sh cada 15 segundos en
# horario de mercado (lunes a viernes, 13:00 a 19:59 UTC). Fuera de ese
# horario duerme 5 minutos y vuelve a mirar. Lo levanta systemd.
cd /opt/bot-familiar-real-pilot || exit 1
LAST_PULL=""
while true; do
  DOW=$(date -u +%u)
  HOUR=$((10#$(date -u +%H)))
  if [ "$DOW" -le 5 ] && [ "$HOUR" -ge 13 ] && [ "$HOUR" -le 19 ]; then
    # Un git pull por bloque de 5 minutos, nunca en cada ciclo: pulls
    # simultaneos chocan con el mismo repositorio.
    BLOQUE=$(date -u +%Y%m%d%H)$(( 10#$(date -u +%M) / 5 ))
    if [ "$BLOQUE" != "$LAST_PULL" ]; then
      git pull -q origin main >> /opt/bot-familiar-real-pilot/pilot.log 2>&1 || echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) -- git pull fallo" >> /opt/bot-familiar-real-pilot/pilot.log
      LAST_PULL="$BLOQUE"
    fi
    bash run_pilot.sh
    sleep 15
  else
    sleep 300
  fi
done
