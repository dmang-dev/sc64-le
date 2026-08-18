"""Play every entry of a patched Scenario list and report what actually loads.

One boot, a savestate taken on the scenario list, and each entry driven from
that same state -- seven boots would cost seven title sequences for no extra
information, and starting every run from an identical state is also what makes
the frames comparable.

Each row is self-checking. The map index the game reports after selecting an
entry must equal the index that entry's record was patched to; if it does not,
the cursor landed somewhere other than intended and the row is void rather than
a result. The verdict itself comes from the framebuffer, with loading screens
waited out rather than scored.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parent / "harness"
sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(HARNESS.parent))

import _deps  # noqa: F401  (puts sc64-maps on sys.path)
from extract_sc64_maps import chk_sections, load_rom, BoltArchive, parse_map
from framecheck import classify
from sc64emu import MAP_INDEX_ADDR, Emu

SCRATCH = HARNESS.parent
SHOTS = SCRATCH / "ladder"
RECORDS = 0x0D16E8
MELEE_BASE = 60

ap = argparse.ArgumentParser()
ap.add_argument("--rom", default=str(SCRATCH / "sc64_ladder_edition.z64"))
ap.add_argument("--entries", type=int, default=7)
ap.add_argument("--settle", type=int, default=1800)
ap.add_argument("--title-frames", type=int, default=900)
a = ap.parse_args()

SHOTS.mkdir(exist_ok=True)
rom_path = Path(a.rom).resolve()
if not rom_path.is_file():
    sys.exit(f"no such ROM: {rom_path}")

rom = load_rom(str(rom_path))
arc = BoltArchive(rom)

# What each entry claims to point at, read from the patched table itself.
expect = []
for n in range(1, a.entries + 1):
    mid, opp = rom[RECORDS + (n - 1) * 2], rom[RECORDS + (n - 1) * 2 + 1]
    idx = mid + MELEE_BASE
    name = "?"
    try:
        chk = arc.read(next(e for e in arc.entries()
                            if e.path == f"008/{idx + 8:03X}"))
        name = parse_map("x", chk).name
    except Exception:
        pass
    expect.append((n, idx, opp, name))

out = SCRATCH / "runs" / "ladder_verify"
out.mkdir(parents=True, exist_ok=True)

print(f"{'#':>2} {'want':>5} {'got':>5} {'opp':>3}  {'verdict':9} "
      f"{'core':8} {'minimap':>8}  map")
print("-" * 88)

ok = 0
with Emu(session_dir=out, core="Mupen64Plus") as e:
    e.boot(str(rom_path))
    e.run_frames(a.title_frames)
    e.press(["Start"], 6); e.run_frames(120)
    e.press(["Start"], 6); e.run_frames(120)
    e.press(["A"], 6); e.run_frames(150)
    e.press(["DPad R"], 6); e.run_frames(90)
    state = out / "scenlist.State"
    e.savestate(state)
    e.screenshot(SHOTS / "list.png")

    for n, want, opp, name in expect:
        e.loadstate(state)
        e.run_frames(2)
        for _ in range(n):
            e.press(["DPad D"], 6); e.run_frames(40)
        e.press(["A"], 6); e.run_frames(180)
        got = e.read_u16(MAP_INDEX_ADDR)
        e.press(["Start"], 6); e.run_frames(a.settle)

        png = SHOTS / f"entry{n}.png"
        for _ in range(5):
            e.screenshot(png)
            m = classify(png)
            if m["verdict"] != "loading":
                break
            e.run_frames(900)
        core = e.detect_crash()["verdict"]

        if got != want:
            verdict = "VOID"
        elif core != "ok":
            verdict = "core-" + core
        else:
            verdict = m["verdict"]
        ok += verdict == "loaded"
        print(f"{n:>2} {want:>5} {got:>5} 1v{opp}  {verdict:9} {core:8} "
              f"{m['minimap_lit']:7.1%}  {name[:28]}")

print(f"\n{ok}/{len(expect)} entries load and play")
print(f"frames in {SHOTS}")
