"""Interfaz de línea de comandos de botKeepGarden.

El enrutado está completo: ``keepgarden --help`` lista todos los comandos desde
el primer día. Los que dependen de contratos aún sin implementar avisan de qué
hito los cubre en vez de reventar con un traceback.

Se usa ``argparse`` a propósito: es stdlib, no añade dependencias y sobra para
lo que hace falta.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from . import __version__

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd

    from .config import Config
    from .data.sources import VenueClient
    from .genome.catalog import GeneCatalog
    from .genome.schema import MarketSpec
    from .types import Timeframe

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_IMPLEMENTED = 3


class PendingMilestone(NotImplementedError):
    """Comando cuyo contrato todavía no está implementado."""

    def __init__(self, milestone: str, what: str) -> None:
        super().__init__(
            f"'{what}' pertenece al {milestone} y todavía no está implementado.\n"
            f"Ver docs/ROADMAP.md para los criterios de aceptación."
        )


# --------------------------------------------------------------------------- #
# Comandos                                                                     #
# --------------------------------------------------------------------------- #


def _progress_printer() -> Callable[[object, int, int], None]:
    """Traza del backfill: una línea que se reescribe en terminal, o un hilo de
    líneas cuando la salida va a un archivo."""
    from .data.backfill import format_ts, miles

    interactive = sys.stdout.isatty()
    seen = 0

    def progress(key: object, last_ts: int, total: int) -> None:
        nonlocal seen
        seen += 1
        line = f"    {format_ts(last_ts)}   {miles(total)} velas"
        if interactive:
            print(f"\r{line}   ", end="", flush=True)
        elif seen % 20 == 0:
            print(line, flush=True)

    return progress


def cmd_data_backfill(args: argparse.Namespace) -> int:
    """Descarga histórico de velas, reanudable."""
    from .config import load_config
    from .data.backfill import (
        backfill,
        format_ts,
        miles,
        parse_since,
        series_status,
        status_lines,
    )
    from .data.sources import VenueClient, VenueError
    from .data.store import CandleStore, SeriesKey

    cfg = load_config(args.config)
    symbol = args.symbol or cfg.primary_symbol
    base_tf = args.timeframe or cfg.market.timeframe
    timeframes = [base_tf]
    if args.context:
        timeframes += [tf for tf in cfg.market.context_timeframes if tf != base_tf]
    since = parse_since(args.since or cfg.market.history_start)

    store = CandleStore(cache_dir=cfg.path(cfg.storage.cache_dir))
    client = VenueClient(venue=cfg.market.venue)

    _warn_about_fees(client, symbol, cfg)

    print(f"backfill de {symbol} en {cfg.market.venue} desde {format_ts(since)} UTC")
    everything_ok = True
    for timeframe in timeframes:
        key = SeriesKey(cfg.market.venue, symbol, timeframe)
        print(f"\n  {key}")
        try:
            report = backfill(
                store,
                client,
                key,
                since=since,
                jump_threshold=cfg.risk.price_jump_anomaly,
                progress=_progress_printer(),
            )
        except VenueError as exc:
            print(f"\n    el venue falló: {exc}", file=sys.stderr)
            print(
                "    lo descargado hasta aquí está guardado: relanza el comando "
                "para continuar donde se quedó.",
                file=sys.stderr,
            )
            return EXIT_ERROR

        if sys.stdout.isatty():
            print()
        if report.up_to_date:
            print("    ya estaba al día")
        else:
            cola = " (incluida historia anterior a la que ya había)" if report.extended_backwards else ""
            print(f"    {miles(report.fetched)} velas nuevas{cola}")
        if report.heal.filled:
            print(f"    {report.heal.filled} velas de relleno en huecos cortos")
        if report.heal.long_gaps:
            print(f"    {len(report.heal.long_gaps)} huecos largos marcados, sin rellenar")

        status = series_status(store, key, jump_threshold=cfg.risk.price_jump_anomaly)
        everything_ok &= status.ok
        print("\n" + "\n".join(status_lines(status)))

    return EXIT_OK if everything_ok else EXIT_ERROR


def _warn_about_fees(client: VenueClient, symbol: str, cfg: Config) -> None:
    """Avisa si el venue cobra más de lo que el jardín cree.

    Evolucionar contra una fricción irreal produce bots que sólo existen en la
    configuración. No es motivo para abortar, pero sí para verlo.
    """
    try:
        info = client.market_info(symbol)
    except Exception:
        # Un aviso jamás debe tumbar el comando: si el venue no contesta, ya se
        # quejará el backfill, que es quien sí necesita la red.
        return
    real = info.get("taker_fee_bps")
    configured = cfg.frictions.taker_fee_bps
    if isinstance(real, (int, float)) and float(real) > float(configured) + 1e-9:
        print(
            f"  aviso: {symbol} cobra {real:.1f} bps de taker y la config dice "
            f"{configured:.1f}. Ajusta frictions.taker_fee_bps o el jardín "
            f"evolucionará contra una fricción irreal.",
            file=sys.stderr,
        )


def cmd_data_status(args: argparse.Namespace) -> int:
    """Estado de la caché: rango, número de velas, huecos y anomalías."""
    from .config import load_config
    from .data.backfill import resolve_keys, series_status, status_lines
    from .data.store import CandleStore

    cfg = load_config(args.config)
    store = CandleStore(cache_dir=cfg.path(cfg.storage.cache_dir))
    keys = resolve_keys(store, args.symbol)

    if not keys:
        donde = f" para {args.symbol}" if args.symbol else ""
        print(f"no hay velas en caché{donde}.")
        print("  keepgarden data backfill --symbol BTC/USDT --timeframe 1h")
        return EXIT_OK

    print(f"caché: {store.root}\n")
    everything_ok = True
    for key in keys:
        status = series_status(store, key, jump_threshold=cfg.risk.price_jump_anomaly)
        everything_ok &= status.ok
        print("\n".join(status_lines(status)))
        print()

    if not everything_ok:
        print(
            "hay series con problemas: relanza el backfill para que los huecos "
            "cortos se rellenen y los largos queden marcados.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    return EXIT_OK


def _load_catalog(cfg: Config) -> GeneCatalog:
    """El catálogo efectivo: el de código más el sesgo de ``config/genes.yaml``."""
    from .genome.catalog import load_catalog

    return load_catalog(cfg.path("config/genes.yaml"))


def _candles_for(
    cfg: Config, market: MarketSpec
) -> tuple[pd.DataFrame, dict[Timeframe, pd.DataFrame]]:
    """Velas operativas y de contexto de un mercado, desde la caché."""
    from typing import cast

    from .data.store import CandleStore, SeriesKey

    store = CandleStore(cache_dir=cfg.path(cfg.storage.cache_dir))
    base = store.load(SeriesKey(market.venue, market.symbol, market.timeframe))
    context: dict[Timeframe, pd.DataFrame] = {}
    for tf in cfg.market.context_timeframes:
        timeframe = cast("Timeframe", tf)
        velas = store.load(SeriesKey(market.venue, market.symbol, timeframe))
        if not velas.empty:
            context[timeframe] = velas
    return base, context


def cmd_genome_sample(args: argparse.Namespace) -> int:
    """Siembra genomas nuevos y los enseña, sin tocar el jardín."""
    import random

    from .config import load_config
    from .genome.catalog import SEEDABLE_FAMILIES
    from .genome.random_genome import random_genome
    from .genome.schema import MarketSpec, genome_hash
    from .genome.serialize import save_genome
    from .types import IdeaFamily

    cfg = load_config(args.config)
    catalog = _load_catalog(cfg)
    rng = random.Random(args.seed if args.seed is not None else cfg.seed)
    market = MarketSpec(
        venue=cfg.market.venue,
        symbol=args.symbol or cfg.primary_symbol,
        timeframe=cfg.market.timeframe,
    )
    familias = (IdeaFamily(args.family.upper()),) if args.family else SEEDABLE_FAMILIES

    destino = cfg.path(args.save) if args.save else None
    if destino is not None:
        destino.mkdir(parents=True, exist_ok=True)

    for i in range(args.count):
        family = familias[i % len(familias)]
        genome = random_genome(family, market, cfg, catalog, rng)
        print(f"\n{genome.describe()}")
        print(f"  hash: {genome_hash(genome)} · complejidad: {genome.complexity()}")
        if destino is not None:
            ruta = save_genome(genome, destino / f"{genome.id}.json")
            print(f"  guardado en {ruta}")
    return EXIT_OK


def cmd_genome_show(args: argparse.Namespace) -> int:
    """Lee un genoma, lo valida y lo compila contra las velas en caché."""
    import numpy as np

    from .config import load_config
    from .data.indicators import IndicatorCache
    from .genome.compile import compile_genome
    from .genome.schema import genome_hash
    from .genome.serialize import load_genome
    from .genome.validate import validate_genome
    from .types import Signal

    cfg = load_config(args.config)
    catalog = _load_catalog(cfg)
    genome = load_genome(args.genome)

    print(genome.describe())
    print(f"\nhash: {genome_hash(genome)} · complejidad: {genome.complexity()}")

    informe = validate_genome(genome, cfg, catalog, strict=False)
    print(f"validación: {'ok' if informe.ok else 'INVÁLIDO'}")
    for error in informe.errors:
        print(f"  error: {error}", file=sys.stderr)
    for aviso in informe.warnings:
        print(f"  aviso: {aviso}")
    if not informe.ok:
        return EXIT_ERROR

    velas, contexto = _candles_for(cfg, genome.market)
    if velas.empty:
        print(
            f"\nno hay velas de {genome.market.key()} en caché: no se puede compilar.\n"
            f"  keepgarden data backfill --symbol {genome.market.symbol}"
        )
        return EXIT_OK

    cache = IndicatorCache(
        candles=velas, context=contexto, catalog=catalog, timeframe=genome.market.timeframe
    )
    compilado = compile_genome(genome, velas, cache)
    señales = compilado.signals()
    operables = len(velas) - compilado.warmup_bars

    from .data.backfill import format_ts, miles

    print(f"\nvelas      {miles(len(velas))} · {format_ts(int(velas.index[0]))} → "
          f"{format_ts(int(velas.index[-1]))}")
    print(f"calentamiento {miles(compilado.warmup_bars)} velas "
          f"({miles(max(0, operables))} operables)")
    for signal in (Signal.ENTER_LONG, Signal.EXIT_LONG):
        n = int((señales == signal).sum())
        cada = f" · una cada {operables // n} velas" if n else ""
        print(f"{signal!s:<12} {miles(n)}{cada}")

    for gene in genome.features:
        serie = compilado.feature_arrays[gene.id]
        validos = serie[np.isfinite(serie)]
        if validos.size:
            print(
                f"  {gene.id:<14} {gene.kind:<14} "
                f"min {validos.min():>12.4g}  mediana {np.median(validos):>12.4g}  "
                f"max {validos.max():>12.4g}"
            )
    return EXIT_OK


def _open_garden(cfg: Config, *, create: bool = True):
    """Abre la base del jardín y devuelve sus repositorios."""
    from .storage.db import open_database
    from .storage.repositories import Repositories

    db = open_database(cfg.db_file, create=create)
    return db, Repositories.open(db)


def cmd_garden_seed(args: argparse.Namespace) -> int:
    """Siembra la población inicial y crea la base del jardín."""
    import random
    import time

    from .config import load_config
    from .genome.distance import mean_pairwise_distance
    from .genome.random_genome import random_population
    from .genome.schema import MarketSpec
    from .ids import bot_id_of, label
    from .types import EventType

    cfg = load_config(args.config)
    catalog = _load_catalog(cfg)
    tamaño = args.size if args.size is not None else cfg.garden.target_population

    if args.reset and cfg.db_file.exists():
        print(f"borrando el jardín anterior: {cfg.db_file}")
        cfg.db_file.unlink()
        for extra in (".db-wal", ".db-shm"):
            sobrante = cfg.db_file.with_suffix(extra)
            if sobrante.exists():
                sobrante.unlink()

    db, repos = _open_garden(cfg)
    try:
        vivos = repos.bots.count_alive()
        if vivos and not args.reset:
            print(
                f"el jardín ya tiene {vivos} bots vivos. Usa --reset para "
                f"empezar de cero (es destructivo).",
                file=sys.stderr,
            )
            return EXIT_ERROR

        market = MarketSpec(
            venue=cfg.market.venue, symbol=cfg.primary_symbol,
            timeframe=cfg.market.timeframe,
        )
        rng = random.Random(cfg.seed)
        print(f"sembrando {tamaño} bots sobre {market.symbol} {market.timeframe}…")
        genomas = random_population(tamaño, market, cfg, catalog, rng)
        if len(genomas) < tamaño:
            print(
                f"  aviso: sólo han salido {len(genomas)} genomas válidos",
                file=sys.stderr,
            )

        ahora = int(time.time() * 1000)
        with db.transaction():
            for genoma in genomas:
                bot = repos.bots.create(
                    genoma, generation=0,
                    initial_capital=cfg.garden.initial_capital_per_bot,
                )
                repos.events.log(
                    EventType.BOT_BORN, f"se siembra {bot} ({genoma.family})",
                    ts=ahora, generation=0, bot_id=bot,
                    payload={"operator": "SEED", "family": str(genoma.family)},
                )
            repos.generations.open(0, ahora)
            db.set_meta("current_generation", 0)
            db.set_meta("seed", cfg.seed)
            db.set_meta("symbol", market.symbol)
            db.set_meta("timeframe", market.timeframe)
            db.set_meta("venue", market.venue)

        diversidad = mean_pairwise_distance(
            genomas, cfg.speciation.distance_weights, catalog
        )
        familias: dict[str, int] = {}
        for g in genomas:
            familias[str(g.family)] = familias.get(str(g.family), 0) + 1

        print(f"\n{len(genomas)} bots vivos en {cfg.db_file}")
        print(f"diversidad genética  {diversidad:.3f}"
              f"  (suelo {cfg.evolution.diversity_floor})")
        for familia, n in sorted(familias.items()):
            print(f"  {familia:<16} {n:>3}  {n / len(genomas):>5.0%}")
        print("\nprimeros bots:")
        for genoma in genomas[:5]:
            print(f"  {label(bot_id_of(genoma.id))}  {genoma.family}")
        return EXIT_OK
    finally:
        db.close()


def cmd_garden_status(args: argparse.Namespace) -> int:
    """Resumen del jardín en la terminal: población, generación, capital."""
    from .config import load_config
    from .ids import label

    cfg = load_config(args.config)
    if not cfg.db_file.exists():
        print(
            "no hay jardín todavía. Siémbralo con 'keepgarden garden seed'.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    db, repos = _open_garden(cfg, create=False)
    try:
        vivos = repos.bots.alive()
        generacion = int(db.get_meta("current_generation") or 0)
        print(f"\njardín  {cfg.db_file}")
        print(f"símbolo {db.get_meta('symbol') or cfg.primary_symbol}"
              f" {db.get_meta('timeframe') or cfg.market.timeframe}"
              f"  ·  semilla {db.get_meta('seed') or cfg.seed}")
        print(f"generación {generacion}  ·  {len(vivos)} bots vivos"
              f"  ·  {len(repos.bots.all())} en total")

        ultima = repos.generations.latest()
        if ultima is not None and ultima["closed_at"]:
            print(
                f"\núltima generación cerrada: {ultima['generation']}"
                f"  nacimientos {ultima['births']}  muertes {ultima['deaths']}"
                f"  descartados {ultima['discarded']}"
            )
            if ultima["fitness_median"] is not None:
                print(
                    f"  fitness  mediana {ultima['fitness_median']:+.3f}"
                    f"  mejor {ultima['fitness_best']:+.3f}"
                    f"  ·  diversidad {ultima['genetic_diversity']:.3f}"
                    f"  ·  {ultima['n_species']} especies"
                )

        if vivos:
            capital = sum(float(b["equity"]) for b in vivos)
            print(f"\ncapital del jardín {capital:,.2f}")
            mejores = sorted(
                (b for b in vivos if b["fitness_effective"] is not None),
                key=lambda b: -float(b["fitness_effective"]),
            )[:8]
            if mejores:
                print("\nmejores bots:")
                for b in mejores:
                    print(
                        f"  {label(b['bot_id']):<28} {b['family']:<16}"
                        f" fit {float(b['fitness_effective']):+.3f}"
                        f"  gen {b['born_generation']:>3}"
                        f"  ops {b['total_trades']:>4}"
                    )

        alertas = repos.events.open_alerts()
        if alertas:
            print("\nalertas abiertas:")
            for a in alertas:
                print(f"  {a['kind']:<18} {a['value']:.3f} vs {a['threshold']:.3f}"
                      f"  {a['detail'] or ''}")
        return EXIT_OK
    finally:
        db.close()


def _save_equity(result: object, cfg: Config, genome_id: str) -> str:
    """Guarda la curva de capital: PNG si hay matplotlib, CSV si no.

    ``matplotlib`` no está en el stack del proyecto y no se añade por un
    gráfico de la CLI: las curvas de verdad se ven en el dashboard. Si el
    entorno lo tiene, se aprovecha; si no, el CSV sirve igual para mirarlo con
    cualquier cosa.
    """
    import numpy as np

    destino = cfg.path(cfg.gardener.report_dir)
    destino.mkdir(parents=True, exist_ok=True)
    equity = result.equity_curve  # type: ignore[attr-defined]
    momentos = result.equity_ts  # type: ignore[attr-defined]

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        ruta = destino / f"equity_{genome_id}.csv"
        np.savetxt(
            ruta, np.column_stack([momentos, equity]),
            delimiter=",", header="ts,equity", comments="", fmt=["%d", "%.8f"],
        )
        return f"{ruta} (sin matplotlib: se guarda la serie, no el gráfico)"

    fechas = np.asarray(momentos, dtype="datetime64[ms]")
    figura, eje = plt.subplots(figsize=(11, 4))
    eje.plot(fechas, equity, linewidth=1.0)
    eje.set_title(f"curva de capital · {genome_id}")
    eje.grid(alpha=0.3)
    figura.tight_layout()
    ruta = destino / f"equity_{genome_id}.png"
    figura.savefig(ruta, dpi=120)
    plt.close(figura)
    return str(ruta)


def cmd_backtest(args: argparse.Namespace) -> int:
    """Corre un genoma sobre un rango de velas e imprime sus métricas."""
    from .config import load_config
    from .data.backfill import format_ts, miles, parse_since
    from .data.indicators import IndicatorCache
    from .engine.backtest import run_backtest
    from .evaluation.metrics import compute_metrics
    from .genome.serialize import load_genome
    from .genome.validate import validate_genome

    cfg = load_config(args.config)
    catalog = _load_catalog(cfg)
    genome = load_genome(args.genome)

    informe = validate_genome(genome, cfg, catalog, strict=False)
    if not informe.ok:
        print(f"{genome.id} no es un genoma válido:", file=sys.stderr)
        for error in informe.errors:
            print(f"  · {error}", file=sys.stderr)
        return EXIT_ERROR

    velas, contexto = _candles_for(cfg, genome.market)
    if velas.empty:
        print(
            f"no hay velas de {genome.market.key()} en caché.\n"
            f"  keepgarden data backfill --symbol {genome.market.symbol}",
            file=sys.stderr,
        )
        return EXIT_ERROR

    desde = parse_since(args.from_date) if args.from_date else None
    hasta = parse_since(args.to_date) if args.to_date else None
    if desde is not None:
        velas = velas.loc[velas.index >= desde]
    if hasta is not None:
        velas = velas.loc[velas.index <= hasta]
    if velas.empty:
        print("el rango pedido no contiene ninguna vela", file=sys.stderr)
        return EXIT_ERROR

    cache = IndicatorCache(
        candles=velas, context=contexto, catalog=catalog, timeframe=genome.market.timeframe
    )
    resultado = run_backtest(genome, velas, cfg, features=cache)
    assert resultado.equity_curve is not None
    m = compute_metrics(
        resultado.equity_curve,
        resultado.trades,
        genome.market.timeframe,
        total_fees=resultado.total_fees,
        benchmark=velas["close"].to_numpy(),
    )

    capital = float(resultado.equity_curve[0])
    print(genome.describe())
    print(
        f"\nventana    {format_ts(resultado.start_ts)} → {format_ts(resultado.end_ts)} "
        f"· {miles(resultado.n_bars)} velas (calentamiento {miles(resultado.warmup_bars)})"
    )
    if resultado.aborted_reason:
        print(f"ABORTADO   {resultado.aborted_reason}")

    print(f"\ncapital    {capital:,.2f} → {resultado.final_equity:,.2f} "
          f"({m.total_return:+.2%})")
    print(f"comisiones {resultado.total_fees:,.2f}"
          f"  ·  buy & hold {velas['close'].iloc[-1] / velas['close'].iloc[0] - 1:+.2%}")

    filas = [
        ("retorno", [("total", f"{m.total_return:+.2%}"), ("CAGR", f"{m.cagr:+.2%}"),
                     ("por operación", f"{m.avg_trade_return:+.3%}"),
                     ("expectancy", f"{m.expectancy:+.3%}")]),
        ("riesgo", [("max drawdown", f"{m.max_drawdown:.2%}"),
                    ("ulcer", f"{m.ulcer_index:.4f}"),
                    ("peor operación", f"{m.worst_trade:+.2%}"),
                    ("en mercado", f"{m.time_in_market:.1%}")]),
        ("ajustadas", [("sortino", f"{m.sortino:.2f}"), ("sharpe", f"{m.sharpe:.2f}"),
                       ("calmar", f"{m.calmar:.2f}"), ("martin", f"{m.martin:.2f}"),
                       ("profit factor", f"{m.profit_factor:.2f}")]),
        ("comportamiento", [("operaciones", miles(m.n_trades)),
                            ("aciertos", f"{m.win_rate:.1%}"),
                            ("velas por op.", f"{m.avg_holding_bars:.0f}"),
                            ("turnover", f"{m.turnover:.1f}x"),
                            ("fee drag", f"{m.fee_drag:.2f}"),
                            ("consistencia", f"{m.consistency:.1%}")]),
        ("relación", [("corr. buy & hold", f"{m.corr_to_benchmark:+.2f}")]),
    ]
    for titulo, pares in filas:
        print(f"\n{titulo}")
        for nombre, valor in pares:
            print(f"  {nombre:<18} {valor:>12}")

    if m.n_trades < cfg.fitness.min_trades:
        print(
            f"\naviso: {m.n_trades} operaciones, por debajo de fitness.min_trades "
            f"({cfg.fitness.min_trades}). Sin evidencia suficiente, el fitness de "
            f"este bot quedaría indefinido."
        )

    if args.plot:
        print(f"\ncurva de capital: {_save_equity(resultado, cfg, genome.id)}")
    return EXIT_OK


def cmd_incubate(args: argparse.Namespace) -> int:
    """Cosechas de incubadora sobre histórico: el reloj rápido."""
    import random

    from .config import load_config
    from .engine.incubator import Incubator
    from .evaluation.fitness import robust_reference
    from .evolution.population import Population
    from .genome.schema import MarketSpec

    cfg = load_config(args.config)
    catalog = _load_catalog(cfg)
    if not cfg.db_file.exists():
        print(
            "no hay jardín todavía. Siémbralo con 'keepgarden garden seed'.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    market = MarketSpec(
        venue=cfg.market.venue, symbol=cfg.primary_symbol,
        timeframe=cfg.market.timeframe,
    )
    velas, _ = _candles_for(cfg, market)
    if velas.empty:
        print(
            f"no hay velas de {market.symbol} en caché. Descárgalas con "
            f"'keepgarden data backfill'.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    db, repos = _open_garden(cfg, create=False)
    try:
        incubadora = Incubator(
            cfg=cfg, candles=velas, catalog=catalog, repo=repos.incubation
        )
        split = incubadora.split
        print(f"\nvelas {len(velas):,}  ·  {len(split.folds)} pliegues  ·  "
              f"holdout intocable desde la vela {split.holdout_start:,}")
        if args.workers is not None:
            print(f"trabajadores: {args.workers}")

        poblacion = Population(
            cfg=cfg, catalog=catalog, db=db, rng=random.Random(cfg.seed), repos=repos
        )
        vivos = poblacion.alive_genomes()
        if not vivos:
            print("el jardín está vacío.", file=sys.stderr)
            return EXIT_ERROR

        # La escala de la generación 0 se congela: sin ella el fitness es
        # relativo a los contemporáneos, su mediana vale 0 por construcción y no
        # hay forma de ver si el jardín mejora. Ver docs/DECISIONS.md D-019.
        base = incubadora.measure(list(vivos.values()), members=vivos)
        from .engine.incubator import fold_metrics_to_metrics

        metricas_cero = {
            bot: fold_metrics_to_metrics(base[g.id].fold_metrics)
            for bot, g in vivos.items()
        }
        poblacion.reference = robust_reference(metricas_cero, cfg.fitness)

        arranque = int(db.get_meta("current_generation") or 0)
        print(f"\npartiendo de la generación {arranque} con {len(vivos)} bots vivos\n")
        for i in range(int(args.generations)):
            generacion = arranque + i + 1
            resultado = poblacion.evolve_generation(generacion, incubadora)
            print(resultado.summary_line)
            for aviso in resultado.alerts:
                print(f"           aviso: {aviso}")
            if not poblacion.alive_ids():
                print("el jardín se ha quedado sin bots.", file=sys.stderr)
                return EXIT_ERROR

        historia = poblacion.fitness_history
        if len(historia) >= 2:
            print(
                f"\nmediana de fitness: {historia[0]:+.3f} → {historia[-1]:+.3f}"
                f"  ({historia[-1] - historia[0]:+.3f})"
            )
        print("\nresumen: keepgarden garden status")
        return EXIT_OK
    finally:
        db.close()


def cmd_run(args: argparse.Namespace) -> int:
    """Arranca el jardín vivo. Con ``--dry-run`` recorre histórico acelerado."""
    raise PendingMilestone("hito 5 (El jardín vivo)", "run")


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Levanta el dashboard local: FastAPI + front, en sólo lectura."""
    from dataclasses import replace

    from .config import load_config

    cfg = load_config(args.config)
    if not cfg.db_file.exists():
        print(
            f"no hay jardín en {cfg.db_file}. Siémbralo con "
            f"'keepgarden garden seed' y córrelo con 'keepgarden incubate'.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    if args.port is not None:
        cfg = replace(cfg, dashboard=replace(cfg.dashboard, port=int(args.port)))

    try:
        from .dashboard.app import serve
    except ImportError as exc:  # fastapi/uvicorn no instalados
        print(
            f"falta una dependencia del dashboard ({exc.name}). Instálalas con "
            f"'pip install -e .' o '.\\scripts\\bootstrap.ps1'.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        serve(cfg, open_browser=not args.no_browser)
    except KeyboardInterrupt:  # pragma: no cover - salida normal con Ctrl+C
        print("\ndashboard parado.")
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    """Genera el informe de generación para el jardinero."""
    raise PendingMilestone("hito 7 (El jardinero)", "report")


def cmd_gardener_apply(args: argparse.Namespace) -> int:
    """Valida y aplica un archivo de propuestas del jardinero."""
    raise PendingMilestone("hito 7 (El jardinero)", "gardener apply")


def cmd_config_show(args: argparse.Namespace) -> int:
    """Carga y valida la configuración, y la imprime resuelta."""
    import json

    from .config import load_config

    cfg = load_config(args.config)
    print(json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False))
    print(f"\nraíz del proyecto: {cfg.root}", file=sys.stderr)
    return EXIT_OK


def cmd_catalog(args: argparse.Namespace) -> int:
    """Lista el catálogo de genes disponible."""
    from .genome.catalog import BY_CATEGORY, INDICATORS, spec

    if args.kind:
        s = spec(args.kind)
        print(f"{s.kind}  [{s.category} / {s.output}]")
        if s.doc:
            print(f"  {s.doc}")
        for p in s.params:
            tipo = "entero" if p.integer else "real"
            print(f"  · {p.name}: {p.low}–{p.high} ({tipo}, paso {p.bin_size})")
        if s.multi_output:
            print(f"  · salidas: {', '.join(s.multi_output)}")
        print(f"  · fuentes: {', '.join(str(x) for x in s.sources)}")
        return EXIT_OK

    for category, kinds in sorted(BY_CATEGORY.items()):
        print(f"\n{category.upper()}")
        for k in sorted(kinds):
            doc = INDICATORS[k].doc
            print(f"  {k:<16} {doc}")
    print(f"\n{len(INDICATORS)} indicadores en el catálogo.")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Parser                                                                       #
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="keepgarden",
        description="botKeepGarden — un jardín de bots de trading que nacen, "
                    "se fusionan, evolucionan y mueren sobre datos reales.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Documentación: docs/ARCHITECTURE.md · Plan de trabajo: docs/ROADMAP.md",
    )
    p.add_argument("--version", action="version", version=f"keepgarden {__version__}")
    p.add_argument("-c", "--config", default=None, help="ruta a garden.yaml")
    sub = p.add_subparsers(dest="command", metavar="<comando>")

    # -- data -------------------------------------------------------------- #
    data = sub.add_parser("data", help="velas: descarga y estado de la caché")
    data_sub = data.add_subparsers(dest="subcommand", metavar="<subcomando>")

    bf = data_sub.add_parser("backfill", help="descarga histórico (reanudable)")
    bf.add_argument("--symbol", default=None, help="por defecto, el primero de market.symbols")
    bf.add_argument("--timeframe", default=None)
    bf.add_argument("--since", default=None, help="fecha ISO, p.ej. 2019-01-01")
    bf.add_argument("--context", action="store_true", help="descargar también 4h y 1d")
    bf.set_defaults(func=cmd_data_backfill)

    ds = data_sub.add_parser("status", help="rango, huecos y anomalías en caché")
    ds.add_argument("--symbol", default=None)
    ds.set_defaults(func=cmd_data_status)

    # -- genome ------------------------------------------------------------ #
    gen = sub.add_parser("genome", help="genomas: sembrar uno nuevo y examinarlo")
    gen_sub = gen.add_subparsers(dest="subcommand", metavar="<subcomando>")

    gsam = gen_sub.add_parser("sample", help="siembra genomas nuevos y los enseña")
    gsam.add_argument("--family", default=None, help="TREND, MEAN_REVERSION, BREAKOUT…")
    gsam.add_argument("--count", type=int, default=3)
    gsam.add_argument("--seed", type=int, default=None, help="por defecto, la semilla del jardín")
    gsam.add_argument("--symbol", default=None)
    gsam.add_argument("--save", default=None, help="carpeta donde escribir los JSON")
    gsam.set_defaults(func=cmd_genome_sample)

    gsh = gen_sub.add_parser("show", help="valida y compila un genoma sobre las velas en caché")
    gsh.add_argument("--genome", required=True, help="ruta a un genoma JSON")
    gsh.set_defaults(func=cmd_genome_show)

    # -- garden ------------------------------------------------------------ #
    garden = sub.add_parser("garden", help="población: sembrar y consultar")
    garden_sub = garden.add_subparsers(dest="subcommand", metavar="<subcomando>")

    gs = garden_sub.add_parser("seed", help="siembra la población inicial")
    gs.add_argument("--size", type=int, default=None, help="por defecto, garden.target_population")
    gs.add_argument("--reset", action="store_true", help="borra el jardín existente (¡destructivo!)")
    gs.set_defaults(func=cmd_garden_seed)

    gst = garden_sub.add_parser("status", help="resumen del jardín")
    gst.set_defaults(func=cmd_garden_status)

    # -- backtest ---------------------------------------------------------- #
    bt = sub.add_parser("backtest", help="corre un genoma y muestra sus métricas")
    bt.add_argument("--genome", required=True, help="ruta a un genoma JSON")
    bt.add_argument("--from", dest="from_date", default=None)
    bt.add_argument("--to", dest="to_date", default=None)
    bt.add_argument("--plot", action="store_true", help="guarda la curva de capital en PNG")
    bt.set_defaults(func=cmd_backtest)

    # -- incubate ---------------------------------------------------------- #
    inc = sub.add_parser("incubate", help="evolución rápida sobre histórico")
    inc.add_argument("--generations", type=int, default=10)
    inc.add_argument("--workers", type=int, default=None)
    inc.set_defaults(func=cmd_incubate)

    # -- run --------------------------------------------------------------- #
    run = sub.add_parser("run", help="arranca el jardín vivo (bucle continuo)")
    run.add_argument("--dry-run", action="store_true", help="recorre histórico como si fuera vivo")
    run.add_argument("--speed", type=float, default=0.0, help="velas/segundo en dry-run; 0 = máxima")
    run.add_argument("--max-ticks", type=int, default=None)
    run.set_defaults(func=cmd_run)

    # -- dashboard --------------------------------------------------------- #
    dash = sub.add_parser("dashboard", help="abre el dashboard local")
    dash.add_argument("--port", type=int, default=None)
    dash.add_argument("--no-browser", action="store_true")
    dash.set_defaults(func=cmd_dashboard)

    # -- report ------------------------------------------------------------ #
    rep = sub.add_parser("report", help="informe de generación para el jardinero")
    rep.add_argument("--generation", default="latest", help="número o 'latest'")
    rep.add_argument("--stdout", action="store_true", help="imprime en vez de escribir el archivo")
    rep.set_defaults(func=cmd_report)

    # -- gardener ---------------------------------------------------------- #
    gard = sub.add_parser("gardener", help="sesiones del jardinero")
    gard_sub = gard.add_subparsers(dest="subcommand", metavar="<subcomando>")
    ga = gard_sub.add_parser("apply", help="valida y aplica propuestas")
    ga.add_argument("--file", required=True, help="JSON con las propuestas")
    ga.add_argument("--dry-run", action="store_true", help="sólo valida, no aplica")
    ga.set_defaults(func=cmd_gardener_apply)

    # -- config / catalog -------------------------------------------------- #
    cs = sub.add_parser("config", help="muestra la configuración resuelta y validada")
    cs.set_defaults(func=cmd_config_show)

    cat = sub.add_parser("catalog", help="lista el catálogo de genes")
    cat.add_argument("kind", nargs="?", help="detalle de un indicador concreto")
    cat.set_defaults(func=cmd_catalog)

    return p


def _utf8_when_redirected() -> None:
    """Escribe UTF-8 cuando la salida no es una consola.

    En Windows, redirigir a un archivo usa la codificación local (cp1252), que
    no sabe escribir ni ``→`` ni ``·`` y revienta a mitad de un informe. En la
    consola no se toca nada: Python ya habla con ella en Unicode y forzar UTF-8
    ahí estropearía los acentos en PowerShell 5.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None and not stream.isatty():
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_when_redirected()
    parser = build_parser()
    args = parser.parse_args(argv)

    func: Callable[[argparse.Namespace], int] | None = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return EXIT_OK

    try:
        return func(args)
    except PendingMilestone as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED
    except KeyboardInterrupt:
        print("\ninterrumpido", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


__all__ = ("PendingMilestone", "build_parser", "main")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
