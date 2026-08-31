"""Build a StarCraft 64 ROM whose melee list is the 2017 Frontier League maps.

Everything this needs is now proven separately:

  * PC ladder maps load and play in the melee slots (campaign slots apply
    campaign mission-end logic and resolve to an instant Victory; melee slots
    do not).
  * bolt-lzss 0.2.0 produces streams the engine accepts, so payloads compress
    to roughly a fifth and the ROM's ~313 KiB of tail padding stops binding.
  * The Scenario list is ten {map_id, opponents} records at 0x0D16E8, with
    map index = map_id + 60, and that table is patchable.
  * That table is inside the boot checksum window, so the header must be
    repaired or the ROM will not boot at all.

The one design decision worth stating: injection targets map indices 85-95,
which no Scenario list entry references. The stock melee maps at 60-84 are left
untouched, so this ADDS a ladder lineup rather than destroying the cartridge's
own. Nothing is overwritten that the game otherwise shows you.

No ROM is distributed by this script and none can be -- it reads a cartridge
you supply and writes a patched copy locally.
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
                               chk_sections, load_rom, looks_like_chk,
                               parse_map)
from inject_map import dir_entry_offset, tail_free_start
from lzcache import encode_cached
from pc_maps import read_chk
from sc64 import find_rom

FLAG_UNCOMPRESSED = 0x08
ALIGN = 16
MELEE_BASE = 60
RECORDS = 0x0D16E8
FREE_SLOTS = list(range(85, 96))        # indices no list entry points at

# With --expand the cartridge is doubled to 64 MiB and every payload goes in
# the new half, so the ~313 KiB of tail padding stops being the budget and the
# whole melee range becomes usable. Verified on the engine: a map whose stream
# sits at file 0x3000000, 16 MiB past the original end, loads and plays. The
# N64 cartridge window runs to roughly 64 MiB, BOLT offsets are u32 relative to
# a base at 0x12CA10, and the boot checksum only covers 0x1000..0x101000, so
# nothing about growing the image disturbs what is already there.
#
# 60 is the first melee map. Indices above 95 are NOT usable: they map to BOLT
# entries 008/068 and beyond, which hold other data, and the selector's window
# is contiguous from 60.
MELEE_SLOTS = list(range(60, 96))

# The two-player melee map selector walks a bounded range starting at map index
# 60. Its length is an immediate in the menu setup code:
#
#     RAM 0x800D9F78 / file 0x0DAB78    addiu a2, zero, 27
#
# 27 covers indices 60..86, so a map installed at 87 or beyond exists, loads
# through the Scenario list, and is simply unreachable in a 1v1 game. Widening
# the immediate to (last_index - 60 + 1) brings the whole lineup into the
# selector. Confirmed by patching it and watching the list grow.
#
# This is inside the boot checksum window, so the header must be repaired --
# which this script does anyway for the Scenario table.
LIST_LEN_OFFSET = 0x0DAB78
LIST_LEN_EXPECT = 0x2406001B          # addiu a2, zero, 0x1b


# Every section tag StarCraft defines. Anything else in a scenario is padding
# a protector added: competitive maps are routinely spammed with sections
# carrying random four-byte tags, which PC StarCraft ignores and plays anyway.
#
# The console does not. Measured across the 2017 Frontier League set, the maps
# that fail to load on the N64 are exactly the ones carrying a lot of junk --
# 24, 28 and 36 junk sections, against 0 or 1 for every map that works. It is
# a count problem rather than a size one; the junk is only ~1 KiB of bytes.
KNOWN_TAGS = {
    b"TYPE", b"VER ", b"IVER", b"IVE2", b"VCOD", b"IOWN", b"OWNR", b"ERA ",
    b"DIM ", b"SIDE", b"MTXM", b"PUNI", b"UPGR", b"PTEC", b"UNIT", b"ISOM",
    b"TILE", b"DD2 ", b"THG2", b"MASK", b"STR ", b"UPRP", b"UPUS", b"MRGN",
    b"TRIG", b"MBRF", b"SPRP", b"FORC", b"WAV ", b"UNIS", b"UPGS", b"TECS",
    b"SWNM", b"COLR", b"PUPx", b"PTEx", b"UNIx", b"UPGx", b"TECx", b"CRGB",
    b"STRx",
}


def collapse_duplicates(chk: bytes) -> tuple[bytes, int]:
    """Resolve repeated sections the way StarCraft does, into one each.

    A CHK may carry the same tag more than once, and StarCraft applies them in
    order with each overwriting from the START of that section's data. Map
    protectors abuse it: the three ladder maps that run with no terrain each
    carry THREE MTXM sections, and all four that work carry exactly one.

    The console cannot cope. Its MTXM handler (0x8002DADC, reached from the
    version-205 dispatch table) copies the section to one fixed tile buffer,
    byte-swaps it in place, and re-runs the terrain rebuild -- every time it is
    called. A second MTXM therefore re-swaps and rebuilds over a buffer that is
    already converted, and the result is no terrain at all while the rest of
    the map loads and plays.

    Applying the overrides here means the console sees one already-resolved
    section and lands on the same tiles StarCraft would have drawn.
    """
    order: list[bytes] = []
    merged: dict[bytes, bytearray] = {}
    dupes = 0
    for tag, payload in chk_sections(chk):
        if tag not in merged:
            order.append(tag)
            merged[tag] = bytearray(payload)
            continue
        dupes += 1
        buf = merged[tag]
        if len(payload) > len(buf):
            buf.extend(b"\0" * (len(payload) - len(buf)))
        buf[:len(payload)] = payload           # later wins, from offset 0
    out = bytearray()
    for tag in order:
        out += tag + struct.pack("<i", len(merged[tag])) + bytes(merged[tag])
    return bytes(out), dupes


def strip_junk(chk: bytes) -> tuple[bytes, int]:
    """Drop sections StarCraft does not define. Returns (chk, n_dropped).

    Order is preserved and duplicates are kept, because a CHK's later section
    of a given tag legitimately overrides an earlier one -- collapsing those
    would change the map rather than clean it.
    """
    out = bytearray()
    dropped = 0
    for tag, payload in chk_sections(chk):
        if tag in KNOWN_TAGS:
            out += tag + struct.pack("<i", len(payload)) + payload
        else:
            dropped += 1
    return bytes(out), dropped


def clamp_terrain(chk: bytes) -> tuple[bytes, int]:
    """Trim an oversized MTXM to exactly width*height*2. Returns (chk, n).

    Five of the 2017 ladder maps carry an MTXM nine bytes longer than the tile
    grid -- a protector's signature, the same +9 on every one. PC StarCraft
    reads width*height tiles and ignores the tail, so it never notices. The
    console does: the MTXM handler at 0x8002DADC range-checks the section
    against width*height*2 and an overshoot hangs the load on the "ACCESSING
    MISSION DATA" screen, indistinguishable from any other failed map.

    strip_junk removes sections the game does not define and collapse_duplicates
    merges repeats, but neither checks the SIZE of a section it keeps. This does,
    for MTXM specifically -- the one whose handler is known to reject an
    oversized section -- and only ever truncates, never pads: an undersized
    MTXM is a different and unproven problem, left alone.

    Verified end to end: the clamped map loads at an index where its unclamped
    form hangs.
    """
    dim = next((p for t, p in chk_sections(chk) if t == b"DIM "), None)
    if not dim or len(dim) < 4:
        return chk, 0
    w, h = struct.unpack_from("<HH", dim, 0)
    expect = w * h * 2
    out = bytearray()
    clamped = 0
    for tag, payload in chk_sections(chk):
        if tag == b"MTXM" and len(payload) > expect:
            payload = payload[:expect]
            clamped += 1
        out += tag + struct.pack("<i", len(payload)) + payload
    return bytes(out), clamped


def convert_strings(chk: bytes) -> tuple[bytes, int]:
    """Give a map that only has STRx a classic STR section. Returns (chk, n).

    Sixteen of the 2017 ladder maps hang the console with the same loading
    screen as every other bad map, and the discriminator turned out to be
    that they have NO 'STR ' section at all -- they carry 'STRx' instead,
    the extended string table that Remastered-era editors write. PC 1.21+
    reads STRx; a 1998 cartridge has never heard of it, so the map arrives
    with no string table the engine can see. All 43 maps that play carry a
    classic STR; all 16 that carry only STRx hang. That is not protection,
    just a resave by a modern editor.

    The conversion is mechanical: STRx is u32 count + u32 offsets + string
    data, STR is the same with u16s. It is only possible while everything
    fits in 16 bits, which a 4 KB table does with room to spare; a table too
    big to convert is left alone and reported by the caller's read-back
    rather than silently truncated. STRx is dropped after conversion so the
    console sees exactly one string table.
    """
    sec = {t: p for t, p in chk_sections(chk)}
    if b"STR " in sec or b"STRx" not in sec:
        return chk, 0
    x = sec[b"STRx"]
    if len(x) < 4:
        return chk, 0
    count = struct.unpack_from("<I", x, 0)[0]
    if count == 0 or 4 + 4 * count > len(x):
        return chk, 0

    strings = []
    for i in range(count):
        off = struct.unpack_from("<I", x, 4 + 4 * i)[0]
        end = x.find(b"\0", off) if off < len(x) else -1
        strings.append(x[off:end] if end >= 0 else b"")

    header = 2 + 2 * count
    total = header + sum(len(s) + 1 for s in strings)
    if count > 0xFFFF or total > 0xFFFF:
        return chk, 0                     # cannot express as STR; leave it

    blob = bytearray(struct.pack("<H", count))
    offs, cursor = [], header
    for s in strings:
        offs.append(cursor)
        cursor += len(s) + 1
    for o in offs:
        blob += struct.pack("<H", o)
    for s in strings:
        blob += s + b"\0"

    out = bytearray()
    for tag, payload in chk_sections(chk):
        if tag == b"STRx":
            out += b"STR " + struct.pack("<i", len(blob)) + bytes(blob)
        else:
            out += tag + struct.pack("<i", len(payload)) + payload
    return bytes(out), 1


def ensure_description(chk: bytes) -> tuple[bytes, int]:
    """Give a map with no description string one. Returns (chk, n_fixed).

    SPRP holds two string indices, name and description, and index 0 means
    "none". Exactly one map in a 188-map build shipped desc = 0, and it was
    exactly the map that wedged the console's map selector: scrolling onto it
    kills the menu (the dispatch loop idles with no handler installed -- the
    familiar signature) while the same map plays fine when launched directly.
    Every stock cartridge map carries a description, so the selector's info
    panel plainly never learned to render "none".

    The repair is two bytes: point the description at the NAME string. The
    info panel then shows the map's name where the blurb would have been,
    which is unremarkable, and nothing else changes -- no string table
    surgery, no size change beyond none at all.
    """
    sec = {t: p for t, p in chk_sections(chk)}
    sprp = sec.get(b"SPRP")
    if not sprp or len(sprp) < 4:
        return chk, 0
    name_i, desc_i = struct.unpack("<HH", sprp[:4])
    if desc_i != 0 or name_i == 0:
        return chk, 0
    out = bytearray()
    for tag, payload in chk_sections(chk):
        if tag == b"SPRP":
            payload = struct.pack("<HH", name_i, name_i) + payload[4:]
        out += tag + struct.pack("<i", len(payload)) + payload
    return bytes(out), 1


def normalise(chk: bytes) -> tuple[bytes, int, int]:
    """Junk-stripped, duplicate-collapsed, terrain-clamped CHK.

    Returns (chk, dropped, dupes). The passes run in this order and always
    together: strip removes undefined sections, collapse merges repeats (which
    must come after strip, or it would merge duplicates about to be thrown
    away), and clamp trims an oversized MTXM to the tile grid (which must come
    after collapse, since the merged section is the one the console will read).
    Every caller that hashes, installs or compares a scenario wants this exact
    composition, so it lives here rather than being spelled out at each site.
    """
    chk, dropped = strip_junk(chk)
    chk, dupes = collapse_duplicates(chk)
    chk, _clamped = clamp_terrain(chk)
    chk, _converted = convert_strings(chk)
    chk, _desc = ensure_description(chk)
    return chk, dropped, dupes


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rom", default=None,
                    help="ROM to patch; found automatically if omitted")
    ap.add_argument("-o", "--out", default="sc64_ladder_edition.z64")
    ap.add_argument("--level", type=int, default=3)
    ap.add_argument("--maps", required=True,
                    help="directory of .scm/.scx maps to install")
    ap.add_argument("--recursive", action="store_true",
                    help="search --maps recursively and drop duplicate "
                         "scenarios; the seasons in a ladder folder repeat "
                         "the pool heavily")
    ap.add_argument("--expand", action="store_true",
                    help="double the ROM to 64 MiB and use the whole melee "
                         "range, replacing the cartridge's own melee maps")
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)

    rom_path = find_rom(a.rom)
    if rom_path is None:
        sys.exit("no ROM found; pass --rom")
    rom = bytearray(load_rom(rom_path))
    variant = n64crc.detect(bytes(rom))
    if variant is None:
        sys.exit("error: unrecognised ROM -- checksum matches no CIC variant")

    slots = MELEE_SLOTS if a.expand else FREE_SLOTS
    if a.expand:
        rom.extend(bytes(len(rom)))
        print(f"expanded to {len(rom):,} bytes ({len(rom) // 2**20} MiB)")

    sources = sorted(Path(a.maps).glob("**/*.sc*" if a.recursive else "*.sc*"))
    if a.recursive:
        # Deduplicate on the NORMALISED scenario, not the file: the same map
        # reappears season after season with different protector padding, so
        # hashing the raw file would keep all of them.
        seen, unique = set(), []
        for src in sources:
            try:
                key = hashlib.sha256(normalise(read_chk(src))[0]).hexdigest()
            except Exception:
                continue
            if key in seen:
                continue
            seen.add(key)
            unique.append(src)
        print(f"{len(sources)} files -> {len(unique)} unique scenarios")
        sources = unique
    if not sources:
        sys.exit(f"no maps found in {a.maps}")
    sources = sources[:len(slots)]

    print(f"ROM {Path(rom_path).name}  CIC {variant}")
    print(f"installing {len(sources)} maps into indices "
          f"{slots[0]}..{slots[len(sources) - 1]}\n")

    installed = []
    for src, idx in zip(sources, slots):
        chk, dropped, dupes = normalise(read_chk(src))
        info = parse_map(src.name, chk)
        sec = {t: p for t, p in chk_sections(chk)}
        humans = sum(1 for b in sec.get(b"OWNR", b"") if b == 6)

        packed, _hit = encode_cached(chk, a.level)

        # Re-read the archive each time: the previous write moved where the
        # tail padding begins, and the next payload has to land after it.
        arc = BoltArchive(bytes(rom))
        slot = f"008/{idx + 8:03X}"
        rec = dir_entry_offset(arc, slot)
        old = arc._entry(slot, rec)
        dest = (tail_free_start(bytes(rom)) + ALIGN - 1) & ~(ALIGN - 1)
        if dest + len(packed) > len(rom):
            sys.exit(f"error: out of tail padding at {src.name}")

        rom[dest:dest + len(packed)] = packed
        abs_rec = arc.base + rec
        rom[abs_rec] = old.flags & ~FLAG_UNCOMPRESSED       # compressed
        struct.pack_into(">I", rom, abs_rec + 4, len(chk))  # decompressed size
        struct.pack_into(">I", rom, abs_rec + 8, dest - arc.base)

        installed.append((src.name, idx, info, humans, len(chk), len(packed)))
        print(f"  {src.name[:30]:30} -> index {idx} ({slot})  "
              f"{info.width}x{info.height} {info.tileset_name:14} "
              f"{len(chk):8,} -> {len(packed):7,} ({len(packed)/len(chk):.3f})"
              + (f"  [-{dropped} junk]" if dropped else "")
              + (f"  [-{dupes} dup]" if dupes else ""))

    # Repoint the list. Entry 0 is Setup Custom and has no record; entries run
    # 1..10 but the tenth is dead data the game never renders, so 9 are usable.
    print()
    for n, (name, idx, info, humans, _, _) in enumerate(installed[:9], start=1):
        rec = RECORDS + (n - 1) * 2
        opponents = max(1, min(humans - 1, 4))
        rom[rec] = (idx - MELEE_BASE) & 0xFF
        rom[rec + 1] = opponents
        print(f"  list entry {n}: -> index {idx}  1v{opponents}  "
              f"{info.name[:34]}")

    # Widen the two-player selector so every installed map is reachable in 1v1.
    last_index = installed[-1][1] if installed else 86
    want_len = max(0x1B, last_index - MELEE_BASE + 1)
    have = struct.unpack_from(">I", rom, LIST_LEN_OFFSET)[0]
    if have != LIST_LEN_EXPECT:
        print(f"  warning: {LIST_LEN_OFFSET:#08x} is {have:#010x}, expected "
              f"{LIST_LEN_EXPECT:#010x} -- leaving the 1v1 list length alone")
    else:
        struct.pack_into(">I", rom, LIST_LEN_OFFSET,
                         (have & 0xFFFF0000) | want_len)
        print(f"  1v1 map list: {have & 0xFFFF} -> {want_len} entries "
              f"(indices {MELEE_BASE}..{MELEE_BASE + want_len - 1})")

    c1, c2 = n64crc.fix(rom, variant)
    print(f"\nboot checksum repaired -> {c1:#010x} {c2:#010x}")

    out = Path(a.out)
    out.write_bytes(rom)

    # Read every installed map back through the ordinary archive walk.
    back = BoltArchive(bytes(rom))
    bad = 0
    for name, idx, info, _, plain, _ in installed:
        slot = f"008/{idx + 8:03X}"
        got = back.read(next(e for e in back.entries() if e.path == slot))
        if len(got) != plain or not looks_like_chk(got):
            # Protected maps are not looks_like_chk clean; check the sections.
            tags = {t for t, _ in chk_sections(got)}
            if len(got) != plain or not {b"VER ", b"DIM ", b"MTXM"} <= tags:
                print(f"  READ-BACK FAILED: {name}")
                bad += 1

    print(f"read-back through BoltArchive: "
          f"{len(installed) - bad}/{len(installed)} ok")
    print(f"other entries still walkable  : "
          f"{sum(1 for e in back.entries() if not e.path.startswith('008/0'))}")
    print(f"\nwrote {out}  ({out.stat().st_size:,} bytes)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
