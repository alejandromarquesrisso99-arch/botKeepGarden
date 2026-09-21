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

---

### D-012 · 2026-09-20 · La línea de un indicador multi-salida es un índice

**Contexto.** MACD, BBANDS y KELTNER producen tres series cada uno y el genoma
tiene que decir cuál mira. Los parámetros de un `FeatureGene` son números
(`dict[str, float]`, normalizados a `float` para que el hash del genoma sea
estable), así que no cabe un nombre de línea.

**Decisión.** `line` es un parámetro más del catálogo, con rango 0–2, y es el
**índice** dentro de `IndicatorSpec.multi_output`. `IndicatorSpec.line_name`
traduce índice a nombre para quien lo necesite.

**Consecuencias.** Mutación, validación, recorte a rango y distancia genética
tratan `line` sin ningún caso especial, y cambiar de la línea MACD al
histograma es una mutación de parámetro como cualquier otra. A cambio, un
genoma en JSON dice `"line": 2` y hay que mirar el catálogo para saber que eso
es el histograma; `genome.describe()` y el dashboard lo traducen.

---

### D-013 · 2026-09-20 · Los indicadores de rango excluyen la vela actual

**Contexto.** `DONCHIAN_HIGH` ya declaraba en el catálogo que excluye la vela en
curso. `HIGHEST` y `LOWEST` no decían nada.

**Decisión.** Los cuatro excluyen la vela actual (`rolling(n).max().shift(1)`).

**Consecuencias.** Una ruptura puede ocurrir de verdad. Si `HIGHEST` incluyera
la vela actual, `close > HIGHEST(high, n)` sería imposible por definición
—`high[t] >= close[t]`— y la familia BREAKOUT entera evolucionaría sobre reglas
muertas sin que nada fallara ruidosamente. Es la clase de error que se
descubre tres meses después preguntándose por qué una familia no prospera.

---

### D-014 · 2026-09-20 · El muestreo de reglas recibe los genes, no sus ids

**Contexto.** El contrato de `random_rule` recibía `feature_ids: list[str]`.

**Decisión.** Recibe los `FeatureGene` completos.

**Consecuencias.** Sin saber qué indicador hay detrás de un id no se puede
decidir con qué tiene sentido compararlo, que es justo lo que separa el
muestreo sesgado de un generador de ruido. Los ids se deducen de los genes, así
que no se pierde nada.

---

### D-015 · 2026-09-20 · Postura y polaridad al sembrar

**Contexto.** Un muestreo que elige bien los indicadores pero al azar el sentido
de las comparaciones produce bots de tendencia que compran debilidad y bots de
reversión que compran fuerza: la familia dice una cosa y el genoma hace otra.

**Decisión.** Cada familia tiene una **postura** (`up`, `down`, `compression`)
que fija el sentido de todas las comparaciones de la entrada; la salida es la
postura contraria. Además, los indicadores de rango tienen **polaridad**: un
techo (`DONCHIAN_HIGH`, `HIGHEST`) sólo se compara con el precio al alza y un
suelo (`DONCHIAN_LOW`, `LOWEST`) sólo a la baja.

**Consecuencias.** Sobre siete años de BTC/USDT, alrededor de tres de cada
cuatro bots sembrados llegan a abrir posición, frente a uno de cada diez sin el
sesgo. La evolución empieza a explorar desde ideas con forma en vez de gastar
generaciones descartando reglas que nunca se cumplen. Coste: el sembrador no
explora combinaciones "al revés" que podrían funcionar; la mutación del hito 4
sí puede llegar a ellas.

---

### D-016 · 2026-09-21 · `FIXED_FRACTION` invierte `max_exposure`, no `risk_per_trade`

**Contexto.** El contrato de `portfolio.py` decía que `FIXED_FRACTION` compra
`equity * risk_per_trade / precio`. Con `risk_per_trade` entre 0.002 y 0.02, eso
son posiciones de entre el 0.2 % y el 2 % del capital: `max_exposure` no llegaría
a actuar nunca y el criterio de aceptación del hito 3 —un buy & hold reproduce el
retorno del activo— sería inalcanzable.

**Decisión.** `FIXED_FRACTION` invierte `max_exposure` del capital, que es la
"fracción fija" que el gen anuncia. `risk_per_trade` conserva su significado de
**riesgo**, que es el que usan `ATR_RISK` (pérdida al saltar el stop) y
`VOL_TARGET` (volatilidad diaria objetivo).

**Consecuencias.** Los tres modos de sizing significan cosas distintas y
coherentes, y ninguno mezcla "fracción invertida" con "fracción arriesgada", que
era lo que hacía incoherente la línea original. Un bot de fracción fija con
`max_exposure = 0.95` despliega casi todo su capital, así que la evolución tiene
que elegir de verdad entre exposición y riesgo en vez de que la decisión venga
dada por un descuido de la fórmula.

---

### D-017 · 2026-09-21 · El take profit no cobra el hueco a favor

**Contexto.** Si una vela abre en 115 con el take profit en 110, una orden
limitada real habría ejecutado en 115. Cobrarlo así es lo realista.

**Decisión.** Se cobra en 110. El hueco a favor no se cobra; el hueco en contra
del stop sí (ahí el fill es la apertura, no el precio del stop).

**Consecuencias.** La asimetría es deliberada: en las dos direcciones se elige
el peor caso. Un backtest optimista es peor que ninguno, porque da confianza
donde no la hay, y con velas de 1h no hay forma de reconstruir lo que pasó
dentro de la vela. Coste: el motor subestima ligeramente a los bots con take
profit, y eso hay que tenerlo presente al comparar familias.

---

### D-018 · 2026-09-21 · El trailing se mide con el ATR de entrada

**Contexto.** Un trailing `ATR_MULT` necesita un ATR para saber cuánto separarse
del extremo favorable. Podía ser el ATR vivo de cada vela o el del momento de
abrir.

**Decisión.** El de entrada, guardado en la posición.

**Consecuencias.** Con el ATR vivo, un repunte de volatilidad *alejaría* el stop
del precio, que es exactamente lo contrario de lo que un trailing debe hacer:
soltaría la posición justo cuando más protección hace falta. Con el ATR de
entrada, el stop sólo se mueve a favor y el 1R de la operación significa lo
mismo de principio a fin.
