#!/usr/bin/env python3
"""Replace the "ACCESSING MISSION DATA..." loading screens with your own art.

    python loading_screen.py --image ladder.png -o sc64_ladder_edition.z64
    python loading_screen.py --image ladder.png --all -o out.z64

The caption is painted INTO the artwork -- there is no string anywhere in the
ROM -- so whatever you supply is the whole screen, caption included. Draw your
own text or leave it out.

There are eight of them in BOLT directory 009, each an image followed by its
own palette:

    009/000  009/002  009/004  009/006  009/008  009/00A  009/00C  009/00E

009/00C is the one this project has seen on every failed load. 009/00E is the
odd one at 512x384; the rest are 320x240. Which one the game shows when is not
known, so `--all` is the honest default for a themed build: replace one and it
turns up only some of the time.

This is an easier target than the title screen. `title_brand.py` writes palette
INDICES into an existing image and is therefore limited to colours the title
already had -- its ink is index 66 because that is the game's gold. Here the
paired palette is replaced too, so the image is an arbitrary 256 colours. And
because these entries are LZSS-compressed rather than written in place, the
payload is not bound by the original's length.

No checksum repair is needed or done: BOLT begins at 0x12CA10 and the boot
checksum covers only 0x1000..0x101000, so nothing written here is inside it.
Anything that patches the static segment -- the Scenario table, the 1v1 list
length -- does need it, which is what n64crc.py is for.

The artwork you replace is Blizzard's. Do this to a cartridge you own.

Copyright (C) 2026 sc64-le contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

import bolt_lzss

import _deps  # noqa: F401  (puts sc64-maps on sys.path)
from extract_sc64_maps import BoltArchive, load_rom
from inject_map import dir_entry_offset, tail_free_start
from sc64 import find_rom

try:
    from PIL import Image
except ImportError:                                     # pragma: no cover
    sys.exit("this tool needs Pillow:  pip install Pillow")

FLAG_UNCOMPRESSED = 0x08
ALIGN = 16

# Image entry: u32 bpp, u32 0, u16 width, u16 height, u32 0, then width*height
# palette indices. Palette entry: a 6-byte prefix then 256 big-endian RGBA5551
# words. The prefix is 00 00 00 00 00 ff on every palette in the cartridge, so
# it is copied from the entry being replaced rather than invented.
IMAGE_HEADER = 16
PALETTE_SIZE = 518
PALETTE_PREFIX = 6

# The eight loading screens, image slot -> palette slot. Directory 009 pairs
# each image with the palette in the very next entry.
SCREENS = [(f"009/{n:03X}", f"009/{n + 1:03X}")
           for n in (0x000, 0x002, 0x004, 0x006, 0x008, 0x00A, 0x00C, 0x00E)]
DEFAULT_SLOT = "009/00C"


def to_rgba5551(r: int, g: int, b: int) -> int:
    """One RGBA5551 word, alpha always set.

    Five-bit channels, then a single alpha bit: RRRRRGGGGGBBBBBA. Every one of
    the 256 entries in all eight shipped palettes has that bit set, so these
    are opaque backgrounds and there is no transparency to preserve.
    """
    return ((r >> 3) << 11) | ((g >> 3) << 6) | ((b >> 3) << 1) | 1


def quantise(img: Image.Image, size: tuple[int, int], fit: str,
             dither: bool) -> tuple[bytes, list[tuple[int, int, int]]]:
    """Fit `img` to `size` and reduce it to 256 colours.

    Pillow's median cut is used rather than a hand-rolled quantiser. It is
    already a dependency of this repository (the emulator harness reads
    framebuffers with it), and a worse median cut written here would only be
    worse -- the interesting part of this tool is the cartridge side.
    """
    img = img.convert("RGB")
    w, h = size
    if fit == "stretch":
        img = img.resize((w, h), Image.LANCZOS)
    else:
        scale = (max if fit == "cover" else min)(w / img.width, h / img.height)
        scaled = img.resize((max(1, round(img.width * scale)),
                             max(1, round(img.height * scale))), Image.LANCZOS)
        canvas = Image.new("RGB", (w, h), (0, 0, 0))
        canvas.paste(scaled, ((w - scaled.width) // 2, (h - scaled.height) // 2))
        img = canvas

    d = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    q = img.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=d)

    # getpalette() returns only the colours actually used, which is often
    # fewer than 256; the rest are padded black rather than left as empty
    # tuples that pack() would choke on.
    flat = q.getpalette() or []
    n = min(len(flat) // 3, 256)
    table = [tuple(flat[i * 3:i * 3 + 3]) for i in range(n)]
    table += [(0, 0, 0)] * (256 - n)
    return q.tobytes(), table


def build_entries(pixels: bytes, table, size: tuple[int, int],
                  prefix: bytes) -> tuple[bytes, bytes]:
    """(image entry, palette entry) in cartridge form."""
    w, h = size
    image = struct.pack(">IIHHI", 8, 0, w, h, 0) + pixels
    words = b"".join(struct.pack(">H", to_rgba5551(*c)) for c in table)
    return image, prefix + words


def _install(rom: bytearray, slot: str, payload: bytes, level: int) -> int:
    """Compress `payload` into `slot`. Returns the compressed length.

    The archive is re-read per write because the previous one moved where the
    tail padding starts, and the next payload has to land after it.
    """
    packed = bolt_lzss.encode(payload, level)
    if bolt_lzss.decode(packed, len(payload)) != payload:
        sys.exit(f"error: {slot} failed its compression round trip")
    arc = BoltArchive(bytes(rom))
    rec = dir_entry_offset(arc, slot)
    old = arc._entry(slot, rec)
    dest = (tail_free_start(bytes(rom)) + ALIGN - 1) & ~(ALIGN - 1)
    if dest + len(packed) > len(rom):
        sys.exit(f"error: out of room writing {slot}; "
                 "rebuild the ROM with ladder_edition.py --expand")
    rom[dest:dest + len(packed)] = packed
    abs_rec = arc.base + rec
    rom[abs_rec] = old.flags & ~FLAG_UNCOMPRESSED
    struct.pack_into(">I", rom, abs_rec + 4, len(payload))
    struct.pack_into(">I", rom, abs_rec + 8, dest - arc.base)
    return len(packed)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Replace StarCraft 64's loading-screen artwork.")
    ap.add_argument("--image", help="PNG (or anything Pillow reads) to install")
    ap.add_argument("--rom", default=None,
                    help="ROM to patch; found automatically if omitted")
    ap.add_argument("-o", "--out", default="sc64_loading.z64")
    ap.add_argument("--slot", default=DEFAULT_SLOT,
                    help=f"which screen to replace (default {DEFAULT_SLOT})")
    ap.add_argument("--all", action="store_true",
                    help="replace all eight, so the art always shows")
    ap.add_argument("--fit", choices=("contain", "cover", "stretch"),
                    default="contain",
                    help="how to fit the source to the screen (default contain)")
    ap.add_argument("--dither", action="store_true",
                    help="Floyd-Steinberg dithering; helps on photographic art")
    ap.add_argument("--level", type=int, default=3, help="bolt-lzss level")
    ap.add_argument("-l", "--list", action="store_true",
                    help="report the eight screens and exit")
    a = ap.parse_args(argv)

    rom_path = find_rom(a.rom)
    if rom_path is None:
        sys.exit("no ROM found; pass --rom")
    rom = bytearray(load_rom(rom_path))
    arc = BoltArchive(bytes(rom))
    by = {e.path: e for e in arc.entries()}

    if a.list:
        print(f"{'image':9} {'palette':9} {'size':>11}  dimensions")
        for ip, pp in SCREENS:
            raw = arc.read(by[ip])
            w, h = struct.unpack_from(">HH", raw, 8)
            print(f"{ip:9} {pp:9} {len(raw):>11,}  {w}x{h}"
                  + ("   <- seen on every failed load" if ip == DEFAULT_SLOT else ""))
        return 0

    if not a.image:
        sys.exit("--image is required (or use --list)")
    targets = SCREENS if a.all else [
        (ip, pp) for ip, pp in SCREENS if ip == a.slot]
    if not targets:
        sys.exit(f"{a.slot} is not a loading screen; try --list")

    src = Image.open(a.image)
    print(f"ROM {Path(rom_path).name}")
    print(f"source {a.image}  {src.width}x{src.height}  "
          f"fit={a.fit} dither={'on' if a.dither else 'off'}\n")

    for ip, pp in targets:
        raw = arc.read(by[ip])
        w, h = struct.unpack_from(">HH", raw, 8)
        prefix = arc.read(by[pp])[:PALETTE_PREFIX]

        pixels, table = quantise(src, (w, h), a.fit, a.dither)
        image, palette = build_entries(pixels, table, (w, h), prefix)
        if len(palette) != PALETTE_SIZE:
            sys.exit(f"built a {len(palette)}-byte palette, expected {PALETTE_SIZE}")

        ci = _install(rom, ip, image, a.level)
        cp = _install(rom, pp, palette, a.level)
        print(f"  {ip} {w}x{h}  image {len(image):,} -> {ci:,}   "
              f"palette {pp} {len(palette)} -> {cp}")

    out = Path(a.out)
    out.write_bytes(rom)

    # Read back through the ordinary archive walk, and check the palette
    # survives a decode -- a palette that round-trips as garbage would still
    # install cleanly and only show itself on the television.
    back = BoltArchive(bytes(rom))
    byb = {e.path: e for e in back.entries()}
    bad = 0
    for ip, pp in targets:
        gi, gp = back.read(byb[ip]), back.read(byb[pp])
        bpp, _, w, h, _ = struct.unpack(">IIHHI", gi[:IMAGE_HEADER])
        ok = (bpp == 8 and len(gi) == IMAGE_HEADER + w * h
              and len(gp) == PALETTE_SIZE)
        if not ok:
            print(f"  READ-BACK FAILED: {ip}")
            bad += 1
    print(f"\nread-back: {len(targets) - bad}/{len(targets)} screens ok")
    print(f"other entries still walkable: "
          f"{sum(1 for e in back.entries() if not e.path.startswith('009/'))}")
    print(f"wrote {out}  ({out.stat().st_size:,} bytes)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
