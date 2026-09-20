# Registro de decisiones

Formato: una entrada por decisión, con fecha, contexto, decisión y consecuencias.
Añade al final; no reescribas entradas pasadas. Si una decisión se revierte, se
añade una entrada nueva que la supersede.

---

### D-001 · 2026-09-14 · Cripto 24/7 con velas de 1h

**Contexto.** El jardín debe "estar funcionando constantemente con datos reales".
Las alternativas eran índices US en diario (1 observación al día, mercado cerrado
16h y fines de semana) o cripto.

**Decisión.** Binance spot, `BTC/USDT`, velas de 1h.

**Consecuencias.** El jardín late 24/7 y una generación semanal da 168
observaciones. Datos históricos gratis y largos. A cambio: mercado más ruidoso,
sin cortos en spot, y comisiones del 0.1% que pesan mucho en estrategias de
rotación alta — de ahí la penalización por `fee_drag` en el fitness.

---

### D-002 · 2026-09-14 · El bot es un genoma de datos, no código generado

**Contexto.** Se podía hacer que un agente escribiera el código Python de cada
bot. Es más creativo pero imposible de correr en bucle continuo: lento, caro, no
determinista y con superficie de ejecución arbitraria.

**Decisión.** El bot es una estructura de datos serializable, compilada a señales
por un compilador cerrado que sólo produce combinaciones del catálogo.

**Consecuencias.** Mutación y cruce son operaciones sobre datos, triviales y
baratas. Se pueden evaluar miles de genomas por segundo. El dashboard puede
mostrar el genoma. A cambio: la creatividad del sistema está acotada por el
catálogo de genes, y ampliarlo requiere escribir código. Por eso el jardinero
tiene la propuesta `ADD_GENE`, que genera una tarea de ingeniería.

---

### D-003 · 2026-09-14 · Doble reloj: incubadora rápida, jardín vivo lento

**Contexto.** Con velas de 1h y generaciones semanales, evolucionar sólo en vivo
daría ~50 generaciones al año. Demasiado lento para que la evolución descubra
nada.

**Decisión.** Dos relojes. La incubadora hace evolución masiva sobre histórico
con walk-forward; el jardín vivo hace de juez con datos reales y es quien manda
en el fitness efectivo (peso 0.7 frente a 0.3).

**Consecuencias.** Se combinan velocidad de exploración y honestidad de
evaluación. Riesgo principal: sobreajuste en la incubadora, mitigado con embargo,
holdout intocable, penalización por multiplicidad y parsimonia.

---

### D-004 · 2026-09-14 · Cada bot tiene cartera aislada

**Contexto.** Alternativa: un capital común del que los bots piden asignación.

**Decisión.** Cada bot arranca con su propio capital simulado y no lo comparte.

**Consecuencias.** Las comparaciones entre bots son limpias y un bot no puede
degradar a otro. El "capital del jardín" es la suma. A cambio, el sistema no
modela la asignación de capital entre estrategias, que es un problema real y
que queda para más adelante (probablemente como un meta-bot asignador).

---

### D-005 · 2026-09-14 · SQLite + Parquet

**Contexto.** Hacía falta persistencia con cero infraestructura en Windows.

**Decisión.** SQLite en modo WAL para el estado del jardín (bots, genomas,
generaciones, operaciones, eventos, decisiones). Parquet para las velas.

**Consecuencias.** El jardín entero es un archivo copiable. El dashboard lee en
paralelo sin bloquear al motor. Las velas, que son muchas y se leen en bloque,
van donde deben. Si algún día el volumen lo pide, DuckDB lee los mismos Parquet
sin migración.

---

### D-006 · 2026-09-14 · `live` bloqueado por código

**Contexto.** El destino del proyecto es dinero real, pero un error de
configuración no puede ser el camino hasta ahí.

**Decisión.** `execution.mode: live` aborta con error explícito en `config.py`.
Desbloquearlo exige editar código a mano y conscientemente.

**Consecuencias.** Imposible llegar a dinero real por accidente ni por una
decisión del jardinero.

---

### D-007 · 2026-09-14 · La fusión no consume a los padres

**Contexto.** Al fusionar bots, ¿mueren los padres?

**Decisión.** No. La fusión crea un hijo ensemble y los padres siguen vivos y
operando por su cuenta.

**Consecuencias.** Se puede medir directamente si la fusión aporta: el hijo y sus
padres corren en paralelo sobre los mismos datos. Coste: más población viva. Se
compensa con `fusion.max_depth = 2`, que impide ensembles de ensembles de
ensembles.

---

### D-008 · 2026-09-14 · Los impuestos quedan fuera del fitness

**Contexto.** En el proyecto anterior se planteó modelar impuestos y comisiones
en paper trading.

**Decisión.** Comisiones y slippage sí, siempre. Impuestos no, dentro del motor.

**Consecuencias.** El impuesto es aproximadamente un factor común a todas las
estrategias y meterlo en el fitness añade ruido sin cambiar el orden. Cuando haya
dinero real se hará un informe fiscal aparte, que es donde importa.

---

### D-009 · 2026-09-20 · El parentesco incluye la co-parentalidad

**Contexto.** El test `test_parentesco` del andamiaje exigía que dos bots que
han tenido un hijo juntos se consideren emparentados, aunque no compartan
ningún antepasado. La implementación original de `Pedigree.related` sólo miraba
el árbol de antepasados y el test estaba en rojo.

**Decisión.** `related(a, b)` es cierto si comparten antepasado, si uno
desciende del otro **o si han engendrado un hijo directo juntos**. Compartir un
descendiente lejano no cuenta.

**Consecuencias.** Dos fundadores sin relación de sangre quedan emparentados en
cuanto se cruzan, que es justo lo que la selección necesita saber para no
repetir el mismo cruce generación tras generación. Se descartó la variante
"comparten cualquier descendiente" porque en un jardín con fusiones todo
confluye aguas abajo y acabaría declarando emparentado a todo el mundo.

---

### D-010 · 2026-09-20 · Las marcas de huecos viven en el manifiesto de la caché

**Contexto.** `docs/DATA.md` dice que un hueco de velas se marca en la tabla
`data_gaps` de SQLite. Pero esa tabla pertenece a `storage/`, que es un hito 4,
y la regla de dependencias de `docs/ARCHITECTURE.md` §6 prohíbe que `data/`
importe de `storage/`.

**Decisión.** En el hito 1 las marcas de huecos se guardan en
`state/cache/candles/manifest.json`, junto al resto del resumen de cada serie.
`keepgarden data status` las lee de ahí para distinguir un hueco explicado de
uno que nadie ha mirado.

**Consecuencias.** El hito 1 no arrastra media capa de persistencia y `data/`
sigue sin saber que SQLite existe. Cuando llegue el hito 4, el volcado a
`data_gaps` se hace desde arriba (un repositorio que lee el manifiesto), no
desde `data/`. Coste: la misma información en dos sitios durante un tiempo; el
manifiesto es la fuente y SQLite la copia consultable.

---

### D-011 · 2026-09-20 · `trades` puede ser desconocido

**Contexto.** `ccxt.fetch_ohlcv` no expone el número de operaciones de la vela,
aunque el endpoint de Binance sí lo devuelve. La columna `trades` es parte del
esquema canónico de velas.

**Decisión.** `trades` es `float64` y vale `NaN` cuando el venue no lo dice. Las
velas sintéticas de relleno llevan `trades = 0`, igual que el volumen.

**Consecuencias.** Un futuro indicador de microestructura puede distinguir "no
lo sé" de "no hubo ninguna operación", que son cosas muy distintas. Si algún día
hace falta de verdad, se rellena con una llamada específica al endpoint de
klines sin tocar el esquema.
