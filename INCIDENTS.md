# Incidentes -- bitacora para no repetir errores

Registro de cada bug/incidente operativo real encontrado en este sistema:
sintoma, causa raiz, fix aplicado, y la LECCION en una linea (la parte que
importa para no repetirlo). Orden cronologico, mas reciente arriba.

**Regla para quien trabaje en este repo (agente o humano): si corregis un
bug real en este sistema, agrega una entrada aca ANTES de dar el trabajo
por terminado.** Un fix sin entrada en este archivo es un fix que se puede
repetir en 3 meses.

---

## 2026-09-17 #3 -- salidas evaluadas fuera de horario de mercado (COST, doble venta erronea)

**Sintoma:** COST se vendio por stop-loss a las 4:08pm ET (-20.64%) y,
tras reabrir la posicion para corregirlo, se volvio a vender sola a las
6:07pm ET (-27.05%), disparando ademas un circuit breaker en una cuenta
por perdida diaria.

**Causa raiz:** `too_close_to_open_new_position()` (bollinger_strategy.py)
ya bloqueaba ENTRADAS nuevas fuera de horario (ver incidente 2026-09-17 #2
mas abajo), pero `run_exits_for_account()` (multi_user_entry.py), que
revisa las posiciones YA ABIERTAS para cerrarlas por stop-loss/objetivo,
nunca tuvo ese chequeo -- corria 24/7 sin parar. Despues de las 4pm ET,
yfinance sigue devolviendo "cotizaciones" con volumen 0 (confirmado con
datos reales: la barra de 3:55-4:00pm de COST tuvo 184,493 de volumen
real, la de las 4:00pm en adelante tuvo 0) -- el motor de salidas tomaba
esas cotizaciones sin liquidez real como si fueran validas.

**Fix:** `market_is_open_now()` (multi_user_entry.py) -- mismo criterio
que las entradas (9:30am-4:00pm ET, dia de mercado real via
`is_trading_day`/`market_close_time`) -- ahora se chequea al inicio de
`run_exits_for_account()`. Fuera de esa ventana, la funcion devuelve 0
sin evaluar nada. Las dos ventas erroneas se revirtieron a mano en las 6
cuentas afectadas (trade de venta borrado, cash/realized_pnl restaurados,
posicion reinsertada con su profit_target_pct/stop_loss_pct originales
recuperados del texto de la razon de la compra).

**Leccion:** un limite de horario agregado de un lado (entradas) no
protege el otro lado (salidas) -- son dos rutas de codigo separadas. Toda
verificacion de "el mercado esta abierto" tiene que aplicarse a CADA
punto donde el sistema decide actuar sobre datos de precio, no solo a
donde se abren posiciones nuevas.

---

## 2026-09-17 #2 -- contrato de respaldo con spread ancho sin filtro propio (V, -22.6%)

**Sintoma:** V se compro por el camino de respaldo (delta fuera del
rango ideal 0.40-0.50) y perdio valor solo por el spread bid/ask, sin
que el mercado se moviera -- 22.6% de spread minutos despues de la
entrada (comprar al ask y marcar al bid ya perdia esa diferencia).

**Causa raiz:** `option_selection.py` ya tenia `MAX_SPREAD_PCT = 0.15`
(agregado 2026-09-11 tras el incidente de NIO), pero se aplicaba por
igual al camino ideal Y al camino de respaldo. Un contrato de respaldo ya
es de peor calidad por el delta -- exigirle el mismo spread flojo que al
camino ideal combina dos riesgos a la vez.

**Fix:** `MAX_SPREAD_PCT_FALLBACK = 0.10` -- limite mas estricto SOLO
para el camino de respaldo. Si ningun candidato del respaldo cumple ese
limite mas estricto, se usa el conjunto completo igual (no bloquea la
entrada si no hay alternativa mejor, mismo criterio que ya existia para
liquidez).

**Leccion:** un filtro de calidad (spread, liquidez) no puede tener un
solo umbral para TODOS los caminos de seleccion de contrato -- un camino
de respaldo, que ya acepta peor delta, necesita ser mas exigente en lo
demas, no igual de permisivo.

---

## 2026-09-17 #1 -- entradas de madrugada con datos de sesion anterior (10 posiciones perdidas)

**Sintoma:** 10 operaciones se abrieron entre las 12:00am y 12:16am ET,
todas perdedoras, usando la señal giro_sma20 con datos de la ultima vela
de 15m de la sesion ANTERIOR contra precios de fuera de horario sin
movimiento real.

**Causa raiz:** `too_close_to_open_new_position()` solo calculaba
"cuantos minutos faltan para el cierre de HOY" -- a las 12:16am, faltan
matematicamente ~943 minutos para las 4pm de HOY, asi que devolvia
`False` (permitido) sin verificar nunca si el mercado estaba realmente
abierto en ese momento.

**Fix:** se agrego el chequeo real `MARKET_OPEN_BAR_TIME <= now.time() <=
market_close_time(day)` mas `is_trading_day(day)`, ademas del calculo de
minutos-hasta-el-cierre que ya existia.

**Leccion:** "minutos hasta el cierre" y "el mercado esta abierto ahora"
son preguntas DISTINTAS -- la primera puede dar un numero grande y
positivo (pareciendo seguro) incluso cuando la segunda es claramente
falsa. Ver tambien incidente 2026-09-17 #3: el mismo tipo de hueco
aparecio de nuevo del lado de las salidas unas horas despues.

---

## 2026-09-09 #8 -- re-entrada inmediata sobre la misma señal (HOOD)

**Sintoma:** HOOD cerro con +10.27% de ganancia (`objetivo_alcanzado`) y
`auto_entry.py` volvio a comprarlo 19 segundos despues, practicamente al
mismo precio.

**Causa raiz:** una vela de 15m cerrada sigue siendo "la ultima señal"
hasta que pasan 15 min de verdad (no cambia entre ciclos de 2 min). La
salvaguarda de `open_tickers` (ver incidente #3) solo evita comprar de MAS
sobre una posicion YA abierta -- una vez que esa posicion se CIERRA, el
ticker deja de estar en `open_tickers` y la misma señal (todavia vigente,
misma vela) vuelve a calificar para una entrada nueva. No es una
duplicacion tecnica (el trade es legitimo por si solo) pero es re-comprar
un movimiento que ya se cobro, en vez de esperar una señal nueva.

**Fix:** `REENTRY_COOLDOWN_MINUTES = 15` en `auto_entry.py` -- despues de
CUALQUIER trade (compra o venta) de un ticker, no se abre una posicion
nueva en ese mismo ticker hasta que pasen 15 minutos (una vela completa),
sin importar si la señal sigue calificando.

**Leccion:** "no tiene posicion abierta" no es lo mismo que "es seguro
volver a entrar" -- un ticker recien cerrado puede seguir devolviendo la
misma señal (vela sin cambiar) por varios ciclos. El cooldown tiene que
medirse desde el ULTIMO trade (no solo desde la ultima apertura).

---

## 2026-09-09 #7 -- multi-agente para auditoria = rate limit de la API

**Sintoma:** al pedir una auditoria "minuciosa, detalle por detalle" del
sistema, se lanzo `/code-review max --fix` en modo multi-agente (10
sub-agentes en paralelo, uno por angulo de analisis). La mayoria fallo con
HTTP 429 ("hit your session limit") a mitad de camino -- cada sub-agente
es una sesion de API separada, y 10 en paralelo agotaron la cuota antes de
terminar. Solo 2 de 10 completaron (~150k tokens cada uno).

**Causa raiz:** usar el modo multi-agente (pensado para repos grandes) en
un pedido que en realidad se puede resolver leyendo ~10 archivos propios
directamente -- fan-out innecesario para el tamaño del trabajo.

**Fix:** las 2 auditorias que si completaron encontraron bugs reales
igual (ver incidentes de sesiones futuras que citen este); se aplicaron
leyendo el codigo directamente, sin relanzar mas agentes. Ver memoria
`feedback_trading_bot_no_multiagent` (fuera de este repo, en el sistema de
memoria del agente): para este proyecto, review/fix de codigo se hace en
una sola pasada, no en fan-out multi-agente, salvo que el usuario lo pida
explicitamente por nombre.

**Leccion:** el tamaño del pedido ("revisa todo minuciosamente") no
implica que la herramienta correcta sea multi-agente -- para ~10 archivos
propios, una lectura directa y cuidadosa es mas rapida, mas barata, y no
depende de que 10 sesiones de API entren en la misma ventana de rate
limit.

---

## 2026-09-09 #6 -- watchdog "colgado" era falso positivo (sin heartbeat)

**Sintoma:** `watchdog.log` mostraba `position_monitor.py`/`proximity_watch.py`
reiniciados cada ~10 minutos, casi en cada ciclo del censor.

**Causa raiz:** esos dos scripts solo imprimen algo cuando pasa un evento
(un cierre, una "casi-señal") -- en ciclos tranquilos (normal, no es un
bug) el log no crece durante minutos. El detector de "colgado" de
`watchdog.py` mide justamente eso (tiempo sin crecer el log) -- confundia
"nada que reportar" con "proceso muerto" y lo mataba/reiniciaba sin
necesidad.

**Fix:** ambos scripts ahora imprimen un heartbeat en CADA ciclo aunque no
haya nada que reportar (`position_monitor.py`, `proximity_watch.py`).

**Leccion:** un detector de "colgado" basado en actividad de log NECESITA
que el proceso vigilado garantice un heartbeat periodico. No asumas que
"sin logs nuevos" == "muerto" a menos que el proceso este diseñado para
loguear igual cuando no pasa nada.

---

## 2026-09-09 #5 -- reinicio manual sin redirigir stdout = log huerfano (de nuevo)

**Sintoma:** despues de arreglar el heartbeat (incidente #6), segui sin
ver la nueva linea en el log tras reiniciar los procesos a mano.

**Causa raiz:** relance `position_monitor.py`/`proximity_watch.py` via
PowerShell `Start-Process ... -WindowStyle Hidden` SIN
`-RedirectStandardOutput` -- exactamente el mismo error que origino la
sesion completa (ver #1). El stdout del proceso no iba a ningun lado util.

**Fix:** relanzar SIEMPRE con `subprocess.Popen(..., stdout=open(log,'a'), stderr=subprocess.STDOUT)`
(Python) en vez de `Start-Process` de PowerShell sin redirigir. `watchdog.py`
ya lo hace bien en su funcion `_start()` -- usar ESA como referencia, no
reinventar el lanzamiento a mano.

**Leccion:** cualquier reinicio manual de un proceso gestionado tiene que
redirigir stdout/stderr a su log, siempre. Si vas a reiniciar algo a mano,
copia el patron de `watchdog.py::_start()`, no uses `Start-Process` pelado.

---

## 2026-09-09 #4 -- msvcrt.locking() bloqueaba bytes distintos entre procesos

**Sintoma:** se agrego un lock de instancia unica (`singleton_lock.py`)
para evitar que 2 copias del mismo script corrieran a la vez -- una
prueba real (`auto_entry.py --once`) corrio en paralelo con la instancia
continua sin ser bloqueada, y re-compro una posicion que la otra ya tenia
abierta.

**Causa raiz:** el archivo se abria en modo `"a+"` y se llamaba a
`msvcrt.locking(f.fileno(), LK_NBLCK, 1)` SIN hacer `f.seek(0)` antes. En
modo append, la posicion inicial del archivo puede quedar al FINAL (no en
el byte 0) si el archivo ya tenia contenido de una corrida anterior --
`msvcrt.locking()` bloquea `n` bytes a partir de la posicion ACTUAL, asi
que dos procesos podian terminar bloqueando rangos de bytes distintos (uno
en el byte 0, otro en el byte 10) y nunca chocar entre si.

**Fix:** `f.seek(0)` SIEMPRE antes de `msvcrt.locking()`, para que todas
las instancias compitan por exactamente el mismo byte.

**Leccion:** con locks de archivo por posicion (`msvcrt.locking`, `fcntl.lockf`
con offset), fijar la posicion EXPLICITAMENTE antes de bloquear. Nunca
asumir que "recien abierto" implica posicion 0 -- depende del modo de
apertura y de si el archivo ya tenia contenido.

---

## 2026-09-09 #3 -- watchdog + relanzamiento manual = doble proceso (race)

**Sintoma:** tras reiniciar `watchdog.py`, aparecieron 2 instancias de
`auto_entry.py` corriendo en paralelo (mismo segundo de arranque).

**Causa raiz:** `watchdog.py` corre su primer chequeo de salud INMEDIATO
al arrancar (antes del primer sleep). Si justo en ese momento el agente
tambien esta arrancando el mismo proceso a mano, hay una ventana de
carrera: ambos ven "no esta corriendo" y ambos lo arrancan.

**Fix real:** el lock de instancia unica (ver incidente #4, una vez
corregido) -- eso es lo que realmente cierra esta ventana de carrera, no
un cambio de orden de arranque (que solo la hace menos probable, no
imposible).

**Leccion:** el orden de arranque no evita condiciones de carrera, solo
las hace menos frecuentes. La proteccion real es un lock exclusivo a
nivel de sistema operativo.

---

## 2026-09-09 #2 -- backtest historico con proxy de precio tautologico

**Sintoma:** un primer intento de medir si "entrar temprano" (antes de que
cierre la vela) daba mas ganancia uso el nivel de la banda de Bollinger
como precio de entrada temprana -- dio "100% de las veces mejor", un
resultado sospechosamente perfecto.

**Causa raiz:** el nivel de banda es, POR DEFINICION, un precio mas
favorable que el cierre en cualquier vela de ruptura (si rompe hacia
arriba, el cierre SIEMPRE esta por encima de la banda superior) -- el
"resultado" no media nada real del mercado, era una tautologia matematica
del proxy elegido.

**Fix:** se rehizo el estudio con datos reales de 1 minuto (yfinance,
ultimos ~7 dias) simulando exactamente lo que `analyze_forming_bar()`
habria visto minuto a minuto -- resultado honesto: valor esperado
practicamente nulo, no la ventaja que sugeria el primer intento.

**Leccion:** antes de confiar en un backtest, preguntar "¿el resultado
podria ser verdadero por construccion, sin importar los datos reales?" --
si el proxy de entrada/salida esta definido de forma que garantiza el
resultado, no es una medicion, es un calculo circular.

---

## 2026-09-09 #1 -- yfinance sin timeout = procesos congelados indefinidamente

**Sintoma:** el usuario reporto que "el bot se paraliza y no trabaja" --
`position_monitor.py`/`proximity_watch.py` parecian vivos (PID activo)
pero no avanzaban, y el panel Flask dejaba de responder por completo.

**Causa raiz:** ninguna llamada a yfinance (`.history()`, `.option_chain()`,
`.fast_info`, `yf.download()`) en todo el proyecto tenia limite de tiempo
explicito. Bajo rate-limit sostenido de Yahoo (varios procesos pidiendo
datos en paralelo cada 15-60s), una sola llamada podia colgarse varios
minutos, bloqueando el hilo entero del proceso que la hizo -- y el
servidor Flask de desarrollo (un solo hilo) quedaba sin responder a NADA
mientras tanto.

**Fix:** `_with_timeout()` (`webapp/market_data.py`) y `_fetch_history()`
(`paper_trading/bollinger_strategy.py`) envuelven toda llamada de red en
un `ThreadPoolExecutor` con `future.result(timeout=15)` -- limite de pared
duro, independiente de si la libreria respeta su propio timeout interno.

**Leccion:** cualquier llamada de red en un proceso de larga duracion
necesita un limite de tiempo impuesto desde AFUERA de la libreria (thread
+ `future.result(timeout=N)`), nunca confiar en que la libreria lo maneje
bien por si sola -- sobre todo con APIs no oficiales/scraping como
yfinance.
