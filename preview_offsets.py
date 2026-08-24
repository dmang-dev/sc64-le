#!/usr/bin/env python3
"""Read or repoint the map-preview arithmetic.

    python preview_offsets.py --list
    python preview_offsets.py --images 48 --palettes 96 -o out.z64

When a melee map is selected the engine fetches its preview by adding two
constants to the map's own BOLT resource id:

    preview image   = map_id + 36
    preview palette = map_id + 72

There is no preview table and no base pointer -- just those two offsets, which
is why looking for a base constant never found one. They are the immediates of

    0xDAA5C   addiu a0, s0, 36
    0xDAA68   addiu s0, s0, 72

which is code DMA'd to 0x8012400C at run time. It is NOT compressed and NOT in
BOLT: it sits uncompressed in the static region, at a file offset the usual
RAM-to-file delta does not predict, because this block relocates elsewhere.
Verified end to end -- changing 36 to 37 makes the engine fetch 0x869 instead
of 0x868, observed on the resource getter at 0x80064D60.

Both offsets are inside the boot checksum window (0x1000..0x101000), so the
header is repaired here. Skip that and IPL3 refuses to hand off: black screen,
no code runs, nothing to distinguish it from "the patch did nothing".

WHY YOU WOULD CHANGE THEM. The pair caps the melee range at 36 maps, and not
because of space. Maps occupy ids 0x844 upward, so images must start at least
N ids past the first map and palettes at least N past the images -- K1 >= N and
K2 >= K1 + N. The cartridge ships K1 = 36 and K2 = 72 with 36 maps, which is
exactly the minimum; index 96 therefore asks for image 0x88C, which is preview
palette 0, and palette 0x8B0, which does not exist at all since directory 008
ends at 0x0AF.

So more maps needs THREE things, not one: this patch, room in directory 008 for
N previews and N palettes (68 + 3N entries in total, against the 176 it ships
with), and a preview built for every new map. This tool only does the first.

Copyright (C) 2026 sc64-le contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

import n64crc

import _deps  # noqa: F401  (puts sc64-maps on sys.path)
from extract_sc64_maps import load_rom
from sc64 import find_rom

IMAGE_SITE = 0xDAA5C          # addiu a0, s0, <image offset>
PALETTE_SITE = 0xDAA68        # addiu s0, s0, <palette offset>
IMAGE_EXPECT = 0x26040024     # addiu a0, s0, 36
PALETTE_EXPECT = 0x26100048   # addiu s0, s0, 72

MELEE_BASE = 60
FIRST_MAP_ID = 0x808 + MELEE_BASE     # 0x844
DIR_008_ENTRIES = 176


def read_sites(rom: bytes) -> tuple[int, int, int, int]:
    a = struct.unpack_from(">I", rom, IMAGE_SITE)[0]
    b = struct.unpack_from(">I", rom, PALETTE_SITE)[0]
    return a, b, a & 0xFFFF, b & 0xFFFF


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Read or repoint the map-preview offsets.")
    ap.add_argument("--rom", default=None,
                    help="ROM to read; found automatically if omitted")
    ap.add_argument("-o", "--out")
    ap.add_argument("--images", type=int,
                    help="new image offset (map_id + this)")
    ap.add_argument("--palettes", type=int,
                    help="new palette offset (map_id + this)")
    ap.add_argument("-l", "--list", action="store_true")
    a = ap.parse_args(argv)

    rom_path = find_rom(a.rom)
    if rom_path is None:
        sys.exit("no ROM found; pass --rom")
    rom = bytearray(load_rom(rom_path))
    wi, wp, cur_i, cur_p = read_sites(bytes(rom))

    if wi & 0xFFFF0000 != IMAGE_EXPECT & 0xFFFF0000 or \
            wp & 0xFFFF0000 != PALETTE_EXPECT & 0xFFFF0000:
        sys.exit(f"error: {IMAGE_SITE:#x} is {wi:#010x} and {PALETTE_SITE:#x} "
                 f"is {wp:#010x}; neither looks like the expected addiu, so "
                 "this is not a ROM this tool understands")

    print(f"ROM {Path(rom_path).name}")
    print(f"  image offset   {cur_i:>4}   (file {IMAGE_SITE:#x}, {wi:#010x})")
    print(f"  palette offset {cur_p:>4}   (file {PALETTE_SITE:#x}, {wp:#010x})")
    n = min(cur_i, cur_p - cur_i)
    print(f"  supports {n} melee maps: images {FIRST_MAP_ID + cur_i:#05x}.."
          f"{FIRST_MAP_ID + cur_i + n - 1:#05x}, palettes "
          f"{FIRST_MAP_ID + cur_p:#05x}..{FIRST_MAP_ID + cur_p + n - 1:#05x}")

    if a.list or (a.images is None and a.palettes is None):
        return 0

    new_i = cur_i if a.images is None else a.images
    new_p = cur_p if a.palettes is None else a.palettes
    n = min(new_i, new_p - new_i)
    if new_p <= new_i:
        sys.exit("error: the palette offset must exceed the image offset, or "
                 "the two regions overlap")
    print(f"\n  -> image {new_i}, palette {new_p}: room for {n} maps")

    top = FIRST_MAP_ID + new_p + n - 1
    if top > 0x800 + DIR_008_ENTRIES - 1:
        print(f"  warning: the highest palette id would be {top:#05x}, past "
              f"the end of directory 008 ({0x800 + DIR_008_ENTRIES - 1:#05x}). "
              "The directory needs growing first or the fetch will miss.")

    if not a.out:
        sys.exit("error: pass -o to write the patched ROM")
    variant = n64crc.detect(bytes(rom))
    if variant is None:
        sys.exit("error: unrecognised ROM -- checksum matches no CIC variant")
    struct.pack_into(">I", rom, IMAGE_SITE, (wi & 0xFFFF0000) | (new_i & 0xFFFF))
    struct.pack_into(">I", rom, PALETTE_SITE, (wp & 0xFFFF0000) | (new_p & 0xFFFF))
    c1, c2 = n64crc.fix(rom, variant)
    out = Path(a.out)
    out.write_bytes(rom)
    print(f"  boot checksum repaired -> {c1:#010x} {c2:#010x}")
    print(f"  wrote {out}  ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
