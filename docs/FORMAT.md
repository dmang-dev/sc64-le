# Format notes: patching StarCraft 64

These are the notes for CHANGING a cartridge. The read side -- the BOLT
archive, the CHK scenario format, the MPQ wrapper, the briefing scripts -- is
documented in sc64-maps/docs/FORMAT.md, and this assumes it.

Sections are numbered as they were when both halves lived in one file, so
external references to "FORMAT.md section 9" still resolve.

## 9. The melee Scenario list

StarCraft 64 has a melee mode, and it matters more than it sounds. A PC ladder
map injected into a **campaign** BOLT slot loads and renders perfectly well, but
resolves to an instant `Victory` — the slot applies campaign mission-end logic
to a map carrying no campaign triggers, so the end condition is met on frame
one. The same map in a **melee** slot plays normally: resources, supply counter,
no premature win.

### 9.1 Reaching it

From the title screen, `Start` twice reaches the main menu. That menu shows a
race on the left and an episode on the right, and **D-pad LEFT** cycles them
together:

| presses | race | episode | logo |
|---|---|---|---|
| 0 | Terran | I | StarCraft |
| 1 | Zerg | II | StarCraft |
| 2 | Protoss | III | StarCraft |
| 3 | Protoss | IV | BroodWar |
| 4 | Terran | V | BroodWar |
| 5 | Zerg | VI | BroodWar |

then wraps. So there is **no separate Brood War mode** — Brood War is episodes
IV–VI of one selector, and both campaigns run the same map loader. The selector
byte is at RAM `0x800DD937`, holding `episode mod 6`. (The ROM also carries
"Expansion Pak required for Broodwar Missions" at `0x0D182C`, so the expansion
campaign is gated on the 4 MB Expansion Pak.)

`A` from the main menu opens mission select, whose header reads
`[Episode N] [Scenario] [Load Saved]`. **D-pad RIGHT** moves to `Scenario`, and
that list is the melee mode.

### 9.2 The table

Three structures sit together in the static segment:

| what | file offset | RAM | layout |
|---|---|---|---|
| label strings | `0x0D15F4` | `0x800D09F4` | NUL-terminated, entries pre-padded with two spaces |
| pointer array | `0x0D16BC` | — | 11 × big-endian pointer |
| records | `0x0D16E8` | — | 10 × `{u8 map_id, u8 opponents}` |

`map_id + 60` is the map index, and `map_id + 68` the BOLT file number in
directory `008`. 60 is Challenger, the first melee map.

| # | record | map_id | opp | index | BOLT | label | scenario name |
|---|---|---|---|---|---|---|---|
| 1 | `0b 01` | 11 | 1 | 71 | `008/04F` | `1v1 Blood Bath` | Blood Bath |
| 2 | `00 01` | 0 | 1 | 60 | `008/044` | `1v1 Challenger` | Challenger |
| 3 | `03 01` | 3 | 1 | 63 | `008/047` | `1v1 Discovery` | Discovery |
| 4 | `08 02` | 8 | 2 | 68 | `008/04C` | `1v2 Triumvirate` | Triumvirate |
| 5 | `0b 02` | 11 | 2 | 71 | `008/04F` | `1v2 Blood Bath` | Blood Bath |
| 6 | `18 02` | 24 | 2 | 84 | `008/05C` | `1v2 Hunters` | The Hunters |
| 7 | `11 03` | 17 | 3 | 77 | `008/055` | `1v3 Power Lines` | Power Lines |
| 8 | `0e 03` | 14 | 3 | 74 | `008/052` | `1v3 Brushfire` | Brushfire |
| 9 | `18 04` | 24 | 4 | 84 | `008/05C` | `1v4 Hunters` | The Hunters |
| 10 | `5f 01` | 95 | 1 | — | — | ` *Mass Hysteria*` | (see 9.5) |

Three things establish the decode rather than merely fitting it. The
`opponents` column reads `1,1,1,2,2,2,3,3,4`, matching every `1vN` label.
`map_id` repeats exactly where the list repeats — `0x0B` twice for Blood Bath,
`0x18` twice for Hunters. And nine of ten resolve to the correct scenario name
read straight out of the referenced CHK. Reading the byte as a *raw* map index
instead yields campaign missions ("T12) The Hammer Falls" for entry 1), so the
`+60` base is not optional.

This also confirms the community static-address rule on live data:
`file = RAM − 0x80000000 + 0xC00` maps `0x800D09F4` to `0x0D15F4` exactly.

### 9.3 Patching here needs a checksum repair

`0x0D16E8` is 857,832, which is **inside** the CIC boot checksum window
`0x1000`–`0x101000`. Patch it and leave the header alone and IPL3 refuses to
hand off: the ROM boots to a black screen, RAM never gets a map index, and the
failure looks exactly like "the patch had no effect".

BOLT starts at `0x12CA10`, past the window, which is why swapping map data
never needed this and why the trap is easy to walk into.

The two checksum words are at header `0x10` and `0x14`, big endian. Rather than
assume CIC-6102 because it is the common case, compute with every seed and keep
whichever reproduces the ROM's own stored value; for the USA cart that is the
6101/6102 seed `0xF8CA4DDC`, reproducing `0x0684FBFB 0x5D3EA8A5`.

### 9.4 What can be changed

* **10 list entries**, each repointed by writing two bytes.
* **36 reachable slots.** `map_id` is a `u8`, so indices `60`–`315` are
  expressible but only `60`–`95` exist: the melee maps plus the bonus maps
  (Orbital Death, Eruption, Pro Bowl, Round-Up, King of the Hill, Old Faithful,
  Guardians, Zerg Troopers, Resurrection IV, Rage, Mass Hysteria).
* **Campaign slots are unreachable** from this list — the `+60` base floors it.
* The **opponent count** is per entry, independent of the map.

Verified on hardware semantics in an emulator: writing `map_id = 0x0E` to
record 2 made that entry load index 74 (Brushfire). Injecting a PC ladder map
into `008/05D` (index 85, a slot no list entry uses) and pointing record 2 at
`map_id = 0x19` with 3 opponents loaded it as a 1v3 melee game — so injection
and repointing compose, and a ladder map can be added without displacing any
map the stock list shows.

A melee slot does **not** constrain the injected map's dimensions: a 128×112
map runs in Blood Bath's 64×64 slot. The map's own `DIM` governs.

### 9.5 The eleventh entry is dead data

The pointer array has 11 entries; the last is `" *Mass Hysteria*"`, and it never
appears in the rendered list. Its record is `5f 01`, and `0x5F` is 95 — exactly
Mass Hysteria's own map index (`008/067`), which is too precise to be
coincidence. But under the `+60` rule that record means index 155, and the
cartridge holds 96 maps.

Patching that record to a valid `map_id` does **not** make an eleventh item
appear: the list still renders exactly ten, and a cursor driven ten steps down
wraps to `Setup Custom`. So the list length is fixed in the menu code rather
than derived from the pointer array, the record was evidently written in a
different convention, and nothing ever exercised it. Enabling the entry means
finding that length constant — it is not in the bytes adjacent to the table,
which are a separate ascending run (`07 0c 11 14 16 19 1d 20 28 31 3b`).

## 10. How the engine parses a CHK

The console does not walk a scenario the way a PC tool does. It has a **section
dispatch table**, and it consults a different list of sections depending on the
map's `VER`.

### 10.1 The table

At RAM `0x80001644` (file `0x002244`) sits an array of 12-byte records:

```
struct { char tag[4]; void (*handler)(void *data, u32 size); u32 flag; }
```

Immediately before it, at `0x8000165C`, is a header with a 40-byte stride, one
entry per supported map version:

```
struct { u32 version; struct { record *list; u32 count; } phase[4]; u32 zero; }
```

Three versions are supported, and the section lists differ between them:

| `VER` | format | phase 1 (9) | phase 2 (2) | phase 3 | phase 4 |
|---|---|---|---|---|---|
| 59 | StarCraft `.scm` | VER DIM ERA OWNR SIDE STR SPRP FORC VCOD | STR MBRF | STR MTXM THG2 UNIT | 19 sections |
| 63 | StarCraft 1.04+ `.scm` | as above | as above | as above | 19 sections |
| 205 | Brood War `.scx` | as above | as above | + COLR (5) | 15 sections |

The phases are load stages — header, briefing, preview, full load — which is
why `STR` reappears at the head of each: every stage that resolves text has to
have the string table in hand first.

Two things fall out of this that matter for injection. The engine reads
**`MTXM` for terrain and never reads `ISOM` or `TILE`**, which is exactly why PC
ladder maps work at all: they carry no `ISOM`. And the version-205 list drops
`UNIS`/`UPGS`/`TECS`/`UPGR`/`PTEC` in favour of the `x` variants, so a Brood
War map is not merely tolerated, it is separately provided for.

### 10.2 The MTXM handler, and why duplicate sections break it

`MTXM`'s handler is at `0x8002DADC`. Decompiled, it is:

```c
if (size <= (u32)DIM_width * (u32)DIM_height * 2) {
    if (copy_section(data, tile_buffer)) {
        for (n = size >> 1; n--; p++)      /* byte-swap each u16 in place */
            *p = CONCAT11(*((u8 *)p + 1), *(u8 *)p);
        rebuild_terrain(); ...
        return 1;
    }
}
return 0;                                  /* no terrain, load continues */
```

So it range-checks the section against `DIM`, copies it to **one fixed tile
buffer**, byte-swaps every entry in place (PC tile ids are little endian, the
N64 is big endian), and re-runs the terrain rebuild. On failure it returns 0
and the rest of the map loads anyway — which is why a bad `MTXM` produces a
*playable* game with resources, units and a minimap, and no tiles at all.

The trap is that the handler runs **once per `MTXM` section**. A CHK may carry
the same tag repeatedly, and StarCraft applies them in order, each overwriting
from the start of the array — map protectors rely on it. The console instead
re-copies, re-swaps and rebuilds over a buffer that has already been converted.

Measured over the 2017 Frontier League set: every map with one `MTXM` renders,
every map with three does not.

| map | `MTXM` sections | terrain |
|---|---|---|
| Destination, Match Point, Circuit Breakers, Jade | 1 | draws |
| Longinus 2, Tau Cross, Fighting Spirit | 3 | absent |

### 10.3 Normalising a PC map before injection

`ladder_edition.py` applies three passes, and only the second is load-bearing:

1. **Strip unknown-tag sections.** Protectors interleave junk sections with
   random four-byte tags; StarCraft ignores them. Correlates perfectly with
   failure and, tested alone, fixes nothing — it is kept because it is
   harmless and shrinks the payload, not because it was shown to matter.
2. **Collapse duplicate sections** the way StarCraft resolves them: one section
   per tag, later occurrences overlaid from offset 0. This is the fix.
3. **Compress with the BOLT LZSS encoder.** Scenarios store at roughly a fifth,
   so the cartridge's ~313 KiB of tail padding stops being the constraint.
   Requires bolt-lzss 0.2.0 or later; earlier versions emit streams this
   engine's decoder rejects.

### 10.4 The two-player map selector is bounded separately

Two-player melee has its own map list, and it is not the Scenario list. It
walks a contiguous run of map indices starting at 60, and its length is an
immediate in the menu setup code:

```
RAM 0x800D9F78   file 0x0DAB78    addiu a2, zero, 27      -> indices 60..86
```

27 is the cartridge's own melee map count. A map installed at index 87 or
beyond therefore exists, loads fine through the Scenario list, and is simply
unreachable in a 1v1 game — the selector wraps from 86 straight back to 60.

Widening the immediate extends the range; at 32 the selector covers 60..91.
Confirmed by patching and watching the list grow rather than by reading alone,
because 27 is a small number that occurs all over the image — a scan for it
finds 482 sites, nearly all stack displacements.

That address is inside the boot checksum window, so the header needs repairing
(see 9.3).

Two-player itself needs no ROM change. It refuses to start without an
Expansion Pak **and** a second controller, and says so on screen; in an
emulator both are configuration. Note that the RDRAM domain size reports
8,388,608 whether or not the Pak is enabled — that is the addressable size, not
what is installed, so it cannot be used to confirm the Pak is present.
