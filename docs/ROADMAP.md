# Roadmap

Ocho hitos. Cada uno termina con **tests que pasan** y **un comando que hace algo
visible**. No se avanza sin cumplir los criterios de aceptación.

---

## Hito 0 — Cimientos  ✅ (hecho en el andamiaje)

Estructura, documentación, tipos, esquema de genoma, catálogo, esquema SQL,
config, CLI enrutado.

**Aceptación**
- `python -c "import keepgarden"` funciona.
- `pytest` pasa con los tests de contrato.
- `keepgarden --help` lista todos los comandos.

---

## Hito 1 — Datos  ✅

Descargar, cachear y servir velas sin huecos ni look-ahead.

- `data/sources.py`: cliente `ccxt` con paginación, rate limit y reanudación.
- `data/store.py`: Parquet particionado por año + manifiesto.
- `data/candles.py`: carga a `pandas`, validación de continuidad, detección de
  huecos y anomalías.
- Comando `keepgarden data backfill` y `keepgarden data status`.

**Aceptación**
- Backfill de `BTC/USDT 1h` desde 2019 completo y reanudable (matarlo a mitad y
  relanzarlo termina el trabajo).  ✅
- `data status` reporta 0 huecos no marcados.  ✅
- Test: la carga de un rango nunca devuelve una vela cuyo `close_time` sea
  futuro respecto al `now` simulado.  ✅

Añadido sobre lo previsto: `data/backfill.py`, que es donde viven los trabajos
que ejecuta la CLI. El backfill también rellena hacia atrás si se le pide más
historia de la que ya hay en caché.

---

## Hito 2 — Indicadores y compilación del genoma  ✅

Convertir un genoma en una serie de señales.

- `data/indicators.py`: todo el catálogo, vectorizado, causal, con caché y
  periodo de calentamiento declarado.
- `genome/compile.py`: genoma → `SignalSeries` sobre un DataFrame de velas.
- `genome/validate.py`: referencias, límites de profundidad y hojas, rangos.
- `genome/random_genome.py`: muestreo por familia con plantillas.

**Aceptación**
- Test de causalidad automático: para cada indicador, calcularlo sobre
  `velas[0:n]` y sobre `velas[0:n+50]` da valores idénticos en `[0:n]`.  ✅
  (26 indicadores × 2 juegos de parámetros)
- 1.000 genomas aleatorios compilan sin error y pasan la validación.  ✅
- Un genoma escrito a mano produce exactamente las señales esperadas sobre una
  serie sintética (test de oro).  ✅

Comando visible: `keepgarden genome sample` siembra bots nuevos y los describe;
`keepgarden genome show --genome <archivo>` lo valida, lo compila contra las
velas en caché y dice cuántas señales da y con qué frecuencia.

---

## Hito 3 — Motor de simulación  ✅

Que un genoma se convierta en una curva de capital creíble.

- `engine/broker.py`: fills en `open[t+1]`, slippage, comisiones, stops con
  convención pesimista, gaps.
- `engine/portfolio.py`: posiciones, sizing, stops/trailing/time-stop, equity.
- `engine/backtest.py`: correr un genoma sobre un rango y devolver trades +
  equity.
- `evaluation/metrics.py`: todas las métricas de `METRICS.md`.

**Aceptación**
- Test de no-look-ahead: un backtest sobre `velas[0:n]` da exactamente los mismos
  trades que el mismo backtest sobre `velas[0:n+500]` truncado a `n`.  ✅
- Test de fricción: con comisión 0 y slippage 0, una estrategia buy & hold
  reproduce el retorno del activo con error < 1e-9.  ✅
- Test de contabilidad: `equity_final == capital_inicial + suma(pnl_neto)`.  ✅
- `keepgarden backtest --genome examples/genome_trend.json` imprime métricas.  ✅

Sobre los siete años de BTC/USDT en caché, ese genoma de ejemplo hace 249
operaciones, gana un 13.8 % frente al +2077 % del buy & hold, y paga en
comisiones el 71 % de su beneficio bruto. El motor funciona; el bot no.

---

## Hito 4 — Incubadora y evolución

El motor genético completo, corriendo sobre histórico.

- `evaluation/walkforward.py`: splits anclados con embargo, holdout intocable.
- `evaluation/fitness.py`: z-score robusto, penalizaciones, Pareto.
- `evolution/mutation.py`, `crossover.py`, `fusion.py`, `selection.py`,
  `speciation.py`, `population.py`.
- `engine/incubator.py`: cribado con umbrales inflados por multiplicidad,
  paralelizado.
- `storage/`: repositorios completos.

**Aceptación**
- `keepgarden garden seed --size 60` crea 60 bots válidos y diversos
  (diversidad genética > 0.4).
- `keepgarden incubate --generations 20` corre sin intervención y la mediana de
  fitness mejora de forma medible frente a la generación 0.
- Test: un cruce nunca produce referencias rotas (fuzz sobre 5.000 cruces).
- Test: la fusión de dos padres con correlación negativa produce un hijo con
  `max_drawdown` menor que ambos padres sobre la misma ventana.
- Test: el holdout no se toca — un test que falla si algún código lee el rango
  de holdout fuera de `promote()`.
- El determinismo se verifica: misma semilla → mismo jardín, byte a byte.

---

## Hito 5 — El jardín vivo

El bucle continuo sobre datos reales.

- `engine/clock.py`: espera a velas cerradas, catch-up tras caídas.
- `engine/runner.py`: el bucle, los circuit breakers, el cierre de generación.
- Reanudación completa desde SQLite.
- Comando `keepgarden run` con `--dry-run` que simula el reloj acelerado.

**Aceptación**
- `keepgarden run --dry-run --speed 1000` recorre 6 meses de histórico como si
  fuera vivo y produce un jardín con al menos 25 generaciones.
- Matar el proceso a mitad y relanzarlo: continúa exactamente donde estaba, sin
  duplicar ni una operación.
- 48 horas de ejecución real contra Binance sin intervención.

---

## Hito 6 — Dashboard  ✅

Ver el jardín.

- `dashboard/app.py`, `api.py`, front completo.
- Las seis vistas de `DASHBOARD.md`, con la genealogía y su slider temporal como
  pieza central, más una séptima de **cría** (docs/DECISIONS.md D-023).

**Aceptación**
- `keepgarden dashboard` abre y pinta un jardín de 25 generaciones en < 2 s.  ✅
- El slider temporal reproduce la evolución generación a generación.  ✅
- Con 2.000 bots históricos la vista de genealogía sigue siendo usable.  ✅
  (por encima de `dashboard.graph_node_limit` se pliegan los linajes extintos)
- El dashboard funciona con el motor apagado.  ✅ abre la base en `mode=ro` y es
  un proceso aparte; un test comprueba que recorrer toda la API no modifica el
  archivo.

Sobre un jardín de 26 generaciones y 61 bots salido de `incubate`, la
genealogía pinta 61 nodos y 35 vínculos, y el slider en la generación 8 enseña
48 bots con los 21 que estaban vivos *entonces*, no los de hoy.

Lo que todavía se ve vacío —curva de capital del jardín, operaciones de un bot,
heatmap de correlación— es lo que escribe el hito 5: las cosechas de incubadora
criban genomas, no simulan cartera. Cada hueco lo dice en su sitio en vez de
enseñar un gráfico en blanco.

---

## Hito 7 — El jardinero

Cerrar el bucle con Claude dentro.

- `gardener/report.py`: el informe completo de `GARDENER_PROTOCOL.md`.
- `gardener/proposals.py`: validación y aplicación con límites.
- `gardener/journal.py`: el diario.
- Comandos `keepgarden report` y `keepgarden gardener apply`.
- Vista de diario en el dashboard.

**Aceptación**
- El informe de una generación real es legible y suficiente para decidir sin
  mirar la base de datos.
- Una propuesta que viola un límite se rechaza con un mensaje que explica cuál.
- Una sesión completa de jardinero queda registrada y su efecto se mide
  automáticamente `review_in_generations` después.

---

## Hito 8 — Endurecimiento

- Multi-símbolo real (`ETH/USDT`, `SOL/USDT`).
- Persistencia de snapshots del jardín (copias de `garden.db` fechadas).
- Informe de robustez: sensibilidad del fitness a comisiones, slippage y
  desplazamiento temporal del inicio.
- Prueba de Monte Carlo sobre el orden de las operaciones.
- Panel de salud del sistema: latencia del venue, huecos, tiempos de tick.

**Aceptación**
- El jardín corre 30 días seguidos sin intervención.
- El informe de robustez muestra que el mejor linaje sobrevive a duplicar la
  fricción.

---

## Fuera de alcance (por ahora)

- Dinero real. Es una decisión de Alex, no un hito de ingeniería.
- Futuros, apalancamiento, cortos reales.
- Modelos de ML pesados dentro del genoma. Si entra ML, entra como un *feature*
  más (por ejemplo, la salida de un modelo pequeño entrenado fuera y congelado),
  nunca como un modelo que se entrena dentro del bucle evolutivo.
- Ejecución en la nube o en un VPS.
