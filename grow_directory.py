#!/usr/bin/env python3
"""Grow BOLT directory 008 and build a cartridge with more than 36 melee maps.

    python grow_directory.py --maps I:\\path\\to\\ladder --count 48 -o out.z64

The 36-map ceiling is three constraints at once, and all three have to go:

  1. The preview arithmetic. A map's preview is fetched as map_id + 36 and its
     palette as map_id + 72, from two immediates at file 0xDAA5C and 0xDAA68.
     With 36 maps those are exactly minimal, so index 96 asks for image 0x88C
     -- which is preview palette 0 -- and palette 0x8B0, which does not exist.
  2. Directory 008 is full. 176 entries: 8 misc, 60 campaign maps, 36 melee
     maps, 36 previews, 36 palettes. For N melee maps you need 68 + 3N.
  3. Every new map needs a preview and a palette, or the fetch misses.

This does all three. The directory table is rebuilt at N = 48 -- 212 entries --
and written into free space, with root entry 8 repointed at it. The layout:

    000..043   68 entries, copied verbatim (8 misc + 60 campaign maps)
    044..073   48 melee maps
    074..0A3   48 preview images
    0A4..0D3   48 preview palettes

Entry records are just (size, offset) pairs, so the 12 extra previews and
palettes do not need new artwork: they point at the cartridge's own
"INTELLIGENCE NOT AVAILABLE" placeholder, which it already ships for two maps
whose previews were never drawn.

Requires --expand-style headroom, so the ROM is doubled to 64 MiB here for the
same reason ladder_edition does it: the stock tail padding is only ~313 KiB and
48 compressed maps will not fit in it.

No game data is distributed by this script. It reads a cartridge you supply.

Copyright (C) 2026 sc64-le contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""
from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from pathlib import Path

import bolt_lzss
import n64crc

import _deps  # noqa: F401  (puts sc64-maps on sys.path)
from extract_sc64_maps import (BOLT_ENTRY_SIZE, BOLT_HEADER_SIZE, BoltArchive,
                               chk_sections, load_rom, parse_map)
from ladder_edition import normalise
from lzcache import encode_cached, stats
from pc_maps import read_chk
from sc64 import find_rom

ALIGN = 16
FLAG_UNCOMPRESSED = 0x08

MELEE_BASE = 60
MISC = 0x008              # files 000..007 are not maps
FIRST_MELEE = 0x044       # index 60
CAMPAIGN_END = FIRST_MELEE
ROOT_DIR8 = 8

# The two preview immediates, and the 1v1 selector length.
IMAGE_SITE, PALETTE_SITE = 0xDAA5C, 0xDAA68
IMAGE_EXPECT, PALETTE_EXPECT = 0x26040024, 0x26100048
LIST_LEN_OFFSET, LIST_LEN_EXPECT = 0x0DAB78, 0x2406001B

# Map index 93 -- Resurrection IV -- ships the "INTELLIGENCE NOT AVAILABLE"
# placeholder rather than a drawn thumbnail, so its preview and palette are the
# natural stand-in for maps that have none.
PLACEHOLDER_INDEX = 93


def records(rom: bytes, arc: BoltArchive, table: int, count: int) -> list[bytes]:
    """The raw 16-byte records of a directory table."""
    at = arc.base + table
    return [bytes(rom[at + i * BOLT_ENTRY_SIZE: at + (i + 1) * BOLT_ENTRY_SIZE])
            for i in range(count)]


def make_record(old: bytes, size: int, offset: int, compressed: bool) -> bytes:
    """A record with new size/offset, keeping type and hash from `old`."""
    flags = (old[0] & ~FLAG_UNCOMPRESSED) if compressed else (old[0] | FLAG_UNCOMPRESSED)
    return (bytes([flags, old[1], old[2], old[3]])
            + struct.pack(">III", size, offset, struct.unpack_from(">I", old, 12)[0]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--maps", required=True, help="directory of .scm/.scx maps")
    ap.add_argument("--count", type=int, default=48, help="melee maps to install")
    ap.add_argument("--skip", action="append", default=[],
                    help="exclude maps whose filename contains this (repeatable). "
                         "The next unique map in order takes the vacated place, "
                         "so the count still comes out at --count")
    ap.add_argument("--rom", default=None)
    ap.add_argument("-o", "--out", default="sc64_grown.z64")
    ap.add_argument("--level", type=int, default=3)
    a = ap.parse_args(argv)

    n = a.count
    need = 68 + 3 * n
    if need > 256:
        sys.exit(f"error: {n} maps needs {need} entries; a directory holds 256")

    rom_path = find_rom(a.rom)
    if rom_path is None:
        sys.exit("no ROM found; pass --rom")
    rom = bytearray(load_rom(rom_path))
    variant = n64crc.detect(bytes(rom))
    if variant is None:
        sys.exit("error: unrecognised ROM -- checksum matches no CIC variant")

    for site, want in ((IMAGE_SITE, IMAGE_EXPECT), (PALETTE_SITE, PALETTE_EXPECT)):
        got = struct.unpack_from(">I", rom, site)[0]
        if got != want:
            sys.exit(f"error: {site:#x} is {got:#010x}, expected {want:#010x}")

    stock = BoltArchive(bytes(rom))
    root = records(bytes(rom), stock, BOLT_HEADER_SIZE - BOLT_HEADER_SIZE,
                   stock.num_entries or 256)
    # root table starts right after the header
    root = [bytes(rom[stock.base + BOLT_HEADER_SIZE + i * BOLT_ENTRY_SIZE:
                      stock.base + BOLT_HEADER_SIZE + (i + 1) * BOLT_ENTRY_SIZE])
            for i in range(stock.num_entries or 256)]
    d8 = root[ROOT_DIR8]
    d8_count, d8_off = d8[3], struct.unpack_from(">I", d8, 8)[0]
    old = records(bytes(rom), stock, d8_off, d8_count)
    print(f"ROM {Path(rom_path).name}  CIC {variant}")
    print(f"directory 008: {d8_count} entries at BOLT+{d8_off:#x}")

    old_first_preview = FIRST_MELEE + 36
    old_first_palette = old_first_preview + 36
    ph_img = old[old_first_preview + (PLACEHOLDER_INDEX - MELEE_BASE)]
    ph_pal = old[old_first_palette + (PLACEHOLDER_INDEX - MELEE_BASE)]

    rom.extend(bytes(len(rom)))
    print(f"expanded to {len(rom):,} bytes ({len(rom) // 2**20} MiB)")

    # --- pick the maps ---------------------------------------------------
    sources = sorted(Path(a.maps).glob("**/*.sc*"))
    seen, unique = set(), []
    for src in sources:
        try:
            key = hashlib.sha256(normalise(read_chk(src))[0]).hexdigest()
        except Exception:
            continue
        if key not in seen:
            seen.add(key)
            unique.append(src)
    print(f"{len(sources)} files -> {len(unique)} unique scenarios")
    for pat in a.skip:
        dropped_maps = [u for u in unique if pat.lower() in u.name.lower()]
        unique = [u for u in unique if pat.lower() not in u.name.lower()]
        for d in dropped_maps:
            print(f"  --skip {pat}: excluding {d.name}")
        if not dropped_maps:
            print(f"  --skip {pat}: matched nothing")
    if len(unique) < n:
        sys.exit(f"error: only {len(unique)} unique maps, need {n}")
    unique = unique[:n]

    # --- write map payloads into the new half ----------------------------
    cursor = (len(rom) // 2 + ALIGN - 1) & ~(ALIGN - 1)

    def place(payload: bytes) -> int:
        nonlocal cursor
        at = cursor
        if at + len(payload) > len(rom):
            sys.exit("error: out of room in the expanded half")
        rom[at:at + len(payload)] = payload
        cursor = (at + len(payload) + ALIGN - 1) & ~(ALIGN - 1)
        return at - stock.base           # BOLT-relative

    map_recs = []
    hits = 0
    print(f"\ninstalling {n} maps into files {FIRST_MELEE:#05x}.."
          f"{FIRST_MELEE + n - 1:#05x} (indices {MELEE_BASE}..{MELEE_BASE + n - 1})")
    for i, src in enumerate(unique):
        chk, dropped, dupes = normalise(read_chk(src))
        info = parse_map(src.name, chk)
        packed, hit = encode_cached(chk, a.level)
        hits += hit
        off = place(packed)
        template = old[FIRST_MELEE + i] if i < 36 else old[FIRST_MELEE]
        map_recs.append(make_record(template, len(chk), off, True))
        if i < 3 or i >= n - 2:
            print(f"  [{MELEE_BASE + i:>3}] {src.name[:30]:30} "
                  f"{info.width}x{info.height} {len(chk):8,} -> {len(packed):7,}")
        elif i == 3:
            print(f"  ... {n - 5} more ...")

    cached, cached_bytes = stats()
    print(f"  compression: {hits}/{n} served from cache "
          f"({cached} entries, {cached_bytes / 1024 / 1024:.1f} MiB)")

    # --- assemble the new table ------------------------------------------
    table = []
    table += old[:CAMPAIGN_END]                      # misc + campaign
    table += map_recs                                # n melee maps
    table += old[old_first_preview:old_first_preview + 36]
    table += [ph_img] * (n - 36)                     # placeholder previews
    table += old[old_first_palette:old_first_palette + 36]
    table += [ph_pal] * (n - 36)                     # placeholder palettes
    assert len(table) == need, f"built {len(table)} entries, wanted {need}"

    blob = b"".join(table)
    table_off = place(blob)
    print(f"\nnew directory table: {len(table)} entries, "
          f"{len(blob):,} bytes at BOLT+{table_off:#x}")

    # --- repoint root entry 8 --------------------------------------------
    at = stock.base + BOLT_HEADER_SIZE + ROOT_DIR8 * BOLT_ENTRY_SIZE
    rom[at + 3] = need & 0xFF
    struct.pack_into(">I", rom, at + 8, table_off)
    old_size = struct.unpack_from(">I", rom, at + 4)[0]
    struct.pack_into(">I", rom, at + 4, old_size + cursor - (len(rom) // 2))
    print(f"root entry 8 -> {need} entries at BOLT+{table_off:#x}")

    # --- the three constants ---------------------------------------------
    wi = struct.unpack_from(">I", rom, IMAGE_SITE)[0]
    wp = struct.unpack_from(">I", rom, PALETTE_SITE)[0]
    struct.pack_into(">I", rom, IMAGE_SITE, (wi & 0xFFFF0000) | n)
    struct.pack_into(">I", rom, PALETTE_SITE, (wp & 0xFFFF0000) | (2 * n))
    print(f"preview arithmetic -> image +{n}, palette +{2 * n}")

    have = struct.unpack_from(">I", rom, LIST_LEN_OFFSET)[0]
    if have == LIST_LEN_EXPECT:
        struct.pack_into(">I", rom, LIST_LEN_OFFSET, (have & 0xFFFF0000) | n)
        print(f"1v1 selector length -> {n}")
    else:
        print(f"  warning: {LIST_LEN_OFFSET:#x} is {have:#010x}, left alone")

    c1, c2 = n64crc.fix(rom, variant)
    print(f"boot checksum repaired -> {c1:#010x} {c2:#010x}")

    out = Path(a.out)
    out.write_bytes(rom)

    # --- read it all back through the ordinary walk -----------------------
    back = BoltArchive(bytes(rom))
    by = {e.path: e for e in back.entries()}
    ok = bad = 0
    for i in range(n):
        slot = f"008/{FIRST_MELEE + i:03X}"
        e = by.get(slot)
        try:
            got = back.read(e)
            tags = {t for t, _ in chk_sections(got)}
            assert {b"VER ", b"DIM ", b"MTXM"} <= tags
            ok += 1
        except Exception:
            print(f"  READ-BACK FAILED: {slot}")
            bad += 1
    prev = sum(1 for i in range(n) if f"008/{FIRST_MELEE + n + i:03X}" in by)
    pal = sum(1 for i in range(n) if f"008/{FIRST_MELEE + 2 * n + i:03X}" in by)
    print(f"\nread-back: {ok}/{n} maps parse, {prev}/{n} preview slots present, "
          f"{pal}/{n} palette slots present")
    print(f"directory 008 now walks {sum(1 for e in back.entries() if e.path.startswith('008/'))} entries")
    print(f"other directories still walkable: "
          f"{sum(1 for e in back.entries() if not e.path.startswith('008/'))}")
    print(f"\nwrote {out}  ({out.stat().st_size:,} bytes)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
