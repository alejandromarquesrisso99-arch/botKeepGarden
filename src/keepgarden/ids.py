"""Identidad de los bots: un id estable y un nombre que un humano recuerde.

Los bots se miran mucho en el dashboard. ``bot_9f04`` es preciso pero no se
recuerda; ``Zarza-Terca`` sí. Cada bot tiene ambos: el id manda, el nombre es
para las personas.

Los nombres se generan de forma determinista a partir del id, así que no hay que
guardarlos ni preocuparse por colisiones de estado: el mismo id siempre da el
mismo nombre.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Iterable

from .types import BotId, GenomeId, LineageId

#: Sustantivos de jardín. El primer componente del nombre.
_PLANTS: tuple[str, ...] = (
    "Zarza", "Helecho", "Roble", "Musgo", "Cardo", "Junco", "Liquen", "Hiedra",
    "Ortiga", "Laurel", "Cipres", "Aliso", "Retama", "Tomillo", "Salvia", "Enebro",
    "Acebo", "Almendro", "Brezo", "Espino", "Sauce", "Membrillo", "Nogal", "Lentisco",
    "Jara", "Madroño", "Adelfa", "Boj", "Mirto", "Glicina", "Serbal", "Alerce",
    "Abedul", "Castaño", "Fresno", "Olmo", "Tejo", "Arce", "Chopo", "Encina",
)

#: Adjetivos. El segundo componente.
_TRAITS: tuple[str, ...] = (
    "Terca", "Sobria", "Rapida", "Paciente", "Aspera", "Nocturna", "Fria", "Tenaz",
    "Agil", "Sorda", "Lenta", "Astuta", "Esquiva", "Firme", "Huraña", "Voraz",
    "Serena", "Torcida", "Alerta", "Sagaz", "Tosca", "Fiel", "Adusta", "Viva",
    "Seca", "Nueva", "Vieja", "Honda", "Clara", "Turbia", "Fina", "Dura",
)


def new_bot_id(rng_token: str | None = None) -> BotId:
    """Genera un id de bot nuevo: ``bot_`` + 8 hex.

    Args:
        rng_token: Si se pasa, el id es determinista a partir de él. Se usa en
            los tests y en las ejecuciones sembradas, donde todo el jardín debe
            ser reproducible. En producción se deja a ``None``.
    """
    if rng_token is None:
        return f"bot_{secrets.token_hex(4)}"
    digest = hashlib.sha256(rng_token.encode("utf-8")).hexdigest()
    return f"bot_{digest[:8]}"


def new_genome_id(bot_id: BotId) -> GenomeId:
    """El genoma comparte identidad con su bot: un bot, un genoma."""
    return bot_id.replace("bot_", "gen_", 1)


def bot_id_of(genome_id: GenomeId) -> BotId:
    """La inversa de ``new_genome_id``: de quién es este genoma.

    Hace falta porque la genealogía y las carteras se indexan por bot mientras
    que los operadores evolutivos trabajan con genomas, y sin esto cada módulo
    acabaría haciendo el ``replace`` por su cuenta.
    """
    return genome_id.replace("gen_", "bot_", 1)


def new_lineage_id(seed_bot_id: BotId, family: str) -> LineageId:
    """Id del linaje raíz, creado cuando nace un bot por ``SEED``."""
    short = seed_bot_id.replace("bot_", "")[:6]
    return f"lin_{family.lower()}_{short}"


def bot_name(bot_id: BotId) -> str:
    """Nombre legible y determinista a partir del id."""
    digest = hashlib.sha256(bot_id.encode("utf-8")).digest()
    plant = _PLANTS[digest[0] % len(_PLANTS)]
    trait = _TRAITS[digest[1] % len(_TRAITS)]
    suffix = digest[2] % 100
    return f"{plant}-{trait}-{suffix:02d}"


def short(bot_id: BotId) -> str:
    """Forma corta para tablas estrechas: ``9f04``."""
    return bot_id.split("_")[-1][:4]


def label(bot_id: BotId) -> str:
    """``Zarza-Terca-41 (9f04)`` — lo que se muestra en el dashboard."""
    return f"{bot_name(bot_id)} ({short(bot_id)})"


def unique_names(bot_ids: Iterable[BotId]) -> dict[BotId, str]:
    """Nombres garantizando unicidad dentro del conjunto dado.

    Con 40x32x100 = 128.000 combinaciones las colisiones son raras pero no
    imposibles. Cuando ocurren se desambigua con el id corto.
    """
    seen: dict[str, BotId] = {}
    out: dict[BotId, str] = {}
    for bid in bot_ids:
        name = bot_name(bid)
        if name in seen:
            out[bid] = f"{name}·{short(bid)}"
            other = seen[name]
            out[other] = f"{name}·{short(other)}"
        else:
            seen[name] = bid
            out[bid] = name
    return out


__all__ = (
    "bot_id_of",
    "new_bot_id",
    "new_genome_id",
    "new_lineage_id",
    "bot_name",
    "short",
    "label",
    "unique_names",
)
