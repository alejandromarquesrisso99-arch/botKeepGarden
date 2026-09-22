"""Permite ``python -m keepgarden``.

El script de consola ``keepgarden`` existe y es lo normal, pero depende de que
la carpeta ``Scripts\\`` del entorno esté en el PATH. Los lanzadores de doble
clic invocan el intérprete del entorno directamente, así que les vale más esto.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
