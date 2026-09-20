-- ==========================================================================
-- botKeepGarden — esquema de la base del jardín
--
-- SQLite en modo WAL. Un archivo = un jardín completo, copiable como snapshot.
-- El dashboard abre esta base en SÓLO LECTURA y nunca escribe.
--
-- Principio: toda la historia del jardín está aquí. Un bot nunca se borra,
-- cambia de estado. El dashboard no recalcula historia: la lee.
-- ==========================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

-- --------------------------------------------------------------------------
-- Metadatos del jardín
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS garden_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Claves esperadas: schema_version, created_at, seed, config_hash,
-- current_generation, last_tick_ts, status, venue, symbol, timeframe.

-- --------------------------------------------------------------------------
-- Genomas
--
-- Un bot tiene exactamente un genoma y no cambia: mutar produce un bot NUEVO.
-- Eso es lo que hace que la genealogía tenga sentido.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS genomes (
    genome_id       TEXT PRIMARY KEY,
    hash            TEXT NOT NULL,          -- SHA-256 de la estructura, sin meta ni id
    family          TEXT NOT NULL,
    venue           TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    is_ensemble     INTEGER NOT NULL DEFAULT 0,
    complexity      INTEGER NOT NULL,       -- n_features + n_hojas (parsimonia)
    n_features      INTEGER NOT NULL,
    max_depth       INTEGER NOT NULL,
    generation      INTEGER NOT NULL,       -- generación de creación
    operator        TEXT NOT NULL,          -- SEED | MUTATE | CROSSOVER | FUSION | GRAFT
    root_lineage    TEXT NOT NULL,
    payload         TEXT NOT NULL,          -- JSON canónico completo
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS ix_genomes_hash       ON genomes(hash);
CREATE INDEX IF NOT EXISTS ix_genomes_family     ON genomes(family);
CREATE INDEX IF NOT EXISTS ix_genomes_lineage    ON genomes(root_lineage);
CREATE INDEX IF NOT EXISTS ix_genomes_generation ON genomes(generation);

-- --------------------------------------------------------------------------
-- Bots
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bots (
    bot_id              TEXT PRIMARY KEY,
    genome_id           TEXT NOT NULL REFERENCES genomes(genome_id),
    name                TEXT NOT NULL,           -- legible: Zarza-Terca-41
    status              TEXT NOT NULL,           -- CONCEIVED|INCUBATING|DISCARDED|ALIVE|CULLED|RETIRED
    family              TEXT NOT NULL,
    root_lineage        TEXT NOT NULL,
    species_id          TEXT,                    -- reasignado cada generación
    born_generation     INTEGER NOT NULL,
    died_generation     INTEGER,
    death_cause         TEXT,                    -- ver types.DeathCause
    operator            TEXT NOT NULL,

    -- capital (cartera aislada por bot, ver DECISIONS D-004)
    initial_capital     REAL NOT NULL,
    equity              REAL NOT NULL,
    peak_equity         REAL NOT NULL,
    cash                REAL NOT NULL,

    -- protecciones
    protected_until_gen INTEGER NOT NULL DEFAULT 0,
    is_elite            INTEGER NOT NULL DEFAULT 0,
    on_pareto_front     INTEGER NOT NULL DEFAULT 0,

    -- fitness vigente
    fitness_incubator   REAL,
    fitness_live        REAL,
    fitness_effective   REAL,

    -- actividad
    generations_alive   INTEGER NOT NULL DEFAULT 0,
    idle_generations    INTEGER NOT NULL DEFAULT 0,
    total_trades        INTEGER NOT NULL DEFAULT 0,
    warmup_until_ts     INTEGER,                 -- no opera hasta que sus indicadores están calientes

    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS ix_bots_status   ON bots(status);
CREATE INDEX IF NOT EXISTS ix_bots_family   ON bots(family);
CREATE INDEX IF NOT EXISTS ix_bots_lineage  ON bots(root_lineage);
CREATE INDEX IF NOT EXISTS ix_bots_species  ON bots(species_id);
CREATE INDEX IF NOT EXISTS ix_bots_fitness  ON bots(fitness_effective DESC);
CREATE INDEX IF NOT EXISTS ix_bots_born     ON bots(born_generation);

-- --------------------------------------------------------------------------
-- Parentesco
--
-- Tabla aparte, no una columna: un bot fusionado tiene N padres, y la
-- genealogía del dashboard es exactamente esta tabla.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS parentage (
    child_id        TEXT NOT NULL REFERENCES bots(bot_id),
    parent_id       TEXT NOT NULL REFERENCES bots(bot_id),
    operator        TEXT NOT NULL,       -- MUTATE | CROSSOVER | FUSION | GRAFT
    weight          REAL NOT NULL DEFAULT 1.0,  -- peso del padre en una fusión
    ordinal         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (child_id, parent_id)
);

CREATE INDEX IF NOT EXISTS ix_parentage_parent ON parentage(parent_id);

-- --------------------------------------------------------------------------
-- Generaciones
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS generations (
    generation          INTEGER PRIMARY KEY,
    started_ts          INTEGER NOT NULL,       -- ms UTC de la primera vela
    ended_ts            INTEGER,
    n_ticks             INTEGER NOT NULL DEFAULT 0,

    -- demografía
    population_size     INTEGER NOT NULL DEFAULT 0,
    births              INTEGER NOT NULL DEFAULT 0,
    deaths              INTEGER NOT NULL DEFAULT 0,
    fusions             INTEGER NOT NULL DEFAULT 0,
    discarded           INTEGER NOT NULL DEFAULT 0,  -- no pasaron la incubadora
    n_species           INTEGER NOT NULL DEFAULT 0,
    oldest_bot_age      INTEGER NOT NULL DEFAULT 0,

    -- fitness
    fitness_best        REAL,
    fitness_p75         REAL,
    fitness_median      REAL,
    fitness_p25         REAL,
    fitness_worst       REAL,

    -- diversidad
    genetic_diversity   REAL,
    family_shares       TEXT,                   -- JSON {familia: fracción}
    lineage_shares      TEXT,                   -- JSON {linaje: fracción}

    -- capital
    garden_equity       REAL,
    benchmark_equity    REAL,
    garden_alpha        REAL,
    garden_drawdown     REAL,

    -- parámetros efectivos usados en esta generación (mutación adaptativa)
    effective_params    TEXT,                   -- JSON
    best_bot_id         TEXT REFERENCES bots(bot_id),
    closed_at           TEXT
);

-- --------------------------------------------------------------------------
-- Métricas por bot y generación
--
-- Una fila por (bot, generación). Es lo que alimenta las series del dashboard
-- y el histórico de fitness.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bot_metrics (
    bot_id              TEXT NOT NULL REFERENCES bots(bot_id),
    generation          INTEGER NOT NULL REFERENCES generations(generation),
    scope               TEXT NOT NULL DEFAULT 'live',  -- live | incubator | holdout

    -- retorno
    total_return        REAL,
    cagr                REAL,
    avg_trade_return    REAL,
    expectancy          REAL,

    -- riesgo
    max_drawdown        REAL,
    ulcer_index         REAL,
    downside_dev        REAL,
    time_in_market      REAL,
    worst_trade         REAL,

    -- ajustadas
    sharpe              REAL,
    sortino             REAL,
    calmar              REAL,
    martin              REAL,
    profit_factor       REAL,

    -- comportamiento
    n_trades            INTEGER,
    win_rate            REAL,
    turnover            REAL,
    avg_holding_bars    REAL,
    fee_drag            REAL,
    consistency         REAL,

    -- relacionales
    corr_to_population  REAL,
    corr_to_benchmark   REAL,
    novelty             REAL,

    -- resultado
    fitness             REAL,
    fitness_rank        INTEGER,
    on_pareto_front     INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (bot_id, generation, scope)
);

CREATE INDEX IF NOT EXISTS ix_metrics_gen     ON bot_metrics(generation, scope);
CREATE INDEX IF NOT EXISTS ix_metrics_fitness ON bot_metrics(generation, fitness DESC);

-- --------------------------------------------------------------------------
-- Órdenes y operaciones
--
-- `orders` es el registro de intenciones ejecutadas; el índice único sobre
-- (bot_id, candle_ts, kind) hace idempotente el reprocesado de una vela tras
-- un reinicio. Ver docs/ARCHITECTURE.md §9.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS orders (
    order_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id          TEXT NOT NULL REFERENCES bots(bot_id),
    trade_id        INTEGER,
    candle_ts       INTEGER NOT NULL,       -- vela que generó la decisión
    fill_ts         INTEGER NOT NULL,       -- vela en la que se rellenó (t+1)
    kind            TEXT NOT NULL,          -- ENTRY | EXIT_* (ver types.OrderKind)
    side            TEXT NOT NULL,          -- LONG | SHORT
    price           REAL NOT NULL,          -- precio de fill, ya con slippage
    reference_price REAL NOT NULL,          -- precio teórico sin slippage
    slippage        REAL NOT NULL,
    amount          REAL NOT NULL,
    notional        REAL NOT NULL,
    fee             REAL NOT NULL,
    UNIQUE (bot_id, candle_ts, kind)
);

CREATE INDEX IF NOT EXISTS ix_orders_bot   ON orders(bot_id, fill_ts);
CREATE INDEX IF NOT EXISTS ix_orders_trade ON orders(trade_id);

CREATE TABLE IF NOT EXISTS trades (
    trade_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id          TEXT NOT NULL REFERENCES bots(bot_id),
    generation      INTEGER NOT NULL,
    side            TEXT NOT NULL,
    open_ts         INTEGER NOT NULL,
    close_ts        INTEGER,
    open_price      REAL NOT NULL,
    close_price     REAL,
    amount          REAL NOT NULL,
    stop_price      REAL,
    take_price      REAL,
    exit_kind       TEXT,                   -- por qué se cerró
    holding_bars    INTEGER,
    pnl_gross       REAL,
    pnl_net         REAL,
    fees            REAL NOT NULL DEFAULT 0,
    return_pct      REAL,
    r_multiple      REAL,                   -- pnl / riesgo inicial
    is_open         INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS ix_trades_bot  ON trades(bot_id, open_ts);
CREATE INDEX IF NOT EXISTS ix_trades_gen  ON trades(generation);
CREATE INDEX IF NOT EXISTS ix_trades_open ON trades(is_open) WHERE is_open = 1;

-- --------------------------------------------------------------------------
-- Curvas de capital
--
-- Una fila por (bot, vela). Es la tabla que más crece: 60 bots x 8760 velas al
-- año = ~525k filas/año. Perfectamente manejable en SQLite.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS equity_snapshots (
    bot_id          TEXT NOT NULL REFERENCES bots(bot_id),
    ts              INTEGER NOT NULL,
    equity          REAL NOT NULL,
    cash            REAL NOT NULL,
    position_value  REAL NOT NULL DEFAULT 0,
    drawdown        REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (bot_id, ts)
);

CREATE INDEX IF NOT EXISTS ix_equity_ts ON equity_snapshots(ts);

-- Curva agregada del jardín y del benchmark. Se escribe una vez por tick.
CREATE TABLE IF NOT EXISTS garden_equity (
    ts                  INTEGER PRIMARY KEY,
    generation          INTEGER NOT NULL,
    garden_equity       REAL NOT NULL,
    benchmark_equity    REAL NOT NULL,
    n_alive             INTEGER NOT NULL,
    n_open_positions    INTEGER NOT NULL DEFAULT 0,
    garden_drawdown     REAL NOT NULL DEFAULT 0
);

-- --------------------------------------------------------------------------
-- Especies
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS species (
    species_id          TEXT NOT NULL,
    generation          INTEGER NOT NULL,
    representative_id   TEXT REFERENCES bots(bot_id),
    dominant_family     TEXT,
    size                INTEGER NOT NULL,
    mean_fitness        REAL,
    shared_fitness      REAL,
    breeding_quota      INTEGER NOT NULL DEFAULT 0,
    mean_age            REAL,
    PRIMARY KEY (species_id, generation)
);

-- Distancias genéticas cacheadas, para el scatter del dashboard.
CREATE TABLE IF NOT EXISTS genetic_distances (
    generation      INTEGER NOT NULL,
    bot_a           TEXT NOT NULL,
    bot_b           TEXT NOT NULL,
    distance        REAL NOT NULL,
    PRIMARY KEY (generation, bot_a, bot_b)
);

-- --------------------------------------------------------------------------
-- Eventos — la historia completa del jardín
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,       -- ms UTC
    generation      INTEGER,
    type            TEXT NOT NULL,          -- ver types.EventType
    bot_id          TEXT,
    severity        TEXT NOT NULL DEFAULT 'info',   -- info | warn | critical
    summary         TEXT NOT NULL,          -- una línea, legible
    payload         TEXT                    -- JSON con el detalle
);

CREATE INDEX IF NOT EXISTS ix_events_ts   ON events(ts DESC);
CREATE INDEX IF NOT EXISTS ix_events_type ON events(type, ts DESC);
CREATE INDEX IF NOT EXISTS ix_events_bot  ON events(bot_id, ts DESC);

-- --------------------------------------------------------------------------
-- Alertas activas
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS alerts (
    alert_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,          -- ver types.AlertKind
    raised_ts       INTEGER NOT NULL,
    raised_gen      INTEGER,
    cleared_ts      INTEGER,
    detail          TEXT,
    value           REAL,
    threshold       REAL
);

CREATE INDEX IF NOT EXISTS ix_alerts_open ON alerts(cleared_ts) WHERE cleared_ts IS NULL;

-- --------------------------------------------------------------------------
-- El jardinero
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS gardener_sessions (
    session_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    generation      INTEGER NOT NULL,
    opened_ts       INTEGER NOT NULL,
    closed_ts       INTEGER,
    trigger         TEXT NOT NULL,          -- scheduled | alert:<kind>
    report_path     TEXT,
    journal_entry   TEXT                    -- la narración, en prosa
);

CREATE TABLE IF NOT EXISTS gardener_decisions (
    decision_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id              INTEGER NOT NULL REFERENCES gardener_sessions(session_id),
    generation              INTEGER NOT NULL,
    kind                    TEXT NOT NULL,      -- ver types.ProposalKind
    status                  TEXT NOT NULL,      -- PENDING | APPLIED | REJECTED | REVIEWED
    target                  TEXT,               -- bot_id, linaje, familia o parámetro
    payload                 TEXT NOT NULL,      -- JSON con la propuesta completa
    rationale               TEXT NOT NULL,      -- obligatorio
    expected_effect         TEXT NOT NULL,      -- obligatorio, en métricas concretas
    review_in_generations   INTEGER NOT NULL,   -- obligatorio
    rejection_reason        TEXT,

    -- se rellena automáticamente al llegar la generación de revisión
    reviewed_generation     INTEGER,
    observed_effect         TEXT,
    verdict                 TEXT                -- worked | no_effect | backfired
);

CREATE INDEX IF NOT EXISTS ix_decisions_session ON gardener_decisions(session_id);
CREATE INDEX IF NOT EXISTS ix_decisions_review
    ON gardener_decisions(status, reviewed_generation);

-- --------------------------------------------------------------------------
-- Incubadora — el registro de lo que NO nació también es información
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS incubation_runs (
    run_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    generation          INTEGER NOT NULL,
    genome_id           TEXT NOT NULL,
    genome_hash         TEXT NOT NULL,
    family              TEXT NOT NULL,
    operator            TEXT NOT NULL,
    parents             TEXT,                   -- JSON
    passed              INTEGER NOT NULL,
    reject_reason       TEXT,
    median_sortino      REAL,
    median_drawdown     REAL,
    median_trades       REAL,
    oos_decay           REAL,
    threshold_used      REAL,                   -- umbral inflado por multiplicidad
    n_candidates        INTEGER,                -- tamaño de la cosecha
    fold_metrics        TEXT,                   -- JSON por fold
    duration_ms         INTEGER
);

CREATE INDEX IF NOT EXISTS ix_incubation_gen ON incubation_runs(generation, passed);

-- Holdout: se escribe UNA vez por bot, al ascender. Si hay una segunda fila
-- para el mismo bot, alguien está mirando el holdout más de una vez: es un bug.
CREATE TABLE IF NOT EXISTS holdout_results (
    bot_id          TEXT PRIMARY KEY REFERENCES bots(bot_id),
    generation      INTEGER NOT NULL,
    evaluated_ts    INTEGER NOT NULL,
    sortino         REAL,
    max_drawdown    REAL,
    n_trades        INTEGER,
    decay           REAL,
    promoted        INTEGER NOT NULL
);

-- --------------------------------------------------------------------------
-- Calidad de datos
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS data_gaps (
    venue           TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    start_ts        INTEGER NOT NULL,
    end_ts          INTEGER NOT NULL,
    n_missing       INTEGER NOT NULL,
    filled          INTEGER NOT NULL DEFAULT 0,
    note            TEXT,
    PRIMARY KEY (venue, symbol, timeframe, start_ts)
);

CREATE TABLE IF NOT EXISTS data_anomalies (
    anomaly_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    venue           TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    ts              INTEGER NOT NULL,
    kind            TEXT NOT NULL,          -- price_jump | bad_ohlc | negative_volume
    detail          TEXT
);

-- --------------------------------------------------------------------------
-- Vistas de conveniencia para el dashboard
-- --------------------------------------------------------------------------

CREATE VIEW IF NOT EXISTS v_alive_bots AS
SELECT b.*, g.payload AS genome_payload, g.hash AS genome_hash
FROM bots b
JOIN genomes g ON g.genome_id = b.genome_id
WHERE b.status = 'ALIVE';

CREATE VIEW IF NOT EXISTS v_lineage_edges AS
SELECT p.parent_id            AS source,
       p.child_id             AS target,
       p.operator             AS operator,
       p.weight               AS weight,
       cb.born_generation     AS generation
FROM parentage p
JOIN bots cb ON cb.bot_id = p.child_id;

CREATE VIEW IF NOT EXISTS v_open_alerts AS
SELECT * FROM alerts WHERE cleared_ts IS NULL ORDER BY raised_ts DESC;

CREATE VIEW IF NOT EXISTS v_pending_reviews AS
SELECT d.*
FROM gardener_decisions d
WHERE d.status = 'APPLIED' AND d.reviewed_generation IS NULL;
