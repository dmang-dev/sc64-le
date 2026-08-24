"""Cache bolt-lzss output, because compression is the whole cost of a build.

Encoding 62 maps at level 3 takes the better part of twenty minutes and every
rebuild pays it again for byte-identical input. Decoding the same stream takes
well under a second. So the cache stores compressed payloads keyed by the hash
of what went in, and VERIFIES EVERY HIT by decoding it and comparing -- the
cheap direction -- rather than trusting the file on disk.

That matters more than the speed. A silently corrupt cache would produce a ROM
that builds cleanly, reads back cleanly through the archive walk, and then
fails on hardware for reasons pointing anywhere but here. A hit that fails its
round trip is deleted and re-encoded, so the cache is self-healing and a
corrupt entry costs one rebuild rather than an afternoon.

The cache holds compressed Blizzard map data, so it lives outside the
repository by default -- under the platform temp directory, or wherever
SC64_LZ_CACHE points. Nothing here is safe to commit.

    from lzcache import encode_cached
    packed, hit = encode_cached(chk, level=3)

Copyright (C) 2026 sc64-le contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import bolt_lzss

_DEFAULT = Path(tempfile.gettempdir()) / "sc64-lzcache"


def cache_dir() -> Path:
    d = Path(os.environ.get("SC64_LZ_CACHE") or _DEFAULT)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(payload: bytes, level: int) -> Path:
    return cache_dir() / f"{hashlib.sha256(payload).hexdigest()}-L{level}.lz"


def encode_cached(payload: bytes, level: int = 3) -> tuple[bytes, bool]:
    """(compressed, was_a_hit). Every result has passed a round trip."""
    path = _key(payload, level)
    if path.is_file():
        try:
            packed = path.read_bytes()
            if bolt_lzss.decode(packed, len(payload)) == payload:
                return packed, True
        except Exception:
            pass
        # Wrong or unreadable: drop it rather than let it through.
        path.unlink(missing_ok=True)

    packed = bolt_lzss.encode(payload, level)
    if bolt_lzss.decode(packed, len(payload)) != payload:
        raise ValueError("bolt_lzss round trip failed on freshly encoded data")

    # Write via a temporary file in the same directory: a build interrupted
    # mid-write must not leave a truncated entry that a later run trusts.
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(packed)
    os.replace(tmp, path)
    return packed, False


def stats() -> tuple[int, int]:
    """(entries, bytes) currently cached."""
    d = cache_dir()
    files = list(d.glob("*.lz"))
    return len(files), sum(f.stat().st_size for f in files)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Inspect or clear the bolt-lzss cache.")
    ap.add_argument("--clear", action="store_true")
    a = ap.parse_args(argv)
    n, size = stats()
    print(f"cache: {cache_dir()}")
    print(f"  {n} entries, {size / 1024 / 1024:.1f} MiB")
    if a.clear:
        for f in cache_dir().glob("*.lz"):
            f.unlink()
        print("  cleared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
