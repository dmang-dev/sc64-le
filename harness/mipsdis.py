"""A small big-endian MIPS III disassembler, enough to read StarCraft 64.

Ghidra is set up for this project but headless runs here have been fragile,
and the question at hand needs sixty instructions in two places rather than a
whole-program analysis. This decodes the subset the game is actually built
from and prints anything else as a raw word rather than guessing.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

RAM_TO_FILE = 0x7FFFF400

REG = ["zero", "at", "v0", "v1", "a0", "a1", "a2", "a3",
       "t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7",
       "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7",
       "t8", "t9", "k0", "k1", "gp", "sp", "fp", "ra"]

SPECIAL = {
    0x00: "sll", 0x02: "srl", 0x03: "sra", 0x04: "sllv", 0x06: "srlv",
    0x07: "srav", 0x08: "jr", 0x09: "jalr", 0x0C: "syscall", 0x0D: "break",
    0x10: "mfhi", 0x11: "mthi", 0x12: "mflo", 0x13: "mtlo",
    0x18: "mult", 0x19: "multu", 0x1A: "div", 0x1B: "divu",
    0x20: "add", 0x21: "addu", 0x22: "sub", 0x23: "subu",
    0x24: "and", 0x25: "or", 0x26: "xor", 0x27: "nor",
    0x2A: "slt", 0x2B: "sltu",
}

OPS = {
    0x02: "j", 0x03: "jal", 0x04: "beq", 0x05: "bne", 0x06: "blez",
    0x07: "bgtz", 0x08: "addi", 0x09: "addiu", 0x0A: "slti", 0x0B: "sltiu",
    0x0C: "andi", 0x0D: "ori", 0x0E: "xori", 0x0F: "lui",
    0x14: "beql", 0x15: "bnel", 0x16: "blezl", 0x17: "bgtzl",
    0x20: "lb", 0x21: "lh", 0x23: "lw", 0x24: "lbu", 0x25: "lhu",
    0x28: "sb", 0x29: "sh", 0x2B: "sw",
    0x31: "lwc1", 0x39: "swc1", 0x37: "ld", 0x3F: "sd",
}

REGIMM = {0x00: "bltz", 0x01: "bgez", 0x10: "bltzal", 0x11: "bgezal"}


def _s16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def decode(word: int, pc: int) -> str:
    op = word >> 26
    rs, rt = (word >> 21) & 31, (word >> 16) & 31
    rd, sa = (word >> 11) & 31, (word >> 6) & 31
    fn = word & 63
    imm = word & 0xFFFF
    simm = _s16(imm)

    if word == 0:
        return "nop"
    if op == 0:
        m = SPECIAL.get(fn)
        if m is None:
            return f".word {word:#010x}"
        if m in ("sll", "srl", "sra"):
            return f"{m} {REG[rd]}, {REG[rt]}, {sa}"
        if m in ("jr",):
            return f"jr {REG[rs]}"
        if m == "jalr":
            return f"jalr {REG[rd]}, {REG[rs]}"
        if m in ("mfhi", "mflo"):
            return f"{m} {REG[rd]}"
        if m in ("mult", "multu", "div", "divu"):
            return f"{m} {REG[rs]}, {REG[rt]}"
        return f"{m} {REG[rd]}, {REG[rs]}, {REG[rt]}"
    if op == 1:
        m = REGIMM.get(rt, f"regimm{rt}")
        return f"{m} {REG[rs]}, {pc + 4 + simm * 4:#010x}"
    m = OPS.get(op)
    if m is None:
        return f".word {word:#010x}"
    if m in ("j", "jal"):
        return f"{m} {((pc + 4) & 0xF0000000) | ((word & 0x03FFFFFF) << 2):#010x}"
    if m in ("beq", "bne", "beql", "bnel"):
        return f"{m} {REG[rs]}, {REG[rt]}, {pc + 4 + simm * 4:#010x}"
    if m in ("blez", "bgtz", "blezl", "bgtzl"):
        return f"{m} {REG[rs]}, {pc + 4 + simm * 4:#010x}"
    if m == "lui":
        return f"lui {REG[rt]}, {imm:#06x}"
    if m in ("addi", "addiu", "slti", "sltiu"):
        return f"{m} {REG[rt]}, {REG[rs]}, {simm}" + (
            f"  ({simm:#x})" if abs(simm) > 9 else "")
    if m in ("andi", "ori", "xori"):
        return f"{m} {REG[rt]}, {REG[rs]}, {imm:#06x}"
    return f"{m} {REG[rt]}, {simm}({REG[rs]})"


def dump(rom: bytes, ram: int, before: int = 0x40, after: int = 0xC0,
         mark: set[int] | None = None) -> None:
    mark = mark or set()
    start = ram - before
    for addr in range(start, ram + after, 4):
        off = addr - RAM_TO_FILE
        if off < 0 or off + 4 > len(rom):
            continue
        w = struct.unpack_from(">I", rom, off)[0]
        flag = " <<<<" if addr in mark or addr == ram else ""
        print(f"  {addr:#010x}  {off:#08x}  {w:08x}  {decode(w, addr):40}{flag}")


if __name__ == "__main__":
    rom_path = sys.argv[1]
    rom = Path(rom_path).read_bytes()
    for spec in sys.argv[2:]:
        parts = spec.split(":")
        ram = int(parts[0], 16)
        before = int(parts[1], 16) if len(parts) > 1 else 0x40
        after = int(parts[2], 16) if len(parts) > 2 else 0xC0
        print(f"===== {ram:#010x} (file {ram - RAM_TO_FILE:#08x}) =====")
        dump(rom, ram, before, after)
        print()
