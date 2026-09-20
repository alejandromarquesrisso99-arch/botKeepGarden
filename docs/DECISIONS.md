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
