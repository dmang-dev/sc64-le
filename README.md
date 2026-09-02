# sc64-le — StarCraft 64 Ladder Edition

Turn a **StarCraft 64** cartridge into a competitive melee cartridge: install PC
ladder maps into its melee slots, make them selectable in one-player and 1v1,
and brand the title screen.

This is the *write* side. Reading a cartridge — the BOLT archive, the CHK
scenario format, the MPQ wrapper, the briefings — lives in
[sc64-maps](https://github.com/dmang-dev/sc64-maps), which this depends on.

**No game data is here and none can be.** You supply your own ROM and your own
maps; `.gitignore` blocks every ROM and map extension outright.

---

## Quick start

```bash
git clone https://github.com/dmang-dev/sc64-maps      # the read side
git clone https://github.com/dmang-dev/sc64-le
cd sc64-le
pip install "bolt-lzss>=0.3.0" Pillow                 # 0.3.0 fixes the dual-byte bug

python ladder_edition.py --expand --recursive \
    --maps /path/to/StarCraft/Maps/ladder \
    -o sc64_ladder_edition.z64
python title_brand.py --rom sc64_ladder_edition.z64 \
    -o sc64_ladder_edition.z64 --text "LADDER EDITION"
```

`_deps.py` finds sc64-maps as a sibling directory, or set `SC64_MAPS`.

### Environment

| variable | what for |
|---|---|
| `SC64_MAPS` | the sc64-maps checkout, if it is not a sibling directory |
| `SC64_ROM` | a StarCraft 64 cartridge dump, for the emulator harness |
| `BIZHAWK_DIR` | the BizHawk install, if it is not found automatically |

Only `verify_ladder.py` and `harness/` need the last two; the patching tools do
not touch an emulator. Nothing here has a path baked into it.

**bolt-lzss must be 0.3.0 or later.** Earlier versions mis-encode the
dual extension byte: a stream carrying two chained value-duals round-trips
through their own decoder but decodes to a different back-reference distance
on the cartridge, overruns the map buffer, and hangs the console. Only the
largest maps reach the distances that trigger it, which is why it masqueraded
for a while as a "map too big" size limit (it is not one -- the engine boots
maps up to 256x256; see [`grow_directory.py`](grow_directory.py)'s size-guard
comment).

---

## What it does

| tool | |
|---|---|
| `ladder_edition.py` | install PC ladder maps into the melee slots and repoint the lists |
| `inject_map.py` | put one PC map into a BOLT slot, raw or compressed |
| `patch_scenario.py` | read or edit the melee Scenario list |
| `grow_directory.py` | grow BOLT directory 008 and build past the 36-map ceiling |
| `preview_offsets.py` | read or repoint the map-preview arithmetic that caps the melee range at 36 |
| `loading_screen.py` | replace the "ACCESSING MISSION DATA..." screens with your own art |
| `obstruction_notice.py` | turn the silent "too many obstructions" hang into a readable on-screen message |
| `exception_screen.py` | draw the engine's *runtime* error message on screen — any `Error()`, not one baked notice (Ares64 / hardware) |
| `title_brand.py` | stamp text onto the title screen |
| `n64crc.py` | detect and repair the N64 boot checksum |
| `pc_maps.py` | read a PC `.scm`/`.scx`, protected ones included |
| `verify_ladder.py` | play every list entry in an emulator and report what loaded |
| `harness/` | drive BizHawk from Python; decide from the framebuffer whether a map loaded |

## What works

* PC ladder maps play in the melee slots. A campaign slot applies campaign
  mission-end logic and resolves to an instant *Victory*; a melee slot does not.
* **36 maps** fit the melee range 60–95 with `--expand`, which doubles the image
  to 64 MiB. Verified on the engine: a stream 16 MiB past the original end loads
  and plays.
* Two-player **1v1** works with no ROM change — it needs an Expansion Pak and a
  second controller, which in an emulator is configuration.
* The 1v1 map selector's length is patched to match what was installed.

## What does not, yet

* **Beyond 36 maps.** Indices above 95 map to BOLT entries `008/068`+, which
  hold other data, and the selector's window is contiguous from 60. Reaching the
  full 76-map pool needs that data relocated. Space is no longer the constraint.
* **The selector's base index** is not a simple immediate; eight candidates were
  patched simultaneously and ruled out.
* **Starting a match from the two-player setup screen** in the harness — the
  confirm control has not been found. The screen itself works.
* **Removing the campaign from the menus.**

## Which maps won't load

Map *size* is not the limit — with a correct `bolt-lzss` the engine boots maps
up to 256×256. But some individual maps still refuse to load, and the cause is
always the *terrain*, never the size. Three distinct failures, found by tracing
the map load in the emulator (`harness/`, the `WATCHCOND` conditional
breakpoint, and `grow_directory.py --inject-chk` to boot modified variants):

1. **"Too many obstructions" — a designed engine limit, not a bug.**
   StarCraft 64 resolves each terrain tile against the tileset with an 8×7
   neighbourhood match. A tile it cannot resolve trips a deliberate error
   handler whose own message reads: *"The map could not be loaded because it had
   too many obstructions. Try widening corridors and reducing the number of
   small nooks and crannies to correct the problem."* On the retail console that
   message never renders — the load simply aborts and the **"ACCESSING MISSION
   DATA..." screen hangs forever**. Complex island/maze maps (many Brood War
   256×256s — Cauldron, Continental Divide, Frozen Sea, …) hit it; open maps of
   the same size do not. The game is refusing the map, and its own advice is to
   simplify the terrain — but on retail it does so silently.
   [`obstruction_notice.py`](obstruction_notice.py) makes the message visible:
   it installs a notice on a loading-screen slot a melee load never selects and
   trampolines the compiled-out assert handler to show it, so a map that trips
   the limit displays *"too many obstructions — map too complex for StarCraft
   64 — widen corridors, reduce nooks"* instead of hanging. Maps that load are
   untouched. [`exception_screen.py`](exception_screen.py) goes one further and
   draws the engine's *own* runtime string — the full "the map could not be
   loaded…" text, or any other message `Error()` is handed — by trampolining the
   stub to a routine that writes the message straight to the framebuffer
   (upper-cased, word-wrapped, glyphs the 8×8 font lacks dropped). Because it
   pokes RDRAM directly it shows on the cycle-accurate Ares64 core and on
   hardware, but not under the Mupen64Plus HLE renderer; a passing map never
   calls the handler, so it never fires.

2. **Terrain-vertex overflow — a genuine bug, only very intricate maps reach
   it.** The isometric-terrain tracer builds vertex pools counted by a *signed
   16-bit* integer and sizes each reallocation as `sext16(count)·8`. Past 32,767
   vertices that size goes negative, the allocation fails, and the load aborts.
   Only terrain with enormous edge complexity (some tournament maps — e.g.
   "BlockChain SE 2.1") generates that many boundary vertices. Removing doodads
   does not help: the vertices come from the terrain geometry itself. Fixing it
   means widening the counter everywhere the engine reads it — a deep change,
   not attempted.

3. **Unresolvable tile content — bails before allocation.** A few maps (e.g.
   "Homeworld") abort at the very start of the load, before any allocation.
   Grafting a good map's terrain onto them loads; grafting theirs onto a good
   map hangs it — so the cause is their tile data (`MTXM`), not size, units, or
   strings. The exact tile the resolver chokes on is not yet pinned.

`grow_directory.py` excludes maps over 36,864 tiles by default (a conservative
hedge, since large maps hit these hazards often) — pass `--allow-large` to
include them and boot-test, because size alone does not predict which large map
plays. None of the three is diagnosable from the CHK offline; each was found by
watching the load run.

## Reading

`docs/FORMAT.md` covers the melee Scenario list, the CHK section dispatch table,
the per-version section lists, and the boot checksum trap. It carries the dead
ends too, because several of them looked exactly like answers.

## Licence

GPL-3.0-or-later, matching sc64-maps. See [LICENSE](LICENSE).

Nothing here is licensed to you by Blizzard or Nintendo. The ladder maps are
third-party competitive maps distributed with StarCraft; they are not mine to
redistribute and are not included.
