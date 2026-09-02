#!/usr/bin/env python3
"""Turn StarCraft 64's dead error handler into a generic exception screen.

The engine calls a handler, `void Error(int code, const char *msg, ...)` at
0x8003CC40, whenever a load or an assertion fails -- "too many obstructions"
is one such message, but there are others, and the argument is only known at
runtime. Retail compiled that handler out to `jr ra; nop`, so every one of
those failures is a silent hang on the "ACCESSING MISSION DATA..." screen.

Unlike obstruction_notice.py, which shows one message baked into an image, this
draws the actual `a1` string at runtime -- the too-many-obstructions text, or
any other message the engine passes.

How it works: the stub is trampolined to a small routine in unused RAM. Error
fires from deep in the tile resolver, after the loading screen has already been
drawn and the frame has settled (the resolver then spins), so the framebuffer
is static and safe to write. The routine finds the framebuffer the video
interface is scanning by walking the live osViContext (the same bss chain
libultra's retrace handler reads), clears it and blits the message in 16-bit
white -- upper-casing a-z, dropping glyphs the font lacks, char-wrapping at the
edge -- then returns. It calls nothing and touches only RAM, so it cannot
re-enter the busy resource system the way the engine's own draw routines would.

Because the pixels go straight to the RDRAM framebuffer, this is what the VI --
and the cycle-accurate Ares64 core -- scans out. It does NOT display under the
Mupen64Plus HLE core, which renders from the display list rather than scanning
RDRAM; use Ares64 (the harness default) or hardware to see it.

The font and the routine live in a run of bytes at VRAM 0x800D1538 that stays
untouched through a map load. Everything patched is inside the boot-checksum
window, so n64crc repairs it. No game data is redistributed: reads a cartridge
you supply, writes a copy.

    python exception_screen.py --rom sc64.z64 -o sc64_exc.z64

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
from sc64 import find_rom
from title_brand import FONT

DELTA = 0xC00                    # ROM off = (vram - 0x80000000) + DELTA
STUB = 0x8003CC40               # the compiled-out Error() handler
# The live osViContext pointer: [VICTX] -> context, context+4 is its framep --
# what the VI is scanning. This is exactly what libultra's retrace handler reads
# to program VI_ORIGIN; a CPU load of the register itself is not emulated
# reliably, but this bss chain is.
VICTX = 0x800CFA20             # = 0x800D0000 - 0x5E0
FB_W, FB_H = 320, 240         # 16-bit RGBA5551, VI_WIDTH 0x140
WHITE = 0xFFFF                 # ink
GROUND = 0x1843               # dark ground (rgba5551 of ~(24,8,8))
MARGIN = 8

# A run of bytes loaded flat at boot that stays zero through a map load.
FREE = 0x800D1538
FONT_VRAM = 0x800D1540         # 96 * 8 = 0x300
BLIT = 0x800D1840             # the trampoline + framebuffer blitter


def rom_off(vram: int) -> int:
    return (vram - 0x80000000) + DELTA


# --- a tiny MIPS assembler --------------------------------------------------
def _i(op, rs, rt, imm): return (op << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)
def _sp(rs, rt, rd, sh, fn): return (rs << 21) | (rt << 16) | (rd << 11) | (sh << 6) | fn
ZERO, AT, A1 = 0, 1, 5
T0, T1, T2, T3, T4, T5, T6, T7 = 8, 9, 10, 11, 12, 13, 14, 15
T8, T9, RA = 24, 25, 31


def lui(rt, imm): return _i(0x0F, 0, rt, imm)
def ori(rt, rs, imm): return _i(0x0D, rs, rt, imm)
def addiu(rt, rs, imm): return _i(0x09, rs, rt, imm)
def sltiu(rt, rs, imm): return _i(0x0B, rs, rt, imm)
def lw(rt, off, base): return _i(0x23, base, rt, off)
def lbu(rt, off, base): return _i(0x24, base, rt, off)
def sh(rt, off, base): return _i(0x29, base, rt, off)
def sll(rd, rt, s): return _sp(0, rt, rd, s, 0x00)
def srlv(rd, rt, rs): return _sp(rs, rt, rd, 0, 0x06)
def addu(rd, rs, rt): return _sp(rs, rt, rd, 0, 0x21)
def or_(rd, rs, rt): return _sp(rs, rt, rd, 0, 0x25)
def and_(rd, rs, rt): return _sp(rs, rt, rd, 0, 0x24)
def jr(rs): return _sp(rs, 0, 0, 0, 0x08)
def nop(): return 0
def beq(rs, rt, lbl): return ("beq", rs, rt, lbl)
def bne(rs, rt, lbl): return ("bne", rs, rt, lbl)
def L(name): return ("L", name)


def assemble(prog, base: int) -> bytes:
    addr, flat, pc = {}, [], base
    for it in prog:
        if isinstance(it, tuple) and it[0] == "L":
            addr[it[1]] = pc
        else:
            flat.append((pc, it)); pc += 4
    out = bytearray()
    for pc, it in flat:
        if isinstance(it, int):
            w = it
        elif it[0] in ("beq", "bne"):
            _, rs, rt, lbl = it
            w = _i(4 if it[0] == "beq" else 5, rs, rt, (addr[lbl] - (pc + 4)) >> 2)
        else:
            raise ValueError(it)
        out += struct.pack(">I", w)
    return bytes(out)


def _font_table() -> bytes:
    blank = (0,) * 8
    return b"".join(bytes(FONT.get(chr(c), blank)) for c in range(0x20, 0x80))


def _blitter() -> bytes:
    """Error stub -> here: clear the live framebuffer and draw the a1 string.

    Reached by `j` from the stub, so ra still holds the caller's return and a1
    still holds the message. Calls nothing; only temporaries and a1 are used.
    """
    p = [
        beq(A1, ZERO, "ret"), nop(),                      # no message -> leave be
        # t9 = framebuffer, uncached: (**VICTX + 4) | 0x20000000
        lui(T0, 0x800D),
        lw(T1, VICTX - 0x800D0000, T0),                   # t1 = [VICTX] = osViContext
        lw(T9, 4, T1),                                    # t9 = framep (KSEG0)
        lui(T0, 0x2000), or_(T9, T9, T0),                 # -> KSEG1 (uncached)
        # clear FB_W*FB_H halfwords to the ground colour
        ori(T4, ZERO, GROUND),
        addu(T0, T9, ZERO),
        lui(T1, (FB_W * FB_H * 2) >> 16), ori(T1, T1, (FB_W * FB_H * 2) & 0xFFFF),
        addu(T1, T9, T1),
        L("clr"),
        sh(T4, 0, T0), addiu(T0, T0, 2), bne(T0, T1, "clr"), nop(),
        # font base, cursor x=t2 y=t3
        lui(T8, FONT_VRAM >> 16), ori(T8, T8, FONT_VRAM & 0xFFFF),
        addiu(T2, ZERO, MARGIN), addiu(T3, ZERO, MARGIN),
        L("char"),
        lbu(T4, 0, A1), beq(T4, ZERO, "ret"), addiu(A1, A1, 1),
        addiu(T5, T4, -10), beq(T5, ZERO, "nl"), nop(),   # explicit newline
        sltiu(T5, T4, 0x61), bne(T5, ZERO, "up"), nop(),  # upper-case a-z
        sltiu(T5, T4, 0x7B), beq(T5, ZERO, "up"), nop(),
        addiu(T4, T4, -0x20),
        L("up"),
        sltiu(T5, T4, 0x20), bne(T5, ZERO, "sp"), nop(),  # clamp to font, else space
        sltiu(T5, T4, 0x80), bne(T5, ZERO, "gl"), nop(),
        L("sp"), addiu(T4, ZERO, 0x20),
        L("gl"),
        addiu(T5, T4, -0x20), sll(T5, T5, 3), addu(T6, T8, T5),  # glyph ptr
        ori(T4, ZERO, WHITE), addiu(T7, ZERO, 0),         # t4=ink, t7=ry
        L("row"),
        lbu(AT, 0, T6), addiu(T6, T6, 1),
        addu(T0, T3, T7), sll(T1, T0, 8), sll(T0, T0, 6), addu(T0, T0, T1),  # py*320
        addiu(T1, ZERO, 0),                               # rx
        L("col"),
        addiu(T5, ZERO, 0x80), srlv(T5, T5, T1), and_(T5, AT, T5),
        beq(T5, ZERO, "cn"), nop(),
        addu(T5, T2, T1), addu(T5, T5, T0), sll(T5, T5, 1), addu(T5, T9, T5),  # *2, +fb
        sh(T4, 0, T5),                                    # plot ink
        L("cn"),
        addiu(T1, T1, 1), sltiu(T5, T1, 8), bne(T5, ZERO, "col"), nop(),
        addiu(T7, T7, 1), sltiu(T5, T7, 8), bne(T5, ZERO, "row"), nop(),
        addiu(T2, T2, 9), sltiu(T5, T2, FB_W - 8), bne(T5, ZERO, "char"), nop(),
        L("nl"),
        addiu(T2, ZERO, MARGIN), addiu(T3, T3, 10),
        sltiu(T5, T3, FB_H - 9), bne(T5, ZERO, "char"), nop(),
        L("ret"),
        jr(RA), nop(),
    ]
    return assemble(p, BLIT)


def _j(t): return 0x08000000 | ((t >> 2) & 0x03FFFFFF)


def apply(rom: bytearray) -> None:
    """Install the font, the blitter, and the stub trampoline, in place."""
    font, blit = _font_table(), _blitter()
    for vram, blob in ((FONT_VRAM, font), (BLIT, blit)):
        off = rom_off(vram)
        if any(rom[off:off + len(blob)]):
            raise SystemExit(f"target at {vram:#x} (ROM {off:#x}) is not zero")
        rom[off:off + len(blob)] = blob
    struct.pack_into(">I", rom, rom_off(STUB), _j(BLIT))
    struct.pack_into(">I", rom, rom_off(STUB) + 4, 0x00000000)
    n64crc.fix(rom, n64crc.detect(bytes(rom)) or "6101")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rom", default=None)
    ap.add_argument("-o", "--out", default="sc64_exc.z64")
    a = ap.parse_args(argv)
    rom_path = find_rom(a.rom)
    if rom_path is None:
        sys.exit("no ROM found; pass --rom")
    rom = bytearray(load_rom(rom_path))
    apply(rom)
    Path(a.out).write_bytes(rom)
    print(f"wrote {a.out}: Error() at {STUB:#x} now draws its message to the "
          f"framebuffer; code at {FREE:#x} (shows on Ares64 / hardware)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
