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

---

### D-019 · 2026-09-21 · La distancia genética mira el gen antes que el parámetro, y el fitness admite una escala congelada

**Contexto.** Dos piezas del hito 4 salieron mal calibradas nada más medirlas.

La primera, la distancia genética. Con el Jaccard definido sólo sobre
`(kind, params discretizados)`, dos cruces de medias con periodos distintos no
comparten absolutamente nada y acaban a distancia 0.48: especies separadas. Pero
la especiación existe justo para impedir que el jardín entero acabe siendo
"variaciones de la misma EMA", así que el mecanismo fallaba en el único caso que
tenía que cubrir. Además, el operando derecho de una hoja se normalizaba a la
palabra `const` sin mirar su valor, de modo que `TWEAK_THRESHOLD` —una de cada
cinco mutaciones— producía hijos a distancia exactamente cero de su padre.

La segunda, el fitness. Es un z-score robusto contra la población viva, así que
su mediana vale 0 por construcción en toda generación. Eso está bien para
seleccionar, que es comparar contemporáneos, pero hace imposible responder a
"¿está mejorando el jardín?", que es justo el criterio de aceptación del hito.

**Decisión.** Tres cambios.

1. El término de features es mitad Jaccard sobre los `kind` (qué mira el bot) y
   mitad sobre `kind` + parámetros en rejilla (cómo lo ha afinado). La constante
   está en `distance.KIND_SHARE`.
2. Las constantes de las reglas entran en la hoja normalizada con su valor
   discretizado, no como una etiqueta genérica.
3. `compute_fitness` acepta una `reference` —el centro y la escala de otra
   población, que produce `robust_reference`—. `incubate` congela la de la
   generación 0 y mide contra ella todas las demás.

**Consecuencias.** Dos variaciones de la misma idea caen ahora en la misma
especie y por encima del umbral de clon, que es donde tienen que estar. La
mediana de fitness pasa a ser un número comparable entre generaciones y el
progreso del jardín se puede leer de un vistazo. Coste: el fitness tiene dos
modos y hay que saber cuál se está usando; el relativo manda en la selección y
el absoluto sólo sirve para mirar desde fuera.

---

### D-020 · 2026-09-21 · Un hijo sigue mutando hasta dejar de parecerse a su padre

**Contexto.** `speciation.clone_threshold` es 0.15 y una mutación puntual mueve
la distancia entre 0.04 y 0.14. Es decir: casi todo hijo de una sola mutación es
un casi-clon, y la incubadora lo rechazaba sin llegar a probarlo. Con
`TWEAK_PARAM` y `TWEAK_THRESHOLD` sumando el 55 % de los pesos, más de la mitad
de la descendencia por mutación se perdía antes de nacer.

**Decisión.** Tras aplicar sus 1-3 mutaciones, `mutate` mide la distancia al
padre y sigue mutando —hasta `MAX_EXTRA_MUTATIONS` veces más— mientras siga por
debajo del umbral de clon.

**Consecuencias.** La proporción de hijos rechazados por clon baja del 50 % al
4 %, y el operador de mutación vuelve a explorar de verdad. A cambio, una
mutación es un salto algo mayor de lo que la tabla de `docs/EVOLUTION.md`
sugiere: la distancia mediana al padre queda en 0.31. La alternativa era bajar
`clone_threshold`, pero ese umbral también protege al jardín de llenarse de
casi-repeticiones, y aflojarlo habría tenido un coste mucho mayor.

---

### D-021 · 2026-09-21 · El walk-forward desliza el train y reparte la validación por todo el histórico

**Contexto.** El diseño original decía "pliegues anclados: el train empieza
siempre al principio y crece". Implementado así sobre los siete años de
BTC/USDT, el resultado fue que **ni un solo genoma de 200 sembrados pasaba la
incubadora**, y los dos genomas de ejemplo del repositorio tampoco. Al mirarlo
de cerca había dos causas distintas:

1. Con el train anclado, el último pliegue corría seis años seguidos de una tirada.
   El freno de `risk.hard_max_drawdown` saltaba casi siempre en los primeros
   años, y a partir de ahí el bot quedaba abortado y los pliegues siguientes
   medían una curva plana. Además, comparar el Sortino de seis años con el de
   una validación de tres meses para calcular la degradación fuera de muestra no
   compara nada: son ventanas de escalas y regímenes distintos.
2. Las cinco ventanas de validación se apilaban contra el borde del holdout, así
   que las cinco caían dentro de los últimos quince meses —cuatro de ellos
   bajistas—. Exigir la **mediana** de los cinco pliegues dejaba de significar
   "funciona en mercados distintos" para significar "funciona en éste", y sobre
   spot sólo de largos eso era pedir que un bot ganara dinero en un año y medio
   de caídas.

**Decisión.** El train mide `incubator.train_bars` velas y **desliza**: cada
pliegue tiene su propia ventana de un año, no una acumulada desde 2019. Y las
ventanas de validación se **reparten a pasos iguales por todo el tramo
evaluable**, de la más antigua posible a la que toca el embargo del holdout.
Cada pliegue se corre además como un backtest independiente con capital fresco.
La propia configuración ya apuntaba a esto: `train_bars: 8760 # ~1 año`.

**Consecuencias.** Sobre BTC/USDT los cinco pliegues cubren ahora 2020, 2021,
2023, 2024 y 2026, con mercados al alza y a la baja, y la mediana vuelve a
significar robustez entre regímenes. La tasa de aprobación de genomas sembrados
al azar pasa de 0 de 200 a 5 de 200, que es un filtro exigente pero no
imposible. Un mal año ya no puede vaciar los pliegues siguientes. Coste: se
pierde la propiedad de "el train contiene toda la historia disponible", que en
un sistema que no ajusta parámetros no aportaba nada, y el coste de cómputo pasa
a ser `n_folds` backtests cortos por candidato en vez de uno largo.

---

### D-022 · 2026-09-21 · Un ensemble sí reescribe su genoma al morir un miembro

**Contexto.** El esquema dice que un bot tiene un genoma y no cambia: mutar
produce un bot nuevo, y de ahí que la genealogía signifique algo. Pero
`docs/EVOLUTION.md` §FUSION exige que, cuando muere un padre, el ensemble
reparta su peso entre los vivos.

**Decisión.** `BotRepository.reweight_ensemble` reescribe el `payload` de ese
genoma en sitio, y es el único camino del sistema que lo hace.

**Consecuencias.** La excepción está acotada a un método con nombre propio y a
un caso que no es evolución: un ensemble que pierde un miembro sigue siendo el
mismo organismo, no uno nuevo. Queda registrado como evento
`FUSION_REWEIGHTED`, de forma que el cambio es auditable aunque el genoma se
haya sobrescrito.

---

### D-023 · 2026-09-21 · El dashboard tiene una vista de cría, y no sólo de resultados

**Contexto.** `docs/DASHBOARD.md` describía seis vistas centradas en lo que el
jardín *es* ahora: población, genealogía, generaciones, especies, diario. La
incubadora ya escribía en `incubation_runs` todo lo que **no** llegó a nacer —
candidato, operador, padres, métricas por pliegue, motivo del rechazo — y nada
de eso se podía mirar sin abrir SQLite a mano.

**Decisión.** Se añade una séptima vista, *Cría*, y dos endpoints fuera de la
lista original de `DASHBOARD.md §API`:

```
GET /api/breeding            el embudo completo: concebidos → aprobados → nacidos → vivos
GET /api/incubation/{n}      la criba de una generación, candidato a candidato
```

Los motivos de rechazo se agrupan en categorías contables (`reject_category`)
porque el texto crudo trae números —"sortino -2.60 por debajo del umbral
0.86"— y como cadena literal cada rechazo sería su propia categoría.

**Consecuencias.** La mitad invisible de la evolución pasa a ser visible: se ve
qué operador produce clones, qué fracción de la cosecha muere en la criba y qué
forma de criar da bots que duran. El coste es que `DASHBOARD.md §API` ya no es
la lista completa de endpoints; queda anotada allí.

---

### D-024 · 2026-09-21 · El dashboard abre una conexión SQLite por hilo

**Contexto.** FastAPI atiende los endpoints síncronos en un pool de hilos y
SQLite prohíbe usar una conexión desde un hilo distinto del que la creó. Con
una sola conexión compartida, cualquier petición que no cayera en el hilo
principal moría con `ProgrammingError`.

**Decisión.** `dashboard/app.py` mantiene un `threading.local` con un
`DashboardAPI` —y por tanto un `Database`— por hilo, y las cierra todas al
apagar. No se toca `storage/db.py`: el motor es de un solo hilo y no necesita
pagar ese coste.

**Consecuencias.** Un puñado de conexiones de sólo lectura, no una por
petición, porque los hilos del pool se reutilizan. La caché de respuestas caras
sigue siendo única y compartida: son datos, no conexiones.

---

### D-025 · 2026-09-21 · `LineageGraph.to_dict` usaba `__dict__` sobre dataclasses con slots

**Contexto.** `GraphNode` y `GraphEdge` son `@dataclass(slots=True)` y no tienen
diccionario de instancia, así que `to_dict()` —escrito en el hito 4 y nunca
ejercitado— reventaba con `AttributeError` en cuanto el dashboard pidió el
grafo.

**Decisión.** Usar `dataclasses.asdict`. Es una corrección de un bug, no un
cambio de forma: la firma y el contenido del diccionario son los prometidos.

---

### D-026 · 2026-09-21 · El jardín vivo mide con su ventana, no con el histórico

**Contexto.** `Population.evolve_generation` calculaba las métricas de todos los
vivos con el walk-forward de la incubadora, porque hasta el hito 5 no había
ventana que cerrar. Con el jardín corriendo sí la hay, y la config ya decía qué
hacer con ella: `fitness.live_weight: 0.70`.

**Decisión.** `evolve_generation` acepta `window_metrics` y un `scope`
(`live` | `incubator`). El runner le pasa las métricas de las 168 velas
vividas, se guardan en `bot_metrics` con ámbito `live` y el fitness efectivo de
cada bot pasa a ser `0.30 · incubadora + 0.70 · vivo`, con las dos generaciones
de gracia de `effective_fitness`.

De paso, `_persist` deja escrito en `bots` el fitness **vigente** y no el del
día en que nació. Antes, un bot con veinte generaciones seguía mostrando en el
dashboard la nota que sacó en la incubadora.

**Consecuencias.** La selección sigue usando los valores en memoria, así que la
evolución no cambia; lo que cambia es lo que se lee. Y aparece la tensión de
D-030.

---

### D-027 · 2026-09-21 · En un dry-run, la incubadora sólo ve hasta hoy

**Contexto.** Al cerrar una generación durante un replay, la incubadora criba
a los candidatos contra el histórico. Si se le pasan las velas enteras, está
mirando el futuro del propio replay.

**Decisión.** El runner construye una incubadora nueva en cada cierre de
generación, con las velas cortadas en el tick actual.

**Consecuencias.** Un dry-run de seis meses vuelve a tener sentido como
validación: lo que nace en la generación 30 nació con la información que había
en la generación 30. Cuesta reconstruir la partición del walk-forward una vez
por generación, que frente a los backtests de la propia criba es ruido.

---

### D-028 · 2026-09-21 · El benchmark recibe el mismo capital que el jardín

**Contexto.** El jardín crece y encoge: cada nacimiento mete 1.000 USDT y cada
muerte los saca. Compararlo con un buy & hold de capital fijo no dice nada —
basta sembrar más bots para "ganarle".

**Decisión.** La cartera espejo compra unidades del mercado de cada bot cuando
nace, al precio de ese momento, y las devuelve cuando muere. El alfa compara
entonces dos carteras con las mismas entradas y salidas de dinero.

**Consecuencias.** El benchmark es multi-mercado sin esfuerzo: cada bot compra
el suyo. Al reanudar tras una caída se reparte el valor escrito entre los
mercados con la proporción actual, para que la curva no dé un salto.

---

### D-029 · 2026-09-21 · Multi-símbolo: el reloj lo marca el símbolo primario

**Contexto.** El jardín se diseñó multi-símbolo desde el día 1 y el genoma ya
llevaba su `MarketSpec`, pero el motor sólo sabía cargar una serie.

**Decisión.** El runner mantiene una serie por mercado y un único reloj, el del
símbolo primario. Cada serie trae una tabla de alineación que traduce el índice
del reloj al suyo; un bot cuyo mercado no tiene vela en ese momento no opera y
se queda valorado a su último cierre conocido. La criba la reparte
`MultiSymbolIncubator`, que manda cada candidato a la incubadora de su mercado
y compara clones sólo contra los vivos de ese mercado: dos genomas idénticos
sobre mercados distintos no son clones, son la misma idea puesta a prueba en
dos sitios.

**Consecuencias.** `keepgarden garden seed --symbols BTC/USDT,ETH/USDT,SOL/USDT`
reparte la población entre mercados y el jardín corre los tres a la vez. Un
mercado que abre más tarde que el primario (SOL no existía en 2020) sencillamente
no tiene ticks hasta que aparece. Lo que el reloj primario no ve, no ocurre:
si BTC tiene un hueco, esa hora no existe para nadie. Es una simplificación
consciente y el precio de tener un solo reloj.

---

### D-030 · 2026-09-21 · Cuestión abierta: 168 velas no dan 10 operaciones

**Contexto.** Dos decisiones cerradas chocan en cuanto el jardín vive de
verdad. `garden.ticks_per_generation: 168` (una semana) y
`fitness.min_trades: 10`. Medido sobre el dry-run de seis meses: la mediana de
operaciones por bot y ventana es **2,3**, y sólo 5 de 532 pares (bot,
generación) llegan a diez. El fitness vivo queda indefinido casi siempre, la
selección se queda con la nota de la incubadora y el jardín deja de evolucionar
por rendimiento real: 26 generaciones con 0 o 1 nacimientos.

**Decisión.** Resuelta en **D-031**: se mide el fitness vivo sobre una ventana
deslizante de varias generaciones, sin tocar ninguno de los dos parámetros.
Lo que sigue era el menú de opciones que se le planteó a Alex.

**Opciones, por orden de intrusión:**

1. Bajar `fitness.min_trades` a 3 o 4. Un cambio de una línea; el riesgo es
   juzgar con poca evidencia, que es justo lo que ese parámetro evita.
2. Subir `ticks_per_generation` a 500-700 (un mes). Generaciones más lentas
   pero con evidencia de verdad. Cambia el ritmo de todo el jardín.
3. Medir el fitness vivo sobre una **ventana deslizante de varias
   generaciones** en vez de sólo la última. Es la más correcta y la que más
   código toca.

La 3 es la que recomienda quien esto escribe; la 2 es la más barata de probar.
Alex eligió la 3.


---

### D-031 · 2026-09-21 · El fitness vivo se mide sobre una ventana deslizante

**Contexto.** Resuelve D-030. Con 168 velas por generación la mediana de
operaciones por bot es 2,3 y el fitness exige 10, así que casi nadie llegaba a
tener fitness vivo definido. Ni `ticks_per_generation` ni `min_trades` son
negociables: son decisiones cerradas y las dos tienen su razón.

**Decisión.** La ventana de medición del jardín vivo empieza en la generación
que se acaba de cerrar y **crece hacia atrás sólo hasta reunir `min_trades`
operaciones**, con el tope de `fitness.live_window_generations` (4 por
defecto). Un bot que opera mucho se sigue juzgando por su última semana; uno
selectivo, por el último mes. La curva de capital se concatena tal cual —es la
misma cartera— y las comisiones se cuentan desde el principio de la ventana
elegida.

El contador de inactividad **no** usa la ventana: mira sólo la generación que
se cierra. Si mirara la ventana entera, un bot parado desde hace tres semanas
seguiría contando como activo y no moriría nunca por inactivo. Por eso
`evolve_generation` recibe `activity` aparte de `window_metrics`.

**Consecuencias.** Medido sobre los mismos seis meses de dry-run, con la misma
semilla:

| | Antes (1 generación) | Ahora (hasta 4) |
|---|---|---|
| Pares (bot, generación) con fitness definido | 5 de 532 (0,9 %) | 235 de 500 (**47 %**) |
| Generaciones con mediana de fitness | 4 de 26 | **26 de 26** |
| Bots vivos con fitness vivo | 0 de 21 | **10 de 18** |
| Nacimientos / muertes | 28 / 44 | 37 / 54 |

El jardín pasa de no poder comparar a sus bots a seleccionarlos de verdad. El
coste es que la nota de un bot puede arrastrar hasta un mes de historia, que es
exactamente el punto: una semana no es evidencia.

---

### D-032 · 2026-09-21 · Los pesos de familia del jardinero llegan al motor

**Contexto.** `REBALANCE_QUOTAS` aceptaba `family_weights`, los validaba y los
guardaba en `garden_meta`… y no los leía nadie. Media función: el jardinero
proponía algo que no tenía ningún efecto.

**Decisión.** `Population` los lee una vez por generación y sesga con ellos la
elección de familia al sembrar. Sin pesos —lo normal— el muestreo sigue siendo
uniforme y con **exactamente la misma llamada** al `Random` sembrado que antes,
para que un jardín sin `REBALANCE_QUOTAS` salga idéntico byte a byte.

De paso se corrige una inversión de dependencias que se había colado: el motor
importaba `effective_config` de `gardener/`, que está por encima suyo en la
pirámide de docs/ARCHITECTURE.md §6. Esa función y las dos claves de
`garden_meta` viven ahora en `config.py`, que no depende de nadie.

**Consecuencias.** Queda una inversión conocida y asumida: `engine/runner.py`
importa `evolution/population.py` para cerrar generación. Es inherente al
contrato del hito 5 —el andamiaje puso el bucle en `engine/` y cerrar una
generación *es* evolución— y no se arregla moviendo el archivo, sino decidiendo
que el orquestador puede mirar hacia arriba. El resto de `engine/` sigue limpio.

---

### D-033 · 2026-09-21 · El estado vivo de cada cartera se guarda en `bot_runtime`

**Contexto.** El jardín presumía de poder morir y resucitar sin perder nada, y
el test que lo probaba pasaba… porque el corte nunca caía sobre una posición
abierta. Forzando un corte a mitad de una operación, la única que cruzaba el
corte se cerraba distinta:

| | entero | reanudado |
|---|---|---|
| `pnl_net` | 7,8187 | **8,1764** |
| `fees` | 0,7240 | **0,3663** |
| `holding_bars` | 14 | **10** |

La causa: la posición se reconstruía desde `trades`, que guarda lado, tamaño,
precio de entrada, stop y take, y **no** guarda lo que sólo vivía en memoria —
el ancla del trailing, el 1R, las velas aguantadas, la comisión de entrada, el
enfriamiento de la cartera ni la cola de órdenes del broker—. Reanudar perdía
la comisión de entrada (de ahí el PnL inflado) y reiniciaba el contador de
velas (de ahí el `EXIT_TIME` que llegaba tarde).

**Decisión.** Una tabla `bot_runtime` (bot_id, ts, payload JSON) con el estado
vivo completo de cada cartera: todas las posiciones con todos sus campos, la
cola de órdenes pendientes, el enfriamiento y las comisiones acumuladas. Se
reescribe en cada tick **dentro de la transacción del tick**, junto a las
órdenes y a `last_tick_ts`: o está el tick entero o no está. El diseño es el
que Alex tenía implementado en local; esto lo porta.

Con la cola de órdenes guardada tal cual, `_rebuild_pending_orders` —que la
aproximaba volviendo a decidir sobre la última vela— deja de hacer falta.
Sobrevive para un solo caso: un jardín anterior a esta decisión, que no tiene
estado vivo escrito. Ese arranque reconstruye lo que `trades` sabe y aproxima
el resto, una única vez, porque el primer tick ya escribe estado de verdad.

**Esquema.** `SCHEMA_VERSION` pasa a 2 y `migrate()` deja de ser un sello: como
todo `schema.sql` se crea con `IF NOT EXISTS`, volver a pasarlo entero es una
migración aditiva idempotente. Se aplica ahora también al abrir **sin crear**,
que es como abre `keepgarden run`; si no, el motor se encontraría con una tabla
que su código da por hecha. Los cambios destructivos seguirán necesitando su
script en `migrations/`.

**Consecuencias.** El coste por tick es una fila por bot vivo, del tamaño de
sus posiciones abiertas: con 60 bots, decenas de KB. El test de reanudación ya
no elige el corte a ojo: lo busca sobre una operación abierta y falla si no
encuentra ninguna, porque un test de reanudación que no reanuda nada no prueba
nada.
