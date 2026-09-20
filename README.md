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

Andamiaje completo: arquitectura, documentación, modelo de datos del genoma,
catálogo de genes, esquema de base de datos, configuración y CLI. El motor está
especificado como contratos y se implementa siguiendo `docs/ROADMAP.md`.

**Esto opera con dinero simulado.** El modo `live` está bloqueado por código a
propósito.

## Arranque

```powershell
.\scripts\bootstrap.ps1          # entorno virtual e instalación editable
keepgarden config                # valida la configuración
keepgarden catalog               # qué genes existen
```

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
| [docs/DASHBOARD.md](docs/DASHBOARD.md) | Las seis vistas |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Ocho hitos con criterios de aceptación |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Por qué las cosas son como son |

## Aviso

Esto es un proyecto de investigación sobre sistemas evolutivos aplicados a
series financieras. Nada de lo que produzca es asesoramiento financiero, y un
backtest —por honesto que sea— no es una predicción.
