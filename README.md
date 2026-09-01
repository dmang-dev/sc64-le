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

## Reading

`docs/FORMAT.md` covers the melee Scenario list, the CHK section dispatch table,
the per-version section lists, and the boot checksum trap. It carries the dead
ends too, because several of them looked exactly like answers.

## Licence

GPL-3.0-or-later, matching sc64-maps. See [LICENSE](LICENSE).

Nothing here is licensed to you by Blizzard or Nintendo. The ladder maps are
third-party competitive maps distributed with StarCraft; they are not mine to
redistribute and are not included.
