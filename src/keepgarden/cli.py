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


def cmd_garden_seed(args: argparse.Namespace) -> int:
    """Siembra la población inicial y crea la base del jardín."""
    raise PendingMilestone("hito 4 (Incubadora y evolución)", "garden seed")


def cmd_garden_status(args: argparse.Namespace) -> int:
    """Resumen del jardín en la terminal: población, generación, capital."""
    raise PendingMilestone("hito 4 (Incubadora y evolución)", "garden status")


def cmd_backtest(args: argparse.Namespace) -> int:
    """Corre un genoma sobre un rango de velas e imprime sus métricas."""
    raise PendingMilestone("hito 3 (Motor de simulación)", "backtest")


def cmd_incubate(args: argparse.Namespace) -> int:
    """Cosechas de incubadora sobre histórico: el reloj rápido."""
    raise PendingMilestone("hito 4 (Incubadora y evolución)", "incubate")


def cmd_run(args: argparse.Namespace) -> int:
    """Arranca el jardín vivo. Con ``--dry-run`` recorre histórico acelerado."""
    raise PendingMilestone("hito 5 (El jardín vivo)", "run")


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Levanta el dashboard local."""
    raise PendingMilestone("hito 6 (Dashboard)", "dashboard")


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
