# Incidentes -- bitacora para no repetir errores

Registro de cada bug/incidente operativo real encontrado en este sistema:
sintoma, causa raiz, fix aplicado, y la LECCION en una linea (la parte que
importa para no repetirlo). Orden cronologico, mas reciente arriba.

**Regla para quien trabaje en este repo (agente o humano): si corregis un
bug real en este sistema, agrega una entrada aca ANTES de dar el trabajo
por terminado.** Un fix sin entrada en este archivo es un fix que se puede
repetir en 3 meses.

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
