"""Decide from the picture whether a map actually loaded.

This replaces the RAM-residency score, which was unsound. That score searched
RDRAM for raw 16-byte windows of the map's MTXM and called a hit "loaded". It
worked on native maps and was pure noise on injected ones, because native maps
are LZSS-compressed in the ROM and therefore get decompressed into a RAM buffer
where the raw bytes linger, while an injected map stored with FLAG_UNCOMPRESSED
never produces that staging copy. The metric was detecting "was decompressed",
not "was loaded", and it produced two confident false negatives on maps that
were rendering terrain perfectly well on screen.

The framebuffer does not have that problem: a map that draws terrain and fills
in the minimap has loaded, whatever route its bytes took to get there.

Thresholds here are calibrated against frames labelled by eye, not guessed --
see calibrate() and the LABELLED table at the bottom.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

# The minimap is the single most reliable tell. A loaded mission paints a
# scaled image of its terrain there; an engine that bailed leaves the panel
# empty. It survives dialog boxes, which cover the play area but not the
# corner panel, and StarCraft 64 parks on dialog boxes constantly.
MINIMAP_BOX = (0.030, 0.655, 0.245, 0.960)      # l, t, r, b as fractions

# StarCraft 64's loading screen is a dark frame with a caption band low down
# ("ACCESSING MISSION DATA..."). It has to be told apart from a map that failed
# to draw, because they are both mostly black and mean opposite things -- one
# needs more time, the other is a result. Caught this the expensive way: a map
# was scored as drawing nothing when it was still on the loading screen.
BAND_BOX = (0.0, 0.80, 1.0, 0.95)

# The resource readout (minerals / gas / supply) sits top right and is present
# in every frame of an actual game. It is the second opinion the minimap needs:
# some maps come up with a dark minimap panel -- Dire Straits, Proving Grounds
# and Acropolis all render terrain and play normally while their minimap stays
# near black -- and judging on the minimap alone called all three "no-map".
# Measured: playing 5.3-7.5% bright here, menus 0.6%, black/dark screens 0.0%.
HUD_BOX = (0.72, 0.02, 0.99, 0.13)
HUD_BRIGHT_MIN = 0.03
BRIGHT = 140         # a caption glyph is much brighter than dim terrain

DARK = 24            # max(r,g,b) below this counts as unlit
QUANT = 3            # bits dropped per channel when counting distinct colours


def _crop(img: Image.Image, box) -> Image.Image:
    w, h = img.size
    l, t, r, b = box
    return img.crop((int(l * w), int(t * h), int(r * w), int(b * h)))


def _stats(img: Image.Image) -> dict:
    raw = img.convert("RGB").tobytes()
    n = (len(raw) // 3) or 1
    lit = 0
    colours = set()
    for i in range(0, len(raw) - 2, 3):
        r, g, b = raw[i], raw[i + 1], raw[i + 2]
        if r >= DARK or g >= DARK or b >= DARK:
            lit += 1
        colours.add((r >> QUANT, g >> QUANT, b >> QUANT))
    return {"lit": lit / n, "colours": len(colours)}


def _bright_frac(img: Image.Image) -> float:
    raw = img.convert("RGB").tobytes()
    n = (len(raw) // 3) or 1
    hit = sum(1 for i in range(0, len(raw) - 2, 3)
              if raw[i] >= BRIGHT or raw[i + 1] >= BRIGHT or raw[i + 2] >= BRIGHT)
    return hit / n


def analyse(path: str | Path) -> dict:
    """Metrics for one frame. No verdict -- see classify()."""
    img = Image.open(path).convert("RGB")
    whole = _stats(img)
    mini = _stats(_crop(img, MINIMAP_BOX))
    return {
        "frame": Path(path).name,
        "size": img.size,
        "lit": whole["lit"],
        "colours": whole["colours"],
        "minimap_lit": mini["lit"],
        "minimap_colours": mini["colours"],
        "band_bright": _bright_frac(_crop(img, BAND_BOX)),
        "hud_bright": _bright_frac(_crop(img, HUD_BOX)),
    }


# Calibrated on the labelled set below. The minimap gates the decision; the
# whole-frame numbers are reported for context but deliberately do not vote,
# because a full-screen dialog box can light up a frame whose map never loaded.
#
# Measured on the labelled frames the two classes do not come close to touching:
#     loaded   minimap 25.6%..59.1% lit, 92..192 colours
#     no-map   minimap  4.0%.. 4.2% lit,   5     colours
# So the thresholds sit in the middle of that gap rather than at the edge of
# the loaded cluster -- a cutoff of 0.25 would have been one measurement away
# from misfiling the dimmest map that genuinely works.
MINIMAP_LIT_MIN = 0.12

# The minimap's COLOUR COUNT is what separates a drawn map from artwork, and it
# had to be raised from 20 after a false positive. StarCraft 64 has two loading
# screens: a dark one, and a bright full-bleed painting that lights up the
# minimap region as thoroughly as real terrain does (48.2% lit). That one was
# scored "loaded" while the engine was hung.
#
# Whole-frame brightness cannot fix it -- the bright loading screen sits at
# 44.1% lit and a genuine in-game frame under an Advisor dialog sits at 49.1%,
# so they overlap. Measured colour counts do not:
#     loading screens   53, 65
#     real maps         89, 92, 104, 114, 122, 139, 148, 177, 192
#     no map at all     5
# Rendered terrain is dithered from a large palette; artwork scaled into a tiny
# panel is not.
MINIMAP_COLOURS_MIN = 78

# Loading screen, dark variant: a bright caption band over a dark frame.
BAND_BRIGHT_MIN = 0.03
LOADING_LIT_MAX = 0.25

# Loading screen, bright variant: a mid-brightness frame whose minimap panel is
# artwork rather than terrain. Bounded above so a real map under a full-screen
# dialog (80%+ lit) is never mistaken for it.
LOADING_COLOURS_MAX = 70
LOADING_BRIGHT_RANGE = (0.25, 0.75)


def classify(path: str | Path) -> dict:
    """One of: loaded / loading / no-map.

    "loading" is not a result -- it means the frame was captured too early and
    the caller should give the engine more time before deciding anything.
    """
    m = analyse(path)
    lo, hi = LOADING_BRIGHT_RANGE

    # The loading screens are vetoed FIRST. The bright one shows HUD-region
    # activity comparable to a real game, so testing for a game before ruling
    # loading out would call it loaded.
    if m["band_bright"] >= BAND_BRIGHT_MIN and m["lit"] <= LOADING_LIT_MAX:
        m["verdict"] = "loading"                 # dark loading screen
    elif (m["minimap_colours"] <= LOADING_COLOURS_MAX
            and lo < m["lit"] < hi):
        m["verdict"] = "loading"                 # bright loading screen
    elif (m["minimap_lit"] >= MINIMAP_LIT_MIN
            and m["minimap_colours"] >= MINIMAP_COLOURS_MIN):
        m["verdict"] = "loaded"                  # minimap drawn
    elif m["hud_bright"] >= HUD_BRIGHT_MIN:
        m["verdict"] = "loaded"                  # resources on screen
    else:
        m["verdict"] = "no-map"
    return m


def calibrate(shots_dir: str | Path, labels: dict[str, str]) -> int:
    """Print metrics for labelled frames and report how the rule scores.

    Returns the number of misclassifications, so this can be asserted on.
    """
    shots = Path(shots_dir)
    rows, wrong = [], 0
    for name, truth in sorted(labels.items(), key=lambda kv: (kv[1], kv[0])):
        p = shots / name
        if not p.is_file():
            rows.append((name, truth, None, None, None, "MISSING"))
            continue
        m = classify(p)
        ok = m["verdict"] == truth
        wrong += not ok
        rows.append((name, truth, m["lit"], m["minimap_lit"],
                     m["minimap_colours"], m["verdict"] + ("" if ok else "  <-- WRONG")))

    print(f"{'frame':26} {'truth':8} {'lit':>6} {'mini':>6} {'mcol':>5}  verdict")
    print("-" * 76)
    for name, truth, lit, ml, mc, verdict in rows:
        if lit is None:
            print(f"{name:26} {truth:8} {'':>6} {'':>6} {'':>5}  {verdict}")
        else:
            print(f"{name:26} {truth:8} {lit:6.1%} {ml:6.1%} {mc:5}  {verdict}")
    print(f"\n{wrong} misclassified")
    return wrong


# Ground truth: every one of these frames was looked at directly.
# "loaded"  -- terrain drawn, minimap populated
# "loading" -- "ACCESSING MISSION DATA..." caption, engine still working
# "no-map"  -- black play area and an empty minimap panel, engine finished
LABELLED = {
    "content_control.png": "loaded",     # native mission, Raynor and units
    "shape_control.png": "loaded",       # native mission
    "swapcamp_target.png": "loaded",     # Benzene, Space Platform terrain
    "content_target.png": "loaded",      # Benzene tiles on Jade body
    "v_jade_plain_target.png": "loaded",  # Jade, Twilight terrain
    "jadeswap_target.png": "loaded",     # the STOCK map -- that ROM never
                                         # swapped slot 0x02; kept because a
                                         # loaded frame is a loaded frame
    "v_jade_era1_target.png": "loading",  # dark loading screen
    "shape_target.png": "no-map",        # black play area, Victory dialog
    "g_terrain_target.png": "no-map",    # black play area, Victory dialog
}

# The bright loading screen and the frames that exposed it. Kept in a second
# table only because they live in a different directory.
LABELLED_PLAY = {
    "entry1.png": "loaded",   # Destination
    "entry2.png": "loaded",   # Dire Straits -- dark minimap, plays fine
    "entry4.png": "loaded",   # Proving Grounds -- likewise
    "entry6.png": "loaded",   # Acropolis, 192x192 -- plays
    "entry7.png": "loaded",
}

LABELLED_MENUS = {
    "acro_99_ingame.png": "loading",     # bright loading art, engine hung
    "jcomp_99_ingame.png": "loading",
    "recomp_99_ingame.png": "loading",
    "inplace_99_ingame.png": "loading",
    "fixed_99_ingame.png": "loaded",     # re-encoded stream, plays
    "jfix_99_ingame.png": "loaded",      # compressed injection, plays
    "bdisc_99_ingame.png": "loaded",     # Benzene via the Scenario tab
}

if __name__ == "__main__":
    import sys
    root = Path(__file__).parent.parent
    wrong = calibrate(sys.argv[1] if len(sys.argv) > 1 else str(root / "shots"),
                      LABELLED)
    print()
    wrong += calibrate(str(root / "menus"), LABELLED_MENUS)
    print()
    wrong += calibrate(str(root / "ladder"), LABELLED_PLAY)
    raise SystemExit(1 if wrong else 0)
