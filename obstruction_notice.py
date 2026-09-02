#!/usr/bin/env python3
"""Make StarCraft 64's "too many obstructions" failure show its message.

Some maps -- complex island/maze terrain, many Brood War 256x256s -- cannot be
loaded: the engine's tile resolver gives up and the load aborts. The game even
computes a message for it,

    "The map could not be loaded because it had too many obstructions. Try
     widening corridors and reducing the number of small nooks and crannies..."

but the handler that would show it (0x8003CC40) was compiled out to `jr ra;
nop`, so on retail the message never renders and the "ACCESSING MISSION DATA..."
loading screen just hangs forever -- indistinguishable from a crash.

This patch turns that silent hang into a readable notice. How it works:

  * The loading-screen installer (0x800228A8) picks image `0x900 + 2*index`,
    where the index is 7 if [0x800BFEFC], else 6 if [0x800B1E8F], else the byte
    at [0x800D1182]. A melee load forces index 6 (0x90C), so indices 0-5 and 7
    are never shown by a melee load. We install the notice on index 5 (009/00A
    + its palette 009/00B), which a normal load never selects.
  * The stub 0x8003CC40 is trampolined (via a rodata alignment gap that is
    loaded and executable) to clear the index-6 flag, set the selector to 5,
    and re-run the installer -- proven safe to re-run mid-abort. The hung load
    then sits on the notice instead of the generic screen.

A passing map is untouched (verified: it loads normally). The notice replaces
whatever else uses loading-screen index 5 -- a non-melee context; acceptable
for a melee cartridge. The stub and trampoline are inside the boot-checksum
window, so n64crc repairs it.

No game data is redistributed. Reads a cartridge you supply, writes a copy.

    python obstruction_notice.py --rom sc64.z64 -o sc64_notice.z64

Copyright (C) 2026 sc64-le contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

import _deps  # noqa: F401  (puts sc64-maps on sys.path)
import n64crc
from extract_sc64_maps import load_rom
from loading_screen import PALETTE_PREFIX, _install, build_entries
from sc64 import find_rom
from title_brand import FONT, GLYPH_H, GLYPH_W, draw_text

W, H = 320, 240
DELTA = 0xC00                    # ROM off = (vram - 0x80000000) + DELTA
STUB = 0x8003CC40               # the compiled-out assert handler
TRAMP = 0x8000063C             # a 140-byte rodata gap, loaded + executable
SETUP = 0x800228A8            # the loading-screen installer
FLAG6 = 0x800B1E8F           # non-zero forces screen index 6 (0x90C)
SELECTOR = 0x800D1182       # the general screen-index byte

# Newlines (real, or the literal "\n", or "|") force a break; a blank line is a
# gap; everything else is word-wrapped to fit. The 8x8 font only defines upper
# case, digits, space and - . : , so anything else is dropped (with a warning).
DEFAULT_MESSAGE = ("TOO MANY OBSTRUCTIONS\n\n"
                   "MAP TOO COMPLEX\nFOR STARCRAFT 64\n\n"
                   "WIDEN CORRIDORS REDUCE NOOKS.")

LINE_GAP = 3                     # blank pixels between lines, in font units


def rom_off(vram: int) -> int:
    return (vram - 0x80000000) + DELTA


def _clean(text: str) -> str:
    """Upper-case and keep only glyphs the font defines; report the rest."""
    up = text.upper()
    kept = "".join(c for c in up if c in FONT)
    dropped = sorted({c for c in up if c not in FONT and not c.isspace()})
    if dropped:
        print(f"warning: dropping characters the font lacks: "
              f"{' '.join(repr(c) for c in dropped)}", file=sys.stderr)
    return kept


def _layout(message: str) -> tuple[list[str], int]:
    """Wrap `message` to fit 320x240 and pick the largest scale that fits.

    Explicit breaks are honoured; other whitespace is a word boundary. Returns
    (render lines, scale). Falls to scale 1 if scale 2 overflows; a single word
    too long even at scale 1 is left to clip at the edge.
    """
    logical = _clean(message).replace("|", "\n").replace("\\n", "\n").split("\n")
    for scale in (2, 1):
        max_chars = W // ((GLYPH_W + 1) * scale)
        lines: list[str] = []
        for para in logical:
            if not para.strip():
                lines.append("")
                continue
            cur = ""
            for word in para.split():
                cand = (cur + " " + word).strip()
                if len(cand) <= max_chars or not cur:
                    cur = cand
                else:
                    lines.append(cur)
                    cur = word
            lines.append(cur)
        fits_wide = all(len(ln) <= max_chars for ln in lines)
        fits_tall = (GLYPH_H + LINE_GAP) * scale * len(lines) <= H
        if fits_wide and fits_tall:
            return lines, scale
    return lines, 1                          # scale 1, clip the overflow


def _notice_entries(index: int, lines: list[str], scale: int) -> tuple[bytes, bytes]:
    px = bytearray(W * H)                    # index 0 = background
    line_h = (GLYPH_H + LINE_GAP) * scale
    y = max(0, (H - line_h * len(lines)) // 2)
    for text in lines:
        tw = len(text) * (GLYPH_W + 1) * scale
        if text:
            draw_text(px, W, H, text, max(0, (W - tw) // 2), y, scale,
                      ink=1, shadow=None)
        y += line_h
    table = [(0, 0, 0)] * 256
    table[0] = (24, 8, 8)                    # dark ground
    table[1] = (255, 176, 32)               # amber, the screen's own caption hue
    return build_entries(bytes(px), table, (W, H), bytes(PALETTE_PREFIX))


def _jal(t: int) -> int: return 0x0C000000 | ((t >> 2) & 0x03FFFFFF)
def _j(t: int) -> int:   return 0x08000000 | ((t >> 2) & 0x03FFFFFF)


def _patch_code(rom: bytearray, index: int) -> None:
    tramp = [
        0x27BDFFF8,                          # addiu sp, sp, -8
        0xAFBF0000,                          # sw   ra, 0(sp)
        0x3C080000 | (FLAG6 >> 16),          # lui  t0, hi(FLAG6)
        0xA1000000 | (FLAG6 & 0xFFFF),       # sb   zero, lo(FLAG6)(t0)
        0x3C080000 | (SELECTOR >> 16),       # lui  t0, hi(SELECTOR)
        0x24090000 | (index & 0xFFFF),       # addiu t1, zero, index
        0xA1090000 | (SELECTOR & 0xFFFF),    # sb   t1, lo(SELECTOR)(t0)
        _jal(SETUP),                         # jal  0x800228A8
        0x00000000,                          # nop
        0x8FBF0000,                          # lw   ra, 0(sp)
        0x03E00008,                          # jr   ra
        0x27BD0008,                          # addiu sp, sp, 8
    ]
    off = rom_off(TRAMP)
    for i, w in enumerate(tramp):
        struct.pack_into(">I", rom, off + i * 4, w)
    struct.pack_into(">I", rom, rom_off(STUB), _j(TRAMP))
    struct.pack_into(">I", rom, rom_off(STUB) + 4, 0x00000000)


def apply(rom: bytearray, index: int = 5, level: int = 3,
          message: str = DEFAULT_MESSAGE) -> None:
    """Install the notice and trampoline the stub, in place. n64crc after."""
    lines, scale = _layout(message)
    img, pal = _notice_entries(index, lines, scale)
    _install(rom, f"009/{index * 2:03X}", img, level)
    _install(rom, f"009/{index * 2 + 1:03X}", pal, level)
    _patch_code(rom, index)
    n64crc.fix(rom, n64crc.detect(bytes(rom)) or "6101")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rom", default=None)
    ap.add_argument("-o", "--out", default="sc64_notice.z64")
    ap.add_argument("--index", type=int, default=5,
                    help="loading-screen index to install the notice on "
                         "(default 5 = 009/00A, which a melee load never shows)")
    ap.add_argument("--message", default=DEFAULT_MESSAGE,
                    help="notice text. Word-wrapped to fit; use a newline, the "
                         r"literal \n, or | to force a line break, and a blank "
                         "line for a gap. Upper-cased; the 8x8 font supports "
                         "A-Z 0-9 space and - . : only.")
    ap.add_argument("--level", type=int, default=3)
    a = ap.parse_args(argv)
    if not 0 <= a.index <= 7 or a.index == 6:
        sys.exit("error: --index must be 0-5 or 7 (6 is the melee load screen)")
    rom_path = find_rom(a.rom)
    if rom_path is None:
        sys.exit("no ROM found; pass --rom")
    rom = bytearray(load_rom(rom_path))
    apply(rom, a.index, a.level, a.message)
    Path(a.out).write_bytes(rom)
    print(f"wrote {a.out}: notice on loading-screen index {a.index} "
          f"(009/{a.index * 2:03X}); the too-many-obstructions hang now shows it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
