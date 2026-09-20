"""Serialización de genomas a JSON y vuelta.

Garantía de round-trip exacto:

    g2 = load_genome_str(dump_genome_str(g1))
    assert g1.to_dict() == g2.to_dict()

Hay un test que lo comprueba con genomas aleatorios en cada ejecución.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schema import Genome, canonical_json, genome_hash


def dump_genome_str(genome: Genome, *, indent: int | None = 2) -> str:
    """Genoma a JSON legible (con ``meta``)."""
    return json.dumps(
        genome.to_dict(include_meta=True),
        indent=indent,
        ensure_ascii=False,
        sort_keys=False,
    )


def load_genome_str(payload: str) -> Genome:
    return Genome.from_dict(json.loads(payload))


def save_genome(genome: Genome, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_genome_str(genome), encoding="utf-8")
    return p


def load_genome(path: str | Path) -> Genome:
    return load_genome_str(Path(path).read_text(encoding="utf-8"))


def genome_to_row(genome: Genome) -> dict[str, Any]:
    """Fila lista para la tabla ``genomes``."""
    return {
        "genome_id": genome.id,
        "hash": genome_hash(genome),
        "family": str(genome.family),
        "venue": genome.market.venue,
        "symbol": genome.market.symbol,
        "timeframe": genome.market.timeframe,
        "is_ensemble": int(genome.is_ensemble),
        "complexity": genome.complexity(),
        "n_features": len(genome.features),
        "max_depth": genome.max_rule_depth(),
        "generation": genome.meta.generation,
        "operator": str(genome.meta.operator),
        "root_lineage": genome.meta.root_lineage,
        "payload": canonical_json(genome, include_meta=True),
    }


def genome_from_row(row: Any) -> Genome:
    """Reconstruye desde una fila de ``genomes`` (dict o sqlite3.Row)."""
    payload = row["payload"] if not isinstance(row, dict) else row["payload"]
    return load_genome_str(payload)


__all__ = (
    "dump_genome_str",
    "load_genome_str",
    "save_genome",
    "load_genome",
    "genome_to_row",
    "genome_from_row",
)
