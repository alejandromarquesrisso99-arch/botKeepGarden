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
from typing import Callable, Sequence

from . import __version__

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


def cmd_data_backfill(args: argparse.Namespace) -> int:
    """Descarga histórico de velas, reanudable."""
    raise PendingMilestone("hito 1 (Datos)", "data backfill")


def cmd_data_status(args: argparse.Namespace) -> int:
    """Estado de la caché: rango, número de velas, huecos y anomalías."""
    raise PendingMilestone("hito 1 (Datos)", "data status")


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


def main(argv: Sequence[str] | None = None) -> int:
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
