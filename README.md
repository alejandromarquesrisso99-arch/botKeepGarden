# botKeepGarden

Un **jardín de bots de trading**. No es un bot: es un ecosistema donde muchas
estrategias nacen, compiten, se reproducen, se fusionan y mueren, de forma
continua, sobre datos reales de mercado.

```
        catálogo de genes
               │
               ▼
     ┌──────────────────┐   ascenso   ┌──────────────────┐
     │   INCUBADORA     │ ──────────► │   JARDÍN VIVO    │
     │  histórico       │             │  BTC/USDT 1h     │
     │  walk-forward    │ ◄────────── │  24/7, paper     │
     │  miles de gens   │  los que     │  1 gen = 1 semana│
     └──────────────────┘  sobreviven └──────────────────┘
                                              │
                                              ▼
                                       dashboard local
                                    (genealogía, equity,
                                     generaciones, diario)
```

## Qué hace

- **Nacen**: genomas nuevos se muestrean del catálogo o se crean por mutación,
  cruce, fusión o injerto.
- **Compiten**: cada bot opera con su propia cartera simulada sobre velas reales
  de 1h, con comisiones y slippage.
- **Se fusionan**: dos o más bots descorrelacionados producen un hijo *ensemble*
  que combina sus señales — y los padres siguen vivos, para poder medir si la
  fusión aporta algo.
- **Heredan**: todo bot guarda su pedigrí. El árbol genealógico completo es
  navegable en el dashboard.
- **Mueren**: por fitness bajo, por drawdown, por inactividad o por ser clones.
- **Un jardinero los guía**: cada cierto tiempo Claude lee un informe del estado
  del ecosistema, diagnostica y propone cambios dentro de límites estrictos.

## Estado

Los ocho hitos de `docs/ROADMAP.md` están cerrados. No queda ningún módulo sin
implementar.

- **Hito 0 — Cimientos.**
- **Hito 1 — Datos.** Descarga, caché en Parquet y auditoría de velas.
  `BTC/USDT` en 1h, 4h y 1d desde 2019, reanudable y sin look-ahead.
- **Hito 2 — Genoma vivo.** 26 indicadores causales y cacheados, compilación de
  un genoma a señales y siembra sesgada por familia de ideas.
- **Hito 3 — Motor de simulación.** Broker con fricción y convención pesimista,
  cartera con sizing y stops, backtest sin look-ahead y todas las métricas.
- **Hito 4 — Incubadora y evolución.** Walk-forward con holdout blindado,
  fitness con penalizaciones, mutación, cruce, fusión, especiación y poda.
- **Hito 5 — El jardín vivo.** `keepgarden run`, con reanudación exacta tras una
  caída y un dry-run que produce las mismas operaciones que el backtest.
- **Hito 6 — Dashboard.** Ocho vistas, con la genealogía y su slider temporal
  como pieza central.
- **Hito 7 — El jardinero.** Informe, propuestas validadas contra límites,
  revisión automática del efecto y diario.
- **Hito 8 — Endurecimiento.** Multi-símbolo real, informe de robustez con
  Monte Carlo, copias del jardín y panel de salud.

Lo único pendiente del roadmap es lo que necesita tiempo de reloj: 48 horas
contra Binance y 30 días seguidos de jardín vivo.

**Esto opera con dinero simulado.** El modo `live` está bloqueado por código a
propósito.

## Arranque

```powershell
.\scripts\bootstrap.ps1          # entorno virtual e instalación editable
keepgarden config                # valida la configuración
keepgarden catalog               # qué genes existen

keepgarden data backfill --symbol BTC/USDT --timeframe 1h --since 2019-01-01 --context
keepgarden data status           # rango, huecos y anomalías de la caché

keepgarden genome sample --family BREAKOUT --count 3
keepgarden genome show --genome examples/genome_trend.json

keepgarden backtest --genome examples/genome_trend.json --from 2023-01-01

keepgarden garden seed --size 60      # siembra la población inicial
keepgarden incubate --generations 20  # evolución rápida sobre histórico
keepgarden run --dry-run              # el jardín vivo, acelerado sobre 6 meses
keepgarden dashboard                  # http://localhost:8756
```

La guía completa, de un ordenador en blanco a una sesión de jardinero, está en
**[docs/GUIA_USUARIO.md](docs/GUIA_USUARIO.md)**.

## Documentación

| Documento | Qué cuenta |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Guía operativa para Claude Code: invariantes y reglas de trabajo |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Los dos relojes, ciclo de vida, anatomía de un tick |
| [docs/GENOME.md](docs/GENOME.md) | Qué es un bot, como estructura de datos |
| [docs/EVOLUTION.md](docs/EVOLUTION.md) | Operadores, selección, diversidad, anti-sobreajuste |
| [docs/DATA.md](docs/DATA.md) | Velas, huecos, indicadores causales |
| [docs/EXECUTION.md](docs/EXECUTION.md) | Fills, fricción, circuit breakers |
| [docs/METRICS.md](docs/METRICS.md) | Métricas y fórmula de fitness |
| [docs/GARDENER_PROTOCOL.md](docs/GARDENER_PROTOCOL.md) | Qué puede y qué no puede hacer el jardinero |
| [docs/DASHBOARD.md](docs/DASHBOARD.md) | Las ocho vistas |
| [docs/GUIA_USUARIO.md](docs/GUIA_USUARIO.md) | Cómo se usa esto, de principio a fin |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Ocho hitos con criterios de aceptación |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Por qué las cosas son como son |

## Aviso

Esto es un proyecto de investigación sobre sistemas evolutivos aplicados a
series financieras. Nada de lo que produzca es asesoramiento financiero, y un
backtest —por honesto que sea— no es una predicción.
