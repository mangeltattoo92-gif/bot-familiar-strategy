# Instalar tu propia copia (para cada persona)

Cada persona corre su PROPIA copia en su PROPIA computadora -- cuenta,
balance y posiciones separadas de las demas. No comparte nada con nadie.

**Solo Windows por ahora.** No necesitás cuenta de Robinhood: el bot
opera solo con datos gratuitos de yfinance.

## Instalacion (una sola vez)

1. Instalá [Python 3.11+](https://www.python.org/downloads/) si no lo tenés.
2. Copiá esta carpeta completa a tu computadora.
3. Abrí una terminal en esa carpeta y corré:
   ```
   python setup.py
   ```
   Te va a pedir: cuánto capital simulado querés empezar, y un
   usuario/contraseña para tu panel.

## Prender el bot

```
python start_bot.py
```

Eso prende los 5 procesos (panel web, monitor de posiciones, vigía de
señales, motor de entradas automático, y el censor que repara solo si
algo se cae). Después abrí **http://127.0.0.1:5000** en tu navegador.

Se puede correr `python start_bot.py` de nuevo sin miedo -- si el bot ya
está prendido, no duplica nada (cada proceso tiene su propio candado).

## Revisar que todo esté bien

```
python health_check.py
```

## Apagar

Cerrá los procesos `python.exe` desde el Administrador de tareas de
Windows, o preguntale a Claude Code que los pare por vos.

## Qué hace y qué NO hace

- Simula operaciones de opciones (day trading con Bandas de Bollinger)
  con dinero **ficticio**. Nunca coloca una orden real.
- Corre solo, sin que nadie lo supervise -- escanea, compra y vende según
  sus propias reglas cada pocos minutos.
- El respaldo automático (a tu Escritorio) y el `CLAUDE.md`/`INCIDENTS.md`
  con toda la historia del proyecto vienen incluidos, para que si algo se
  rompe se pueda diagnosticar rápido.
