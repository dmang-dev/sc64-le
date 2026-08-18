"""Locate the sc64-maps checkout this repository builds on.

sc64-maps owns the read side: the BOLT archive walker, the CHK parser, the MPQ
reader and key cracker, the ROM finder. This repository owns the write side --
injecting maps, patching tables, rebranding, and driving an emulator to check
the result. Rather than vendor or duplicate those primitives, this puts the
sc64-maps checkout on sys.path.

Resolution order, first hit wins:

    1. the SC64_MAPS environment variable
    2. a sibling directory named sc64-maps
    3. a sibling named sc64_maps

Import this module before anything from sc64-maps:

    import _deps  # noqa: F401
    from extract_sc64_maps import BoltArchive
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# A file that only sc64-maps has, so a wrong directory is rejected rather than
# half-imported with a confusing error several frames later.
SENTINEL = "extract_sc64_maps.py"


def candidates():
    env = os.environ.get("SC64_MAPS")
    if env:
        yield Path(env)
    yield HERE.parent / "sc64-maps"
    yield HERE.parent / "sc64_maps"


def find() -> Path:
    tried = []
    for c in candidates():
        tried.append(str(c))
        if (c / SENTINEL).is_file():
            return c.resolve()
    raise SystemExit(
        "cannot find the sc64-maps checkout.\n"
        "  Set SC64_MAPS, or clone it next to this repository:\n"
        "    git clone https://github.com/dmang-dev/sc64-maps\n"
        "  looked in:\n    " + "\n    ".join(tried))


SC64_MAPS = find()
if str(SC64_MAPS) not in sys.path:
    sys.path.insert(0, str(SC64_MAPS))
