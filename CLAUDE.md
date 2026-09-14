# Bot Familiar -- guia operativa / diagnostico rapido

**Rebrandeado a "Bot Familiar" en todo el frontend (2026-09-11)** --
titulo de pestaña, logo (`FAM`, gradiente distinto al `MAPI` de
trading-bot), favicon -- las plantillas venian heredadas del fork y
todas decian "Trading MAPI", causaba confusion real sobre en cual de
los 2 proyectos estaba parado el usuario (mismo puerto tambien
corregido, ver mas abajo -- puerto 5001, no 5000, choca con trading-bot
en la misma PC).

**Panel de administrador con diseño real (2026-09-11, a pedido del
usuario -- "hazlo parecido a ese" refiriendose al mockup visual
aprobado antes):** badge "Solo administrador" en la marca, tarjetas de
resumen arriba (cuentas activas/pendientes/desconectadas, errores hoy),
grilla visual de los 4 procesos de fondo (cada uno con status-pill
activo/caido, incluye `watchdog.py` que NO esta en `MANAGED_PROCESSES`
-- no puede reiniciarse a si mismo, se chequea aparte). Se actualiza
solo cada 30s. Ver `/admin/health` en `webapp/app.py` para la
estructura de datos (`processes`, `stats`, `problems`, `recent_errors`).

**Este proyecto es un FORK de `trading-bot` (2026-09-10), pensado para la
nube (Hetzner) con cuentas separadas por persona (~10 familiares) -- NO es
el mismo proyecto que corre localmente en esta PC.** Ver seccion
"Multi-usuario" mas abajo para lo que falta construir todavia.

**Panel web en el puerto 5001, NO 5000** (bug real encontrado 2026-09-11):
trading-bot ya usa el 5000 en esta misma PC durante desarrollo local --
dos Flask en el mismo puerto no tiran error visible en Windows, el
segundo arranca "silencioso" pero nunca recibe trafico real (todo va al
primero que agarro el puerto). Se detecto porque bot-familiar nunca
logueaba ningun request pese a estar "corriendo". Corregido en
`run_dashboard.py`, `webapp/app.py`, `watchdog.py`, `health_check.py`.

Sistema de paper trading (SIMULADO por ahora, nunca ordenes reales hoy).
Motor autonomo via yfinance (sin necesitar cuenta de Robinhood, mientras
sea simulado). Bandas de Bollinger (20,2) en 15m, 5 estrategias
(squeeze_breakout, gap_fade_apertura, giro_sma20, vwap_cross,
rsi_reversal). Panel web (pensado para celular, PWA) -- URL final depende
del dominio/IP del servidor Hetzner.

## OBJETIVO FUTURO: dinero real, cuenta de Robinhood de cada persona (2026-09-10)

El usuario confirmo que el plan final es conectar la cuenta REAL de
Robinhood de cada uno de los ~10 familiares, no quedarse en simulado para
siempre. Aclaracion importante del usuario (2026-09-10): esto NO es el
usuario principal gestionando la plata de los demas -- es una
HERRAMIENTA que cada persona usa para SU PROPIA cuenta, bajo SU PROPIO
riesgo, con capital separado del de los demas, corriendo la misma
estrategia/parametros. Cada quien conecta y opera la suya, como si cada
uno instalara la misma herramienta por su cuenta -- no hay gestion
centralizada de las cuentas de terceros.

Aun asi, antes de construir la parte de dinero real:

- **Cada persona conecta y autoriza SU PROPIA cuenta** -- eso sigue
  siendo necesario tecnicamente (cada quien pone sus propias credenciales
  de Robinhood), aunque el encuadre ya no sea "gestion de cuentas ajenas".
- **Seguridad de credenciales reales** sigue siendo un nivel totalmente
  distinto a un login de panel de juguete -- va a necesitar manejo serio
  de secretos (no guardar tokens/API keys de corretaje en texto plano).
- Si en algun momento el diseño cambia hacia que el usuario principal
  decida/ejecute operaciones EN LUGAR de cada persona (no solo comparte
  la herramienta), ahi si vale la pena revisar la implicancia regulatoria
  de nuevo -- no aplica al diseño actual descripto por el usuario.
- **Seguridad de credenciales reales** es un nivel totalmente distinto a
  un login de panel de juguete -- va a necesitar manejo serio de secretos
  (no guardar tokens/API keys de corretaje en texto plano en ningun lado).
- **Diseño de hoy:** la logica de señales esta deliberadamente separada de
  la ejecucion (`record_trade()` simulado) para que, llegado el momento,
  la ejecucion real se pueda enchufar ahi sin reescribir la estrategia --
  pero la parte de auth/consentimiento/secretos por-usuario SI hay que
  construirla especificamente para esto, no es gratis.

## Multi-usuario -- ESTADO: pendiente de construir

**DECISION CONFIRMADA (2026-09-10):** el usuario evaluo una alternativa
mas simple -- distribuir el proyecto como descarga (cada familiar lo
instala en su propia PC) activado con un codigo generado desde el panel
de administrador, en vez de un servidor central 24/7 -- y la DESCARTO
explicitamente ("la idea original") al confirmar que entendia la
contrapartida: con la descarga, el bot de cada persona SOLO funciona
mientras su PC este prendida (se pierde esa ventana si la apagan); con
la nube completa (Hetzner, plan original), corre 24/7 para las 10
cuentas sin depender de que nadie prenda nada. Se sigue con el plan de
nube completa de abajo -- no construir la alternativa de descarga+codigo
de activacion salvo que el usuario pida retomarla.

- [x] `singleton_lock.py` portado a POSIX (fcntl) ademas de Windows (msvcrt) -- 2026-09-10
- [x] **`webapp/auth.py` reescrito multi-usuario completo** (2026-09-10) --
  tabla `users` (username, email, password_hash, status
  pending/code_sent/active/rejected, is_admin, timestamps) + tabla
  `activation_codes` (hasheados igual que contraseñas, vencen a las 48h,
  un solo uso). Funciones: `register_user`, `generate_activation_code`,
  `verify_activation_code`, `list_pending_users`, `list_all_users`,
  `reject_user`, `disconnect_user`, `create_admin_user`. **Probado de
  punta a punta via HTTP real (Flask test client), no solo unitario**:
  registro -> admin aprueba (genera codigo) -> login redirige a activar
  -> codigo correcto activa -> login final entra -- las 3 corridas
  dieron el resultado esperado.
- [x] **Rutas web conectadas** (2026-09-10, `webapp/app.py`): `/registro`
  (auto-registro con correo), `/activar` (pantalla de codigo), `/login`
  reescrito para manejar los 4 estados (pending/code_sent/rejected/active),
  `login_required` ahora chequea el estado EN CADA REQUEST (no solo al
  loguearse -- si el admin desconecta a alguien con sesion abierta, se le
  corta en el proximo click). Templates nuevos: `signup.html`,
  `activate.html`.
- [x] **Panel de administrador conectado** (2026-09-10): `/admin`
  (`admin_required`, decorador que exige `is_admin`), `/admin/approve/<id>`,
  `/admin/reject/<id>`, `/admin/disconnect/<id>`, `/admin/reconnect/<id>`.
  Template `admin.html` -- SIN balance/P&L/posiciones de nadie, solo
  usuario/correo/estado, cumpliendo la correccion de alcance de abajo.
  Al aprobar, el codigo generado se muestra en pantalla al admin (NO se
  manda por correo todavia, ver pendiente de email mas abajo).
  `webapp/manage_users.py` reescrito para crear al admin principal
  (`create_admin_user`, ya no el viejo `create_user` de un solo nivel).
- [ ] **Envio de correo real** -- sigue pendiente, se necesita un
  proveedor (SendGrid/Mailgun/SES) y sus credenciales. Mientras tanto el
  codigo de activacion se muestra en el panel de admin para que Miguel
  se lo pase el mismo (SMS/WhatsApp/de palabra) -- funcional pero manual.
- [ ] `watchdog.py` -- reemplazar los chequeos via PowerShell por algo POSIX (psutil o /proc)
- [ ] **Base de datos de TRADING por usuario** -- decision tomada
  2026-09-10: en vez de agregar `user_id` a las tablas compartidas
  (`account`/`positions`/`trades`/`settings` de `paper_trading/engine.py`,
  el plan original de abajo), reusar el patron YA PROBADO en produccion
  hoy mismo en `trading-bot/family_sim.py`: **una base de datos SQLite
  INDEPENDIENTE por usuario** (`data/users/<user_id>/paper_trading.db`),
  reusando `paper_trading/engine.py` tal cual (ya acepta `db_path` en
  todas sus funciones, no hace falta tocarlo) en vez de reescribirlo para
  multi-tenencia por columna. Mas simple, menos riesgoso, y ya funciono
  hoy con 10 cuentas simuladas corriendo asi.

  **[x] HECHO Y PROBADO (2026-09-10):** `webapp/app.py` reescrito
  completo -- `_user_db_path()` (nueva) resuelve
  `data/users/<username>/paper_trading.db` para `session["user"]`, la
  crea sola con `init_db()` (balance inicial $10,000 placeholder, ver
  pendiente abajo) la primera vez que se pide. TODAS las rutas
  (`/api/status`, `/api/positions/close`, `/api/history`,
  `/api/closed-trades`, `/api/account-value-history`, `/api/error-log`,
  `/api/watchlist`, `/api/fast-watchlist`, `/api/bot-status`,
  `/api/journal-summary`, `/api/settings`) ya pasan `db_path` explicito
  -- ninguna usa la DB compartida por defecto. **Probado de punta a
  punta via HTTP real**: 2 usuarios (sofia_p, ana_g) registrados,
  aprobados, activados, logueados -- sofia cambio su
  `contracts_per_trade` a 5 y ana siguio en 1 (default), confirmando
  aislamiento total, cada una con su propio balance de $10,000 en su
  propia base de datos.
  - [ ] **Pendiente:** dejar que cada usuario fije SU capital inicial
    real al crear la cuenta (hoy todos arrancan con el mismo
    $10,000 placeholder) -- agregar el campo al registro o a un primer
    paso de configuracion tras activarse.
  - [ ] **Pendiente:** `market_data.py` (cotizaciones, historial tecnico)
    sigue siendo compartido entre todos -- correcto asi (no es dato de
    cuenta, es dato de mercado publico), pero falta cachear mejor para
    que 10 usuarios pidiendo lo mismo no multipliquen las llamadas a
    yfinance (mismo riesgo de rate-limit que ya paso hoy en trading-bot).
- [x] **`multi_user_entry.py` + `multi_user_entry_loop.py` -- HECHO Y
  PROBADO (2026-09-10):** motor de entradas Y salidas real, adaptado de
  `trading-bot/family_sim.py` (que uso 10 cuentas de prueba hardcodeadas)
  a los usuarios REALES y activos (`webapp.auth.list_all_users()`
  filtrado a `status == 'active'`). Escaneo del watchlist UNA sola vez
  por ciclo para todos los usuarios juntos (no una vez por usuario --
  cumple el requisito de este mismo item). Cada cuenta: su propio
  `db_path` (mismo que usa el panel web), su propio circuit breaker, su
  propio cooldown de reingreso, ventana horaria de giro_sma20, ejecucion
  al ASK real (compra) / BID real (venta). **Bug encontrado y corregido el
  mismo dia:** se habia olvidado portar `too_close_to_open_new_position()`
  (bloquea entradas nuevas a <= 30 min del cierre) -- agregado y
  reverificado en vivo con el mercado abierto real. `multi_user_entry_loop.py` es
  el wrapper de produccion (proceso fresco por ciclo, mismo patron que
  evito el cuelgue silencioso que aparecio hoy en el loop largo de
  `family_sim.py`). **Probado de punta a punta**: usuario de prueba
  activado, `--once` corrido en limpio, cuenta creada con $10,000, escaneo
  del watchlist compartido completado sin errores.

  **Segunda tanda de pruebas (2026-09-10, mismo dia, "simular hasta que
  todo funcione perfecto"):**
  - Posicion de prueba (SPY call) forzada a objetivo alcanzado ->
    `run_exits_for_account` la cerro sola, al BID real ($5.97),
    P&L +497% calculado correcto, posicion desaparecio de abiertas.
  - Circuit breaker: perdida diaria simulada de -9% (limite -8%) ->
    `bot_enabled` se apago solo para esa cuenta, log en `error_log`,
    reactivado manualmente sin problema.
  - Admin desconecta a un usuario CON SESION YA ABIERTA -> siguiente
    request devuelve 302 (redirect a login), ni siquiera puede
    re-loguearse (queda en `rejected`).
  - Dashboard (`GET /`) y todos los endpoints (`/api/watchlist`,
    `/api/journal-summary`, `/api/account-value-history`) responden 200
    para un usuario activo.
  - **Bug encontrado:** `record_trade()` en la entrada de compra no
    mandaba `entry_volatility_strength`/`entry_band_width_pct`/
    `entry_band_width_percentile`/`entry_volume_ratio`/
    `entry_consecutive_squeeze_bars` (los campos ricos del diario de
    operaciones que SI tiene `trading-bot/auto_entry.py`) -- no rompia
    nada, pero perdia datos para analizar rendimiento por estrategia mas
    adelante. Corregido y reprobado: trade de prueba con TODOS los
    campos se registro bien, y `daily_breakdown()`/`weekly_summary()`
    (analisis del diario) leyeron los datos sin error.
  - **Prueba de la regla central:** misma señal (SPY call, mismo spot) a
    2 cuentas de capital muy distinto ("rica" $50,000 vs "pobre" $300) ->
    rica recibio el contrato ideal (strike 760, mayor volumen dentro del
    rango de delta), pobre cayo al fallback mas barato que le alcanzaba
    (strike 767) -- confirma que el dimensionamiento por capital funciona
    de punta a punta, no solo en aislamiento.

- [x] **Tercera tanda: `corrige todo` (2026-09-10, mismo dia) -- 3 gaps
  cerrados:**
  1. `watchdog.py` y `health_check.py` todavia vigilaban los procesos
     VIEJOS de un solo usuario (`position_monitor.py`, `proximity_watch.py`,
     `auto_entry.py`, que nunca se van a correr en este proyecto) en vez
     de `multi_user_entry_loop.py`. Corregido en los dos archivos,
     re-probado: `health_check.py` detecto correctamente que
     `multi_user_entry_loop.py` no estaba corriendo (cierto, todavia no
     se desplego como proceso persistente).
  2. `check_account_math()`/`check_position_prices()`/
     `check_duplicate_positions()` en `health_check.py` chequeaban la
     UNICA base de datos compartida (que ya no es la cuenta de nadie en
     este sistema multi-tenant) -- reescritas para iterar TODOS los
     usuarios activos, cada uno con su propio chequeo. `BACKUP_DIR`
     tambien corregido (decia "trading-bot-backup", ahora
     "bot-familiar-backup", para no mezclarse con el respaldo del otro
     proyecto en el mismo escritorio).
  3. **Capital real al registrarse** -- `signup.html` tiene un campo
     nuevo "Capital inicial (simulado)", `/registro` en `webapp/app.py`
     ya crea la base de datos de trading del usuario CON ESE capital al
     momento del registro (antes: todos arrancaban con el mismo $10,000
     placeholder, recien creado en el primer acceso). Probado: usuario
     registrado con $2,500 -> su cuenta arranco con $2,500 reales, no el
     placeholder.
  Tambien se sincronizaron desde trading-bot (mismo dia, cambios
  puramente aditivos verificados con diff antes de copiar):
  `paper_trading/engine.py` (circuit breaker), `paper_trading/bollinger_strategy.py`
  (cache de historial compartida entre procesos + reintentos con
  presupuesto acotado ante rate-limit de Yahoo), `webapp/market_data.py`
  (mismo cache/backoff + `get_option_bid_ask`), `paper_trading/option_selection.py`
  (campos bid/ask en el contrato elegido), y `paper_trading/family_sizing.py`
  (nuevo aca, dimensionamiento por capital). El cierre manual de
  posiciones en `webapp/app.py` tambien ejecuta al BID real ahora.
  - [ ] **Pendiente:** `market_data.py` (cotizaciones, historial tecnico)
    sigue siendo compartido entre todos -- correcto asi (no es dato de
    cuenta, es dato de mercado publico), pero falta medir si 10 usuarios
    reales generan mas carga de la que el cache actual (45s TTL) puede
    absorber -- vigilar el mismo riesgo de rate-limit que ya paso hoy en
    trading-bot si se despliega con gente real.

- [x] **Cuarta tanda (2026-09-10, mismo dia, "hazlo"): `proximity_watch.py`
  agregado de vuelta** -- NO fue reemplazado por `multi_user_entry.py`
  (es un servicio compartido de alertas tempranas, nunca toco cuentas de
  nadie). Sincronizado desde trading-bot (concurrencia 8->3) y agregado
  de nuevo a `MANAGED_PROCESSES` en `watchdog.py` y a
  `background_scripts` en `health_check.py`. Probado: `--once` encontro
  3 casi-señales reales (QQQ, SPY, IWM) sin errores.

  **Bug real encontrado en el mismo paso:** `health_check.py` de
  bot-familiar detecto el `proximity_watch.py` de **trading-bot** (otro
  proyecto corriendo en la misma PC) y penso que era el suyo propio --
  el filtro de PowerShell solo comparaba el NOMBRE del archivo, no de que
  carpeta/proyecto venia, y ambos proyectos tienen scripts con el mismo
  nombre. Corregido en `watchdog.py` y `health_check.py`: ahora se
  lanza y se busca cada script por su RUTA ABSOLUTA dentro de ESTE
  proyecto (`MANAGED_PROCESSES[...]["match"]` = `str(ROOT / script)`),
  no por el nombre pelado. Reprobado en vivo: antes de corregir,
  `_script_is_running('proximity_watch.py')` devolvia `True` (el de
  trading-bot); despues de corregir, `_script_is_running(str(ROOT /
  'proximity_watch.py'))` devuelve `False` correctamente (el propio
  proceso de bot-familiar no estaba corriendo, cierto en ese momento).

- [x] **Panel de administrador: seccion "Salud del sistema" + boton
  "Corregir ahora"** (2026-09-10, a pedido explicito del usuario -- "que
  el error llegue al panel de administrador y desde ahi poder dar la
  orden de corregirlo"): nuevas rutas `/admin/health` (GET -- corre los
  4 chequeos de `health_check.py`, TODOS ya iteran cuenta por cuenta,
  mas los ultimos 30 errores del `error_log` del sistema) y `/admin/heal`
  (POST -- corre `watchdog.check_and_heal_once()` AHORA MISMO en vez de
  esperar el proximo ciclo de 10 min, reinicia lo que este caido/colgado
  y devuelve que accion tomo). Nunca expone balance/P&L/posiciones,
  solo el mensaje del problema (que ya incluye el username cuando
  aplica, sin montos).

  **Probado de punta a punta y con efecto real:** el boton "Corregir
  ahora" (probado via HTTP) detecto que `proximity_watch.py` y
  `multi_user_entry_loop.py` no estaban corriendo TODAVIA (cierto, nunca
  se habian desplegado como procesos persistentes) y los ARRANCO de
  verdad -- confirmado con los PID reales corriendo despues. O sea: el
  "auto-reparacion para cada usuario" que pidio el usuario ya existe de
  punta a punta -- `watchdog.py` corre estos mismos chequeos solo cada
  10 min automaticamente, y ahora ADEMAS el admin puede dispararlo a
  demanda desde el panel y ver el resultado al instante.
- [ ] **Tamano de posicion Y CONTRATO proporcional al capital de cada usuario** (a pedido del usuario, 2026-09-10, refinado el mismo dia): reemplazar `contracts_per_trade` (cantidad fija igual para todos) por un `risk_pct_per_trade` POR USUARIO (default sugerido 2%, cada quien puede ajustar el suyo). **NOTA (2026-09-10): la parte de CONTRATO ya esta hecha** -- `family_sizing.py::select_affordable_contract()` ya elige distinto contrato/ticker por capital disponible (cash_balance). Lo que falta es la parte de `risk_pct_per_trade` (hoy la cantidad de contratos sigue siendo `contracts_per_trade` fijo en 1, no un % de riesgo ajustable por usuario).

  1. La regla base NO cambia (ya construida y probada en `trading-bot/option_selection.py`, 2026-09-09): dentro del rango de delta 0.40-0.60, el candidato "ideal" es el de MAYOR VOLUMEN (no solo el mas cercano a 0.50), con el filtro de liquidez real (`MIN_OPEN_INTEREST`). `select_contract()` deja de devolver UN solo contrato -- devuelve una lista de candidatos ordenada por ESE mismo criterio (ideal = mayor volumen dentro de 0.40-0.60 primero, despues los de afuera del rango ordenados por cercania a 0.50 como plan B), reutilizando el mismo trabajo de hoy, solo cambia el `return`.
  2. Para cada usuario: recorrer esa lista en orden (el "ideal" -- mayor volumen dentro de 0.40-0.60 -- primero) y quedarse con el PRIMERO cuyo costo (`precio * multiplier`) quepa dentro de `capital_usuario * risk_pct` para al menos 1 contrato. Cantidad = `floor((capital_usuario * risk_pct) / costo_de_ESE_contrato)`. Si a alguien SI le alcanza el capital, siempre recibe el contrato de mayor volumen dentro del rango ideal -- el "bajar de delta buscando algo mas barato" (punto 4) es SOLO para quien no le alcanza ni para ese.
  3. Esto significa que la MISMA señal puede terminar en un STRIKE DISTINTO por usuario, no solo distinta cantidad -- alguien con poco capital puede terminar en un contrato mas barato/mas lejos del precio (delta mas bajo) que alguien con mas capital, que sí llega al contrato "ideal" de delta ~0.50.
  4. Poner un piso razonable de delta (ej. no bajar de ~0.15-0.20) para no terminar en contratos tipo "billete de loteria" -- si ni el mas barato dentro de ese piso entra en el presupuesto de esa persona para ESE ticker, no se omite el ciclo entero para ella: se pasa a intentar la SIGUIENTE señal de la lista ordenada de ese ciclo (que puede ser un ticker distinto y mas barato, ej. AMC en vez de SPY). Solo si NINGUNA señal del ciclo es afrontable dentro de su riesgo, se omite el ciclo completo para esa persona. Resultado esperado y correcto: dos cuentas pueden terminar operando ETFs distintos en el mismo ciclo, no por eleccion deliberada sino porque cada una solo puede pagar lo que su capital permite. Loguear el motivo y que contrato/ticker especifico se le asigno a cada uno.
  5. Editable por cada persona en SU PROPIO panel (extender `/api/settings`, hoy edita watchlist/limite diario globales -- pasa a ser por-usuario, cada quien ve y cambia solo lo suyo, no lo de los demas).
- [ ] Login multi-usuario real conectado a las cuentas separadas (`webapp/auth.py` ya tiene la base)
- [ ] **Panel de administrador separado, SOLO para el usuario principal** (a pedido del usuario, 2026-09-10; alcance CORREGIDO el mismo dia -- ver nota abajo):
  1. `webapp/auth.py`: agregar columna `is_admin` (bool) a la tabla `users` -- solo el usuario principal la tiene en `True`.
  2. Nuevo decorador `admin_required` (extiende `login_required`) que ademas exige `session["is_admin"] == True` -- si no, 403 o redirect, nunca mostrar nada.
  3. Rutas nuevas SOLO accesibles con `admin_required`: panel `/admin` con SOLO estas funciones (ver correccion de alcance abajo) + salud del sistema (`health_check.py`, estado de los 5 procesos, logs) + gestion de usuarios (aprobar/rechazar solicitudes, conectar/desconectar cuentas).
  4. Los familiares NUNCA ven este panel ni saben que existe salvo que el usuario principal se lo diga -- ningun link visible desde su propio panel normal.
  5. Verificar que ninguna API existente devuelva datos de otros usuarios por error una vez que todo sea multi-usuario (cada endpoint debe filtrar SIEMPRE por el usuario de la sesion, excepto las rutas `/admin/*`).

  **CORRECCION DE ALCANCE (2026-09-10, mismo dia):** el admin NO puede ver
  el balance, P&L, ni posiciones de nadie -- eso es privado de cada
  cuenta, ni siquiera el usuario principal lo ve. El panel `/admin` es
  SOLO para: (a) corregir errores del sistema (salud/logs), (b) otorgar
  permisos (aprobar/rechazar altas), (c) conectar/desconectar cuentas
  (activar o suspender el acceso de alguien), y (d) actualizar la app
  (deploy/mantenimiento). Ningun endpoint de `/admin/*` debe exponer
  `balance`, `P&L`, `positions`, ni `trades` de otro usuario -- si alguna
  vista necesita saber "esta cuenta tiene actividad", usar solo un
  booleano (`activo`/`inactivo`) o timestamp de ultima conexion, nunca
  numeros de dinero.
- [ ] **Alta de usuarios: registro propio -> aprobacion del admin -> codigo por correo** (a pedido del usuario, 2026-09-10, ORDEN CORREGIDO el mismo dia -- ver nota abajo): NO es invitacion previa. El orden real es:
  1. El familiar se registra el mismo (pantalla "Crear cuenta"): elige su propio usuario, contraseña, Y correo electronico (el correo es obligatorio en este paso -- ahi es donde llega el codigo del paso 3). La cuenta queda creada pero en estado PENDIENTE -- no puede entrar al panel todavia.
  2. El usuario principal ve las solicitudes pendientes en `/admin` y las aprueba (o rechaza) una por una.
  3. Al aprobar, el sistema manda un correo (Gmail) a esa persona con un CODIGO numerico de un solo uso (con vencimiento, ej. 24-48h).
  4. La persona ingresa ese codigo en una pantalla de verificacion -- recien ahi la cuenta pasa a ACTIVA y puede entrar de lleno al panel.
  Piezas necesarias:
  - Tabla `users`: agregar estado (`pending` / `active` / `rejected`), no solo username/password.
  - Tabla nueva para los codigos de activacion (`user_id`, `code` hasheado -- igual que las contraseñas, nunca en texto plano, `created_at`, `expires_at`, `used_at`).
  - Envio de correo real: necesita un proveedor (SendGrid/Mailgun/SES o similar) y sus credenciales -- esto es infraestructura nueva, no existe hoy en el proyecto.
  - Login bloquea cuentas `pending` o `rejected` -- solo `active` entra.
  - `/admin` necesita una vista de "solicitudes pendientes" con boton aprobar/rechazar.
  - Mockups ya agregados al canvas de diseño: `Login.dc.html` (pasa a ser tambien registro), `Invite.dc.html` (pantalla de codigo -- OJO: el copy ahi todavia dice "te invito", hay que corregirlo a "tu cuenta fue aprobada, activala con este codigo") -- ver el link del mockup en la conversacion.
- [ ] Servidor de produccion (gunicorn) + HTTPS en vez del servidor de desarrollo de Flask
- [ ] Panel adaptado a pantalla de celular + manifest PWA (instalable desde el navegador)
- [ ] Deploy en el servidor Hetzner del usuario

## Criterio permanente: no sobre-confiar en muestras chicas

Al 2026-09-09, 14 trades cerrados, 100% de acierto (+9.6% a +11.3%
promedio segun estrategia). **No tratar esto como prueba de que la
estrategia funciona** -- n=14 es ruido, no señal. No repetir la
tentacion de reportar "100% win rate" como si fuera skill comprobado.
Revisar de nuevo cuando haya una muestra grande (100+ trades cerrados) y,
sobre todo, cuando haya trades PERDEDORES reales que muestren si el
stop-loss (-20%) esta bien calibrado -- hasta ahora ninguna posicion
cerro por stop-loss, asi que ese numero sigue sin validar con datos
reales.

## Arquitectura: 5 procesos de fondo + 1 censor

| Proceso | Que hace | Cadencia | Log |
|---|---|---|---|
| `run_dashboard.py` | Panel web Flask (:5000) | request-driven | `data/flask.log` |
| `position_monitor.py` | Cierra posiciones abiertas cuando se cumple una regla de salida | cada 30s | `data/position_monitor.log` |
| `proximity_watch.py` | Detecta tickers "cerca" de disparar señal (sin gastar tokens de agente) | cada 30-60s | `data/proximity_watch.log` |
| `auto_entry.py` | **Motor de entradas autonomo**: escanea TODA la watchlist y EJECUTA señales de confianza alta/media sin agente -- contrato elegido via yfinance (Black-Scholes aproximado, ver `paper_trading/option_selection.py`), no via MCP de Robinhood | cada 2 min | `data/auto_entry.log` |
| `watchdog.py` | **Censor**: detecta y REPARA los 4 de arriba si estan caidos o colgados, y sincroniza el respaldo (ver abajo) | cada 10 min | `data/watchdog.log` |
| `watchlist_scan.py` | Version MANUAL de la misma logica que `auto_entry.py`, pero solo REPORTA (no ejecuta) y usa el MCP de Robinhood para elegir el contrato -- para cuando el agente quiere revisar/ejecutar a mano con mejor precision de delta/precio que yfinance | bajo demanda del agente | stdout |

Base de datos: `data/paper_trading.db` (tablas `account`, `positions`,
`trades`, `settings`, `error_log`).

**Respaldo automatico**: `watchdog.py` sincroniza todo el proyecto (codigo +
`data/paper_trading.db`) hacia `%USERPROFILE%\Desktop\trading-bot-backup`
en CADA ciclo (robocopy `/MIR`, excluye `__pycache__` y `*.lock`) -- a
pedido del usuario (2026-09-09), para que el respaldo se mantenga siempre
al dia, no sea una foto unica que envejece. Si el proyecto se pierde, ese
respaldo (codigo + base de datos juntos) alcanza para reconstruir todo
desde cero -- la base de datos SOLA no alcanza (no tiene el codigo).

**Lock de instancia unica**: los 5 procesos gestionados (`position_monitor.py`,
`proximity_watch.py`, `auto_entry.py`, `watchdog.py`, `run_dashboard.py`) usan
`paper_trading/singleton_lock.py` -- se niegan a arrancar si ya hay otra
instancia de si mismos corriendo (`data/<nombre>.lock`, liberado por el SO
al terminar el proceso, sin importar como termine). Sin esto, dos
instancias del mismo script pueden ejecutar la MISMA señal dos veces (paso
de verdad el 2026-09-09: al reiniciar watchdog.py, su chequeo inmediato al
arrancar detecto auto_entry.py "no corriendo" y lo arranco solo, un
segundo antes de que tambien se arrancara a mano -- 2 instancias en
paralelo, una re-compro un contrato que la otra ya habia comprado). Si vas
a reiniciar cualquiera de estos 4 a mano, no hace falta preocuparte por
coordinarlo con el censor -- el lock ya lo protege.

## Si "el bot se paraliza" -- diagnostico en 3 pasos

1. `python health_check.py` -- matematica de cuenta, precios en vivo,
   posiciones duplicadas, si Flask/los monitores responden.
2. `type data\watchdog.log` (o `Get-Content -Tail 30`) -- que detecto y
   reparo el censor en los ultimos ciclos. Si el censor mismo no esta
   corriendo, ese es el problema raiz (ver abajo).
3. Confirmar el censor esta vivo: PowerShell
   `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where CommandLine -like '*watchdog.py*'`.
   Si no aparece, relanzarlo:
   `Start-Process python -ArgumentList '-u','watchdog.py','--interval','600' -WorkingDirectory 'C:\Proyectos\trading-bot' -WindowStyle Hidden`

## Causa raiz ya corregida (2026-09-09): yfinance sin timeout

`webapp/market_data.py` y `paper_trading/bollinger_strategy.py` llamaban a
yfinance (`.history()`, `.option_chain()`, `.fast_info`, `yf.download()`)
sin ningun limite de tiempo explicito. Bajo rate-limit sostenido de Yahoo
(varios procesos pidiendo datos en paralelo cada 15-60s), una sola llamada
podia colgarse varios minutos -- eso bloqueaba el hilo entero del proceso
que la hizo: `position_monitor.py`/`proximity_watch.py` dejaban de
iterar (parecian "congelados" aunque el PID seguia vivo), y el panel
Flask (servidor de desarrollo, un solo hilo) quedaba sin responder a
NINGUNA peticion mientras tanto.

Fix: `_with_timeout()` (`market_data.py`) y `_fetch_history()`
(`bollinger_strategy.py`) envuelven toda llamada de red en un
`ThreadPoolExecutor` con `future.result(timeout=15)` -- limite de pared
duro. Si yfinance no responde en 15s, se lanza `TimeoutError`, que los
`try/except` ya existentes en cada funcion capturan igual que cualquier
otro fallo de red (se salta ese ticker/precio, no se cuelga el proceso).

Esto NO elimina el rate-limit de Yahoo en si -- solo evita que un
timeout se convierta en una congelacion indefinida. Si `watchdog.log`
muestra reinicios frecuentes de `position_monitor.py`/`proximity_watch.py`
por "colgado", es señal de que el rate-limit es demasiado agresivo para la
cadencia actual (considerar espaciar mas los intervalos o reducir tickers
en paralelo en `MAX_PARALLEL_REQUESTS` de `watchlist_scan.py`).

## squeeze_breakout_temprano (entrada sobre vela en formacion): que SI y que NO prueba el estudio

Validado con datos reales de 1m (2026-09-09, ~7 dias, limite de yfinance,
90-138 eventos segun umbral -- muestra chica). Resultado honesto: la
confirmacion (la ruptura se sostiene hasta el cierre real) es 74-84% segun
umbral, PERO el valor esperado de "cuanto mas se gana por entrar temprano
en vez de esperar al cierre de esa misma vela" es aprox. CERO o
ligeramente negativo en todas las combinaciones probadas -- lo que se
pierde en los falsos positivos que revierten supera lo que se gana en los
que se confirman. Parametros subidos a la mejor combinacion encontrada
(margen 0.30%, volumen proyectado >1.3x, antes 0.15%/1.0x).

**No vender esta funcion como "gana mas por adelantarse dentro de la
misma vela"** -- eso no esta probado. Su valor real es reducir el retraso
de reaccion del sistema (el caso SPY del 2026-09-09 que motivo crear
`auto_entry.py`), no fabricar edge direccional extra. Revalidar si el
comportamiento del mercado cambia o si yfinance permite mas historial de 1m.

## Que SI repara el censor vs. que NO

- SI (automatico): Flask caido, `position_monitor.py`/`proximity_watch.py`
  caidos o colgados (log sin crecer > 6x su intervalo esperado).
- NO (solo reporta en `error_log` + `watchdog.log` para revision humana):
  matematica de cuenta que no cuadra, posiciones duplicadas -- corregir
  datos de cuenta a ciegas es mas riesgoso que dejarlo visible.

## Regla oficial: las posiciones SI se mantienen overnight (cambio 2026-09-09)

**Historial:** hasta el 2026-09-09 el sistema era dia trading puro --
`force_eod_exit` cerraba TODA posicion al terminar la sesion, sin importar
su P&L (asi se cerro AMC en -9.52% ese dia, sin haber tocado ni objetivo
ni stop). A pedido explicito del usuario tras ver ese cierre, se cambio:

**Regla actual:** las posiciones YA NO se fuerzan a cerrar por fin de
sesion. `position_monitor.py::_is_near_expiration()` reemplazo a
`is_near_session_close()` como disparador de `force_eod_exit` en
`evaluate_open_position_exit()` -- ahora ese parametro se activa solo
cuando faltan <= `NEAR_EXPIRATION_DAYS` (1) dias calendario para el
VENCIMIENTO REAL del contrato de opcion, no para el cierre del mercado de
hoy. El motivo de cierre paso de `cierre_sesion` a `cierre_por_vencimiento`.

**RIESGO agregado, dicho explicitamente al usuario antes de implementar:**
esto expone las posiciones a gaps de precio fuera de horario (noticias,
after-hours) y a la caida de la prima por el simple paso de los dias
(theta), algo que la disciplina de day trading puro evitaba por completo.
A cambio, una posicion que no llego al objetivo en el dia tiene mas tiempo
real para llegar, en vez de forzarse a cerrar en una zona intermedia.

**Sigue intacto:** `objetivo_alcanzado` y `stop_loss` siguen siendo
prioridad #1 y #2 (se evaluan primero, sin importar el dia) -- y vende al
precio de mercado REAL del momento, no capado al objetivo exacto (si
amanece en +15% habiendo pedido +10%, se capturan esos +15%). Tambien
sigue intacto `too_close_to_open_new_position()` -- las entradas NUEVAS
siguen bloqueadas cerca del cierre de sesion (eso no se toco).

## Bug real: multi_user_entry_loop.py se colgaba (0% CPU, sin loguear nada) -- 2026-09-11

**Sintoma:** el wrapper quedaba vivo (PID visible, `Responding: True`)
pero con ~0% de CPU acumulado y sin agregar NINGUNA linea nueva a
`data/multi_user_entry.log` durante 10+ minutos, pese a un intervalo de
120s. Pasaba de forma reproducible, generalmente despues de 1-2 ciclos
exitosos -- no era un problema de la logica de escaneo en si (probada
por separado llamando `run_entry_cycle()` directo, termino rapido y sin
colgarse).

**Causa real encontrada:** el wrapper llamaba
`subprocess.run([...,"--once"], timeout=90, capture_output=True, text=True)`.
Con `capture_output=True`, Python abre PIPES para stdout/stderr del
hijo. Si el hijo (o algun thread suyo -- `ThreadPoolExecutor` escaneando
45 tickers via yfinance) no cierra limpio esos descriptores al terminar
el proceso, el `communicate()` interno de `subprocess.run()` se queda
esperando a que el pipe cierre DESPUES de que el timeout ya mato al
proceso principal -- deadlock indefinido, sin excepcion, sin log, sin
CPU (esperando I/O que nunca llega).

**Fix:** se elimino `capture_output`/PIPES por completo -- el hijo ahora
corre con `stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL` (ya
loguea a su propio archivo via `_log()`, no hace falta capturar su
stdout), y se cambio de `subprocess.run(timeout=...)` a
`subprocess.Popen()` + `proc.wait(timeout=...)` manual -- si se excede
el timeout, se mata el ARBOL COMPLETO de procesos con
`taskkill /F /T /PID <pid>` (no solo el proceso principal -- un
`proc.kill()` de Python no garantiza matar threads/hijos que haya
dejado colgados). Portado tambien a los 3 wrappers de trading-bot
(`auto_entry_loop.py`, `position_monitor_loop.py`, `family_sim_loop.py`)
por si les puede pasar lo mismo, aunque el sintoma solo se vio del lado
de bot-familiar.
