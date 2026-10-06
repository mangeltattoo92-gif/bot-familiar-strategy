#!/bin/bash
# Bucle del piloto de dinero real: corre run_pilot.sh cada 15 segundos en
# horario de mercado (lunes a viernes, 13:00 a 19:59 UTC). Fuera de ese
# horario duerme 5 minutos y vuelve a mirar. Lo levanta systemd.
cd /opt/bot-familiar-real-pilot || exit 1
while true; do
  DOW=$(date -u +%u)
  HOUR=$((10#$(date -u +%H)))
  if [ "$DOW" -le 5 ] && [ "$HOUR" -ge 13 ] && [ "$HOUR" -le 19 ]; then
    bash run_pilot.sh
    sleep 15
  else
    sleep 300
  fi
done
