# CLAUDE.md — guía operativa de botKeepGarden

Este archivo es para ti, Claude Code. Léelo entero antes de tocar nada.

## Qué es esto

**botKeepGarden** es un *jardín de bots de trading*. No es un bot. Es un ecosistema
donde nacen, compiten, se reproducen, se fusionan y mueren muchas estrategias, de
forma continua, sobre datos reales de mercado.

Tú tienes dos papeles y no debes confundirlos:

1. **Ingeniero**: construyes y mantienes el motor (código de este repo).
2. **Jardinero**: una vez el motor corre, entras periódicamente a leer informes de
   generación, diagnosticar el estado del jardín y proponer cambios evolutivos.
   Ese papel está especificado en `docs/GARDENER_PROTOCOL.md` y **no es código**:
   son decisiones registradas en la base de datos.

Mientras estés construyendo, eres ingeniero. No inventes estrategias todavía.

## Decisiones ya cerradas (no las reabras sin preguntar)

| Decisión | Valor |
|---|---|
| Mercado | Cripto spot, 24/7 |
| Venue de datos | Binance vía `ccxt` (público, sin claves) |
| Timeframe operativo | Velas de 1 hora (cerradas) |
| Símbolo semilla | `BTC/USDT` (el jardín se diseña multi-símbolo desde el día 1) |
| Representación del bot | **Genoma de datos**, no código generado |
| Motor evolutivo | Determinista y barato; el LLM entra sólo como jardinero |
| Ejecución | **Paper trading**. `live` existe como interfaz pero está bloqueado |
| Persistencia | SQLite (`state/db/garden.db`) |
| Lenguaje | Python 3.11+ |
| Dashboard | FastAPI + HTML/JS vanilla, local (`localhost:8756`) |
| SO del usuario | Windows + PowerShell |

## Invariantes que NO puedes romper

1. **Un bot es un genoma serializable.** Si un bot no se puede volcar a JSON y
   reconstruir idéntico, está mal. Nada de `lambda`, closures ni código generado
   dentro de un genoma.
2. **Nada de look-ahead.** Una decisión en la vela `t` sólo puede leer datos
   cerrados hasta `t`. La ejecución ocurre en la apertura de `t+1`. Cualquier
   indicador que use `t+1` es un bug crítico.
3. **El holdout no se toca.** El tramo de datos marcado como `holdout` en la
   config jamás participa en selección, ranking ni ajuste de parámetros. Sólo se
   mira para confirmar o desmentir, una vez, al ascender un bot.
4. **Comisiones y slippage siempre.** Ningún backtest ni simulación puede correr
   con fricción cero. Los valores viven en `config/garden.yaml`.
5. **Todo evento es persistente y auditable.** Nacimiento, mutación, fusión,
   muerte, ascenso, trade y decisión del jardinero se escriben en la tabla
   `events` con su `payload` JSON. El dashboard nunca calcula historia: la lee.
6. **`live` está prohibido.** `execution.mode: live` debe abortar con un error
   explícito hasta que el usuario lo habilite a mano en el código. No lo
   desbloquees por iniciativa propia.
7. **Determinismo.** Todo lo aleatorio pasa por el `Random` sembrado que se
   inyecta. Dos ejecuciones con la misma semilla y los mismos datos dan el mismo
   jardín.

## Cómo está organizado el repo

```
src/keepgarden/
  types.py        enums y tipos base compartidos
  config.py       carga y valida config/garden.yaml
  ids.py          identidad de los bots (id + nombre legible)
  genome/         qué ES un bot: esquema, catálogo de genes, compilación a señal
  data/           velas: descarga, caché, indicadores (backfill.py = los
                  trabajos que ejecuta `keepgarden data ...`)
  engine/         cómo VIVE un bot: reloj, broker simulado, cartera, bucle
  evaluation/     cómo se MIDE: métricas, fitness, walk-forward
  evolution/      cómo se REPRODUCE: mutación, cruce, fusión, selección, especies
  gardener/       cómo entras TÚ: informes, propuestas, diario
  storage/        SQLite: esquema y repositorios
  dashboard/      FastAPI + front
  cli.py          `keepgarden <comando>`
```

## Estado del código

Los módulos marcados abajo con 🔨 son **contratos**: tienen firma, tipos y
docstring que describen exactamente el comportamiento esperado, y levantan
`NotImplementedError`. Tu trabajo es rellenarlos en el orden del roadmap.

Los marcados con ✅ ya están implementados y funcionando. Puedes extenderlos,
pero cambiar su forma rompe el resto: si necesitas hacerlo, anótalo en
`docs/DECISIONS.md` primero.

```
✅ types.py, config.py, ids.py
✅ genome/schema.py, genome/catalog.py, genome/serialize.py
✅ evolution/lineage.py
✅ gardener/proposals.py
✅ storage/schema.sql
✅ cli.py (enrutado completo; `data backfill`, `data status`, `genome sample`,
   `genome show`, `backtest`, `config` y `catalog` hacen trabajo de verdad)
✅ data/candles.py, data/store.py, data/sources.py, data/backfill.py  ← hito 1
✅ data/indicators.py, genome/compile.py, genome/validate.py,
   genome/random_genome.py  ← hito 2
✅ engine/broker.py, engine/portfolio.py, engine/backtest.py,
   evaluation/metrics.py  ← hito 3
✅ genome/distance.py, evaluation/walkforward.py, evaluation/fitness.py,
   evolution/{mutation,crossover,fusion,selection,speciation,population}.py,
   engine/incubator.py, storage/db.py, storage/repositories.py  ← hito 4
✅ dashboard/api.py, dashboard/app.py, dashboard/static/*  ← hito 6
   (`keepgarden dashboard`; se hizo antes que el hito 5 porque la evolución y la
   cría ya se podían mirar con lo que escribe `incubate`)
✅ engine/clock.py, engine/runner.py  ← hito 5 (`keepgarden run`)
✅ gardener/report.py, gardener/apply.py, gardener/journal.py  ← hito 7
   (`keepgarden report`, `keepgarden gardener apply`)
✅ evaluation/robustness.py, multi-símbolo, snapshots, panel de salud  ← hito 8
   (`keepgarden robustness`, `garden seed --symbols`)

```

No queda ningún contrato con `NotImplementedError`. Lo único pendiente del
roadmap es lo que necesita tiempo de reloj y red: 48 horas contra Binance y 30
días seguidos de jardín.

El fitness vivo se mide sobre una ventana deslizante de varias generaciones
(D-030 y **D-031**): es lo que hace que el jardín pueda comparar a sus bots con
generaciones de una semana. Si tocas `_window_metrics` en `engine/runner.py`,
lee esas dos entradas antes.

**Reanudar tiene que dar el mismo jardín que no haberse caído** (invariante 7).
Eso no sale gratis: hay estado vivo que se persiste (`bot_runtime`, el pico del
jardín, el azar de la evolución) y estado que se reconstruye al arrancar desde
`equity_snapshots`, `trades` y `species`. Si añades algo que `Population` o
`_BotState` arrastren entre generaciones, tiene que sobrevivir al reinicio o
romperás el invariante en silencio: **D-033, D-034 y D-035**.

## Orden de trabajo

Sigue `docs/ROADMAP.md`. Está dividido en hitos con **criterios de aceptación
verificables**. No pases al siguiente hito sin cumplir el anterior. Cada hito
debería terminar con tests que pasan y un comando de CLI que hace algo visible.

## Reglas de trabajo

- **Escribe el test antes que la implementación** en todo lo que sea matemática
  financiera (métricas, fitness, PnL, sizing). Son los sitios donde un error
  silencioso cuesta meses.
- **Un commit por unidad conceptual.** Mensajes en español, imperativo.
  La excepción, aprendida a base de romperlo: cuando dos unidades salen de la
  misma investigación y viven en las mismas funciones, separarlas a posteriori
  significa reconstruir a mano un estado intermedio que nunca existió. Eso
  produce un commit que parece verde y no lo está, que es peor que un historial
  poco fino. En ese caso, **un solo commit con un mensaje que nombre las dos
  unidades y sus entradas de `DECISIONS.md`**. La prueba de si separar o no:
  ¿puedo dejar el árbol en el primer commit y pasar la suite entera sin tocar
  nada más? Si no, va junto.
- **No añadas dependencias** sin anotarlas en `docs/DECISIONS.md` con el motivo.
  El stack objetivo es: `ccxt`, `pandas`, `numpy`, `pydantic`, `fastapi`,
  `uvicorn`, `pyyaml`, `typer`, `rich`, `pyarrow`. Nada de frameworks de backtest
  de terceros: el motor es nuestro porque el jardín necesita control fino del
  ciclo de vida.
- **No optimices antes de tiempo.** El cuello de botella real será la incubadora
  (miles de backtests). Ese es el único sitio donde la vectorización importa.
- **Si algo del diseño no te cuadra, pregunta.** Es mejor una pregunta que un
  ecosistema que evoluciona hacia el sitio equivocado durante tres semanas.

## Comandos

```powershell
# preparar el entorno (una vez)
.\scripts\bootstrap.ps1

# descargar histórico
keepgarden data backfill --symbol BTC/USDT --timeframe 1h --since 2019-01-01

# sembrar la población inicial
keepgarden garden seed --size 60

# correr una cosecha de incubadora (evolución rápida sobre histórico)
keepgarden incubate --generations 20

# arrancar el jardín vivo (bucle continuo)
keepgarden run

# dashboard
keepgarden dashboard

# informe de generación para el jardinero
keepgarden report --generation latest
```

## Contexto del usuario

Alex trabaja en Windows con PowerShell y usa Claude Code desde la app de
escritorio. Le interesa especialmente **ver la evolución**: el árbol genealógico,
las curvas de capital por generación y qué linajes ganan. El dashboard no es un
extra: es la mitad del producto.
