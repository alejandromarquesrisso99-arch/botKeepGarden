# Arquitectura

## 1. La metáfora, tomada en serio

Un jardín no es una granja. En una granja siembras, cosechas y vacías el campo.
En un jardín hay plantas de edades distintas conviviendo, algunas llevan años,
otras acaban de brotar, y el jardinero poda, injerta y trasplanta sin parar.

botKeepGarden funciona así:

- **No hay "run" que termine.** El jardín corre indefinidamente.
- **Las generaciones se solapan.** Un bot de la generación 3 puede seguir vivo y
  reproduciéndose en la generación 40 si sigue rindiendo.
- **Morir es normal y barato.** La mayoría de los bots mueren jóvenes.
- **El jardinero entra de vez en cuando**, no en cada tick.

## 2. Los dos relojes

Este es el concepto central del sistema, y el que resuelve la tensión entre
"quiero datos reales en vivo" y "quiero ver muchas generaciones".

```
                      ┌──────────────────────────────────┐
                      │        CATÁLOGO DE GENES         │
                      │  indicadores, reglas, riesgo     │
                      └───────────────┬──────────────────┘
                                      │ siembra
                                      ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  RELOJ RÁPIDO — LA INCUBADORA                                 │
   │  Datos: histórico (2019 → hoy-embargo)                        │
   │  Una generación = una pasada de walk-forward completa         │
   │  Duración real: segundos                                      │
   │  Miles de genomas evaluados, seleccionados, cruzados          │
   │                                                               │
   │  Sale de aquí un bot sólo si:                                 │
   │    · supera el umbral de fitness in-sample                    │
   │    · NO se degrada en out-of-sample                           │
   │    · aporta diversidad (no es un clon de algo ya vivo)        │
   └───────────────────────────┬───────────────────────────────────┘
                               │ ascenso (promotion)
                               ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  RELOJ LENTO — EL JARDÍN VIVO                                 │
   │  Datos: velas 1h reales según se cierran, 24/7                │
   │  Un tick = una vela de 1h                                     │
   │  Una generación = 168 ticks (1 semana)                        │
   │  Dinero simulado, comisiones y slippage reales                │
   │                                                               │
   │  Aquí es donde se mide la verdad. El fitness del jardín vivo  │
   │  pesa mucho más que el de la incubadora.                      │
   └───────────────────────────┬───────────────────────────────────┘
                               │ los supervivientes vuelven
                               │ como padres de la siguiente cosecha
                               └──────────► (realimenta la incubadora)
```

**Por qué dos relojes.** Con velas de 1h, una generación de una semana da 168
observaciones. Evolucionar sólo ahí sería glacial: 50 generaciones = un año. La
incubadora hace el trabajo pesado de exploración sobre histórico en segundos, y
el jardín vivo hace de juez: un bot que brilló en backtest y se hunde en vivo es
la señal más valiosa del sistema, y esa señal se propaga hacia atrás penalizando
a su familia de ideas en la siguiente cosecha.

## 3. Ciclo de vida de un bot

```
              ┌─────────┐
              │ CONCEBIDO│  genoma creado por seed / mutación / cruce / fusión
              └────┬─────┘
                   │ validación estructural
                   ▼
              ┌──────────┐   falla el umbral
              │ INCUBANDO ├──────────────────► DESCARTADO (no llega a nacer)
              └────┬──────┘
                   │ pasa in-sample + out-of-sample + diversidad
                   ▼
              ┌──────────┐
              │ VIVO     │  opera en el jardín con datos reales
              └────┬─────┘
       ┌───────────┼───────────┬──────────────┐
       │           │           │              │
       ▼           ▼           ▼              ▼
  ┌─────────┐ ┌─────────┐ ┌─────────┐   ┌──────────┐
  │REPRODUCE│ │ FUSIONADO│ │ PODADO  │   │ JUBILADO │
  │(es padre│ │(sus genes│ │(rinde   │   │(el       │
  │ y sigue │ │ viven en │ │ mal o   │   │ jardinero│
  │ vivo)   │ │ un hijo) │ │ DD alto)│   │ lo retira│
  └─────────┘ └─────────┘ └─────────┘   └──────────┘
```

Un bot nunca se borra. Cambia de `status` y su historia completa permanece: es lo
que alimenta el árbol genealógico del dashboard.

## 4. Anatomía de un tick del jardín vivo

```
 vela 1h cierra en Binance
        │
        ▼
 [1] INGEST     data/sources.py descarga la vela cerrada; store.py la persiste
        │
        ▼
 [2] FEATURES   data/indicators.py actualiza los indicadores que la población
        │       necesita (se calculan una vez, se comparten entre bots)
        ▼
 [3] DECIDE     cada bot VIVO evalúa su genoma sobre el estado actual
        │       → Signal(NONE | ENTER_LONG | EXIT_LONG | ENTER_SHORT | EXIT_SHORT)
        ▼
 [4] RISK       risk gene traduce la señal a tamaño de orden; se aplican límites
        │       de exposición y de posiciones concurrentes
        ▼
 [5] EXECUTE    broker.py simula el fill en la APERTURA DE LA SIGUIENTE VELA,
        │       con slippage y comisión. Se registran los trades.
        ▼
 [6] MARK       portfolio.py revalora, aplica stops/trailing/time-stop
        │
        ▼
 [7] PERSIST    equity_snapshots + trades + bot_runtime + events
        │
        ▼
 [8] REFLEX     circuit breakers: un bot cuyo drawdown supera el límite duro se
                poda en el acto, sin esperar a fin de generación
```

Cada 168 ticks se dispara la **evaluación de generación** (§5). Cada N
generaciones (config) se genera un informe y se espera una **sesión de jardinero**.

## 5. Evaluación de generación

1. Cerrar la ventana: recoger trades y equity de los 168 ticks.
2. Calcular métricas por bot (`evaluation/metrics.py`).
3. Calcular fitness compuesto y frente de Pareto (`evaluation/fitness.py`).
4. Agrupar en especies por distancia genética y familia de ideas
   (`evolution/speciation.py`).
5. Repartir cupos de reproducción por especie (fitness compartido, para que una
   especie dominante no colonice el jardín).
6. Seleccionar padres (torneo dentro de especie + élite global).
7. Generar descendencia: mutación, cruce, fusión, injerto.
8. Mandar la descendencia a la incubadora; sólo los que pasan nacen.
9. Podar: los peores por fitness, los que llevan N generaciones sin operar, los
   que superan el drawdown máximo, los clones.
10. Escribir la fila de `generations` y los `events`.

## 6. Componentes y responsabilidades

| Módulo | Responsabilidad | No hace |
|---|---|---|
| `genome/` | Definir qué es un bot y traducirlo a señales | No sabe de dinero ni de tiempo real |
| `data/` | Velas e indicadores, con caché | No decide nada |
| `engine/` | El paso del tiempo, la ejecución y la cartera | No sabe de evolución |
| `evaluation/` | Convertir historia en números comparables | No selecciona |
| `evolution/` | Quién se reproduce, con quién, y quién muere | No mide |
| `gardener/` | Interfaz entre el jardín y el LLM | No ejecuta cambios por su cuenta |
| `storage/` | Persistencia y consulta | No contiene lógica de negocio |
| `dashboard/` | Leer y pintar | Nunca escribe en el jardín |

La regla de dependencias es unidireccional:

```
dashboard ─┐
gardener  ─┼─► evolution ─► evaluation ─► engine ─► data ─► genome ─► types
storage   ─┘
```

Nada de `genome/` importa de `engine/`. Nada de `engine/` importa de
`evolution/`. Si necesitas romper esto, el diseño está mal.

## 7. Persistencia

SQLite único en `state/db/garden.db`. Motivos: cero infraestructura, el
dashboard lee en paralelo sin problemas en modo WAL, y todo el jardín cabe en un
archivo que se puede copiar como snapshot.

Las velas no van en SQLite: van en Parquet bajo `state/cache/` particionadas por
símbolo y timeframe, porque son muchas y se leen en bloque.

Esquema completo en `src/keepgarden/storage/schema.sql`.

## 8. Concurrencia

El bucle del jardín es **un solo proceso, single-thread**. Con 1h de timeframe y
poblaciones de cientos de bots, no hay razón para complicarse: un tick entero
tarda milisegundos y hay 3.600 segundos entre velas.

La incubadora sí paraleliza con `multiprocessing` sobre genomas, porque ahí sí
hay miles de backtests. Cada worker recibe el genoma serializado y un array de
velas; devuelve métricas. Sin estado compartido.

El dashboard corre en su propio proceso (`uvicorn`) y abre la base en sólo
lectura. Nunca escribe.

## 9. Fallos y reinicios

El jardín debe poder morir y resucitar sin perder nada:

- Todo el estado vive en SQLite, no en memoria.
- Al arrancar, `runner.py` lee el último tick procesado y rellena el hueco de
  velas que se perdió mientras estaba caído (*catch-up*), procesándolas en orden.
- Un tick es idempotente: reprocesar la vela `t` no debe duplicar trades. La
  clave es `(bot_id, candle_ts)` única en `orders`.
- Lo que una cartera lleva en memoria y no cabe en `trades` —el ancla del
  trailing, el 1R, las velas aguantadas, la comisión de entrada, el
  enfriamiento y la cola de órdenes del broker— se escribe cada tick en
  `bot_runtime`, dentro de la misma transacción. Sin eso, una posición que
  cruza el reinicio se cierra con otras comisiones y otra duración: ver
  docs/DECISIONS.md D-033.
