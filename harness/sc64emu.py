"""Drive StarCraft 64 in BizHawk from Python.

Usage
-----
    from sc64emu import Emu, ROM_USA
    with Emu() as e:
        e.boot(ROM_USA)
        e.run_frames(600)
        e.screenshot("title.png")
        print(hex(e.read_u16(0x800D13F8)))

Everything goes through a Lua script running inside EmuHawk; see
harness.lua.tmpl for the wire protocol.
"""

from __future__ import annotations

import os
import shutil
import json
import subprocess
import time
import hashlib
from pathlib import Path

def _find_bizhawk():
    """Best guess at a BizHawk install, or None. BIZHAWK_DIR overrides."""
    here = Path(__file__).resolve()
    roots = [here.parent.parent, here.parent.parent.parent, Path.home()]
    guesses = [r / "BizHawk" for r in roots]
    for root in roots:
        try:
            guesses.extend(sorted(root.glob("BizHawk-*")))
        except OSError:
            pass
    for g in guesses:
        if (g / "EmuHawk.exe").is_file():
            return g
    return None


# Where BizHawk lives, and which ROM to boot. Both come from the environment so
# this file carries nobody's directory layout:
#
#   BIZHAWK_DIR   the BizHawk install (the folder holding EmuHawk.exe)
#   SC64_ROM      a StarCraft 64 cartridge dump
#
# Neither is required at import. A caller that only wants framecheck, or that
# passes an explicit ROM to boot(), should not need either set; boot() reports
# what is missing at the point it actually matters.
_env_bizhawk = os.environ.get("BIZHAWK_DIR")
BIZHAWK = Path(_env_bizhawk) if _env_bizhawk else _find_bizhawk()
EMUHAWK = (BIZHAWK / "EmuHawk.exe") if BIZHAWK else None
ROM_USA = Path(os.environ["SC64_ROM"]) if os.environ.get("SC64_ROM") else None

HERE = Path(__file__).resolve().parent
LUA_TMPL = HERE / "harness.lua.tmpl"

# Community-documented failure sites (see project notes).
WAIT_LOOP = (0x80004994, 0x80004998)
EXCEPTION_VECTOR = 0x80000180

MAP_INDEX_ADDR = 0x800D13F8       # u16, current map index
SECOND_PLAYER_ADDR = 0x800AFEFC
PROGRESSION_ADDR = 0x800D13C4     # 6 bytes, one per episode


class EmuError(RuntimeError):
    pass


class EmuCrashed(EmuError):
    pass


class EmuTimeout(EmuError):
    pass


# The Mupen64Plus core keeps its sync settings under the CORE class name, not
# under the settings class name -- writing to
# "...N64.N64SyncSettings" is silently ignored and BizHawk fills in its own
# defaults under this key instead.
N64_SYNC_KEY = "BizHawk.Emulation.Cores.Nintendo.N64.N64"
N64_SYNC_TYPE = ("BizHawk.Emulation.Cores.Nintendo.N64.N64SyncSettings, "
                 "BizHawk.Emulation.Cores")


def _patch_n64_hardware(cfg: dict, expansion_pak: bool, controllers: int) -> None:
    """Plug in the Expansion Pak and however many controllers are wanted.

    Both matter to StarCraft 64: its two-player mode refuses to start without
    an Expansion Pak AND a second controller, and says so on screen. The
    defaults have DisableExpansionSlot true and only controller 1 connected.

    Do not infer the Pak's presence from the RDRAM domain size. It reads
    8,388,608 either way -- that is the addressable size, not what is
    installed -- and reading it as confirmation is how this went unnoticed.
    """
    css = cfg.setdefault("CoreSyncSettings", {})
    s = css.get(N64_SYNC_KEY) or {"$type": N64_SYNC_TYPE}
    s["DisableExpansionSlot"] = not expansion_pak
    pads = s.get("Controllers") or [
        {"PakType": 1, "IsConnected": i == 0} for i in range(4)]
    for i in range(4):
        pads[i]["IsConnected"] = i < controllers
    s["Controllers"] = pads
    css[N64_SYNC_KEY] = s


# The default is zoom factor 1, a 320x240 video area, which is unreadable when
# you are watching a run rather than screenshotting it.
ZOOM = 2                      # 320x240 * 2 = 640x480 video area

# Base config.ini text, read once -- see _patch_config.
_BASE_CONFIG: str | None = None


def _patch_config(dst: Path, core: str = "Ares64", *,
                  expansion_pak: bool = True, controllers: int = 1) -> None:
    """Copy BizHawk's config and force the settings a headless run needs.

    We never touch the user's own config.ini -- EmuHawk is launched with
    --config pointing at this copy.

    The base config is read once per process and cached. Re-reading it on
    every boot opened a window where a parallel worker's EmuHawk, saving
    state on exit, held the file just as another worker's boot read it --
    intermittent Permission denied under 6-way sweeps. The first read still
    tolerates a briefly locked file rather than failing the whole run on a
    2-second collision.
    """
    global _BASE_CONFIG
    if _BASE_CONFIG is None:
        last = None
        for _ in range(20):                                # ~2s of patience
            try:
                _BASE_CONFIG = (BIZHAWK / "config.ini").read_text(encoding="utf-8")
                break
            except PermissionError as exc:
                last = exc
                time.sleep(0.1)
        else:
            raise EmuError(f"cannot read {BIZHAWK / 'config.ini'}: {last}")
    cfg = json.loads(_BASE_CONFIG)
    cfg["PreferredCores"]["N64"] = core
    cfg.update({
        # Run as fast as the host allows; frame counts stay deterministic.
        "ClockThrottle": False,
        "VSyncThrottle": False,
        "SoundThrottle": False,
        "Unthrottled": True,
        "SoundEnabled": False,
        "SoundEnabledNormal": False,
        "StartPaused": False,
        # A menu click would otherwise silently freeze the run.
        "PauseWhenMenuActivated": False,
        "RunInBackground": True,
        "SingleInstanceMode": False,
        # No dialogs, ever.
        "UpdateAutoCheckEnabled": False,
        "SkipOutdatedOsCheck": True,
        "SkipSuperuserPrivsCheck": True,
        "SkipRATelemetryWarning": True,
        "SkipWaterboxIntegrityChecks": True,
        "FirstBoot": False,
        "SaveWindowPosition": False,
        "ShowLogWindow": False,
        "AutoSaveLastSaveSlot": False,
        "BackupSaveram": False,
        "AutosaveSaveRAM": False,
        "DontTryOtherCores": True,
        "DispFixAspectRatio": True,
    })
    # Window size comes from the per-system zoom factor, not MainWindowSize --
    # setting that key alone is ignored. The N64 framebuffer is 320x240, so
    # zoom 2 gives a 640x480 video area. Only what a human sees changes;
    # screenshots are read from the framebuffer and stay 320x240 whatever this
    # is set to, so no measurement or threshold depends on it.
    cfg.setdefault("TargetZoomFactors", {})["N64"] = ZOOM
    _patch_n64_hardware(cfg, expansion_pak, controllers)
    dst.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


class Emu:
    def __init__(self, session_dir: str | os.PathLike | None = None,
                 core: str = "Ares64", show_window: bool = True,
                 expansion_pak: bool = True, controllers: int = 1):
        # resolve(): EmuHawk runs with its own working directory, and the Lua
        # gets this path verbatim. A relative session_dir therefore makes the
        # Lua write its ready/response files somewhere under the BizHawk
        # install, Python waits on a path that never appears, and the boot
        # times out after 90s with nothing to explain it. Costs an afternoon
        # the first time; resolving here means it cannot happen again.
        self.dir = Path(session_dir or
                        (HERE.parent / "runs" / time.strftime("s%Y%m%d-%H%M%S"))).resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.core = core
        self.expansion_pak = expansion_pak
        self.controllers = controllers
        self.show_window = show_window
        self.proc: subprocess.Popen | None = None
        self._seq = 1
        self.boot_wall = None

    # ------------------------------------------------------------ lifecycle

    def boot(self, rom_path: str | os.PathLike = ROM_USA, timeout: float = 90.0):
        """Start EmuHawk on `rom_path` and wait for the Lua harness to answer."""
        if EMUHAWK is None or not EMUHAWK.is_file():
            raise EmuError("cannot find BizHawk -- set BIZHAWK_DIR to the "
                           "folder holding EmuHawk.exe")
        if rom_path is None:
            raise EmuError("no ROM given and SC64_ROM is not set -- pass a "
                           "path to boot() or set SC64_ROM")
        rom = Path(rom_path).resolve()
        if not rom.is_file():
            raise EmuError(f"ROM not found: {rom}")

        for stale in self.dir.glob("*.txt"):
            stale.unlink()

        lua = self.dir / "harness.lua"
        lua.write_text(
            LUA_TMPL.read_text(encoding="utf-8").replace("@@DIR@@", str(self.dir)),
            encoding="utf-8")

        cfg = self.dir / "config.ini"
        _patch_config(cfg, self.core, expansion_pak=self.expansion_pak,
                      controllers=self.controllers)

        t0 = time.time()
        self.proc = subprocess.Popen(
            [str(EMUHAWK), str(rom), f"--lua={lua}", f"--config={cfg}"],
            cwd=str(BIZHAWK),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        ready = self.dir / "ready.txt"
        while time.time() - t0 < timeout:
            if ready.is_file():
                self.boot_wall = time.time() - t0
                # First round-trip proves the loop is really turning.
                self.cmd("PING")
                return self.boot_wall
            if self.proc.poll() is not None:
                raise EmuError(f"EmuHawk exited during boot, rc={self.proc.returncode}")
            time.sleep(0.05)
        raise EmuTimeout(f"harness never became ready within {timeout}s")

    def close(self):
        """Shut EmuHawk down and WAIT until it is actually gone.

        Returning while the process is still dying looks harmless and is not:
        the caller's next move is usually to delete the session's ROM or reuse
        its directory, and on Windows both fail with Permission denied while
        EmuHawk still holds the handles. A six-way parallel sweep hit exactly
        that -- workers finishing in waves, each unlink racing a neighbour's
        exit -- and every affected boot succeeded on sequential retry, which
        is the signature of a shutdown race rather than anything in the run.
        So the contract is: close() does not return until the process is
        reaped, even on the kill() path, and an unkillable EmuHawk is a loud
        error rather than a leaked lock.
        """
        if self.proc and self.proc.poll() is None:
            try:
                self._send("QUIT")
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=10)
                except Exception:
                    pass
        if self.proc and self.proc.poll() is None:
            raise EmuError("EmuHawk did not exit even after kill(); "
                           "its session files are still locked")
        self.proc = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ transport

    def _send(self, cmd: str, *args) -> int:
        seq = self._seq
        self._seq += 1
        body = "\n".join([cmd] + [str(a) for a in args])
        tmp = self.dir / f"req_{seq}.tmp"
        # write_bytes, not write_text: text mode would translate \n to \r\n on
        # Windows and the stray \r ends up glued to the command name.
        tmp.write_bytes(body.encode("utf-8"))
        os.replace(tmp, self.dir / f"req_{seq}.txt")
        return seq

    def cmd(self, cmd: str, *args, timeout: float = 120.0) -> str:
        if self.proc is None:
            raise EmuError("not booted")
        seq = self._send(cmd, *args)
        resp = self.dir / f"resp_{seq}.txt"
        t0 = time.time()
        while True:
            if resp.is_file():
                body = resp.read_text(encoding="utf-8")
                resp.unlink(missing_ok=True)
                status, _, payload = body.partition("\n")
                if status.strip() != "OK":
                    raise EmuError(f"{cmd}: {payload.strip()}")
                return payload
            if self.proc.poll() is not None:
                raise EmuCrashed(
                    f"EmuHawk process died waiting for {cmd} (rc={self.proc.returncode})")
            if time.time() - t0 > timeout:
                raise EmuTimeout(f"{cmd} did not answer in {timeout}s (emulator hung?)")
            time.sleep(0.004)

    # ------------------------------------------------------------ primitives

    def run_frames(self, n: int, timeout: float = 300.0) -> int:
        """Advance exactly n emulated frames. Returns the new frame count."""
        return int(self.cmd("FRAMES", n, timeout=timeout))

    def read_mem(self, addr: int, size: int, domain: str = "RDRAM") -> bytes:
        return bytes.fromhex(self.cmd("READ", addr, size, domain))

    def write_mem(self, addr: int, data: bytes, domain: str = "RDRAM") -> int:
        return int(self.cmd("WRITE", addr, data.hex(), domain))

    def read_u16(self, addr: int, domain: str = "RDRAM") -> int:
        return int.from_bytes(self.read_mem(addr, 2, domain), "big")

    def read_u32(self, addr: int, domain: str = "RDRAM") -> int:
        return int.from_bytes(self.read_mem(addr, 4, domain), "big")

    def write_u16(self, addr: int, value: int, domain: str = "RDRAM") -> int:
        return self.write_mem(addr, value.to_bytes(2, "big"), domain)

    def screenshot(self, path: str | os.PathLike) -> Path:
        # Every path handed across to the Lua must be absolute: EmuHawk has its
        # own working directory, so a relative one silently writes into the
        # BizHawk install and the wait here times out pointing at a file that
        # was never going to appear.
        p = Path(path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        self.cmd("SHOT", str(p))
        for _ in range(200):                      # PNG write is async-ish
            if p.is_file() and p.stat().st_size > 0:
                return p
            time.sleep(0.02)
        raise EmuError(f"screenshot never appeared: {p}")

    def press(self, buttons, frames: int = 4, pad: int = 1) -> int:
        """Hold `buttons` (list of BizHawk N64 button names) for n frames."""
        if isinstance(buttons, str):
            buttons = [buttons]
        return int(self.cmd("HOLD", frames, ",".join(buttons), pad, timeout=300))

    def tap(self, button: str, hold: int = 4, gap: int = 10):
        self.press([button], hold)
        self.run_frames(gap)

    def savestate(self, path):
        return self.cmd("SAVESTATE", str(Path(path).resolve()))

    def loadstate(self, path):
        return self.cmd("LOADSTATE", str(Path(path).resolve()))

    # ------------------------------------------------------ instrumentation
    #
    # Watchpoints need the Mupen64Plus core. Ares64 accepts the registration,
    # hands back an all-zero hook id and then never calls back -- so a watch
    # on it reports "nothing touched this address", which is indistinguishable
    # from a real result. The Lua rejects the null id rather than let that
    # through, and Emu(core="Mupen64Plus") is what you want for any run that
    # uses these.

    def watch(self, addr: int, kind: str = "read",
              scope: str = "System Bus",
              snap_addr: int | None = None, snap_len: int = 256) -> str:
        """Record the PC of every access to `addr`.

        Aggregated by PC on the Lua side: the question is which code touches
        the address, and a per-hit log of a word the engine reads every frame
        is a great deal of the same three addresses.
        """
        args = [addr, kind, scope]
        if snap_addr is not None:
            args += [snap_addr, snap_len]
        return self.cmd("WATCH", *args).strip()

    def snap_dump(self) -> dict:
        """{key: (frame, pc, addr, bytes)} captured when each watch first fired.

        Captured inside the callback on purpose: overlay code is resident only
        while it runs, so reading those addresses afterwards disassembles
        whatever replaced it.
        """
        out = {}
        for line in self.cmd("SNAPDUMP").splitlines():
            if not line.strip() or line.strip() == "none":
                continue
            key, frame, pc, addr, regs, hexs = line.split("\t")
            rd = {}
            for pair in (regs.split(',') if regs else []):
                rk, _, rv = pair.partition('=')
                rd[rk] = int(rv, 16)
            out[key] = {'frame': int(frame), 'pc': int(pc, 16),
                        'addr': int(addr, 16), 'regs': rd,
                        'data': (b'' if hexs.startswith('ERR:')
                                 else bytes.fromhex(hexs)),
                        'error': hexs[4:] if hexs.startswith('ERR:') else None}
        return out

    def watch_dump(self, top: int = 64) -> dict:
        """{'total_hits': int, 'truncated': bool, 'watches': {key: [rows]}}.

        Each row is a dict: pc, overlay, hits, first, last.

        Rows are keyed by (pc, overlay), not pc alone. An address in an
        overlay window hosts different code at different times, so merging
        on the address silently adds together unrelated functions.
        """
        out = {"total_hits": 0, "truncated": False, "watches": {}}
        cur = None
        for line in self.cmd("WATCHDUMP", top).splitlines():
            if line.startswith("total_hits="):
                out["total_hits"] = int(line.partition("=")[2])
            elif line.startswith("truncated="):
                out["truncated"] = line.partition("=")[2].strip() == "true"
            elif line.startswith("watch="):
                cur = line.partition("=")[2].split()[0]
                out["watches"][cur] = []
            elif line.strip() and cur:
                pc, sig, n, first, last = line.split()
                out["watches"][cur].append(
                    {"pc": int(pc, 16), "overlay": sig, "hits": int(n),
                     "first": int(first), "last": int(last)})
        return out

    def sig_set(self, addr: int, words: int = 8) -> str:
        """Sample `words` words at `addr` as the resident overlay's identity.

        Point this at a fixed anchor inside the overlay window being studied.
        Every subsequent watch hit is tagged with it, so hits at one address
        under two different resident images stay separate.
        """
        return self.cmd("SIGSET", addr, words).strip()

    def sig_clear(self) -> str:
        return self.cmd("SIGCLEAR").strip()

    def sig(self) -> str:
        """The signature right now, or '-' if unset, 'ERR' if unreadable."""
        return self.cmd("SIG").strip()

    def call_log(self, addr: int, reg: str = "a0") -> str:
        """Record `reg` at every execution of `addr`, not just the first.

        For a resolver like 0x8002F584, which takes a BOLT resource id in a0,
        this answers "what did the game ask for, and in what order" -- which
        is the only way to pair an image with its palette when the pixels
        cannot tell them apart.
        """
        return self.cmd("CALLLOG", addr, reg).strip()

    def call_log_dump(self, top: int = 400) -> dict:
        """{key: [(value, hits, first_frame, last_frame), ...]} in load order."""
        out: dict[str, list] = {}
        cur = None
        for line in self.cmd("CALLLOGDUMP", top).splitlines():
            if line.startswith("log="):
                cur = line.partition("=")[2].split()[0]
                if cur == "none":
                    cur = None
                else:
                    out[cur] = []
            elif line.strip() and cur:
                v, n, first, last = line.split()
                out[cur].append((int(v, 16), int(n), int(first), int(last)))
        return out

    def watch_clear(self) -> int:
        return int(self.cmd("WATCHCLEAR").strip())

    def trace(self, frames: int = 120) -> int:
        """Sample the PC once per frame for `frames` frames.

        Coarse next to a watchpoint -- one sample a frame catches only what
        the CPU is doing at the moment the Lua loop runs -- but it needs
        nothing except a readable PC, so it still reports on a core whose
        memory callbacks are inert, and it still separates a wedged loop from
        one that is making progress.
        """
        return int(self.cmd("TRACE", frames, timeout=30.0 + frames * 0.5))

    def trace_dump(self, top: int = 40) -> dict:
        """{'samples': int, 'distinct_pcs': int, 'top': [(pc, hits), ...]}."""
        out = {"samples": 0, "distinct_pcs": 0, "top": []}
        for line in self.cmd("TRACEDUMP", top).splitlines():
            if line.startswith("samples="):
                out["samples"] = int(line.partition("=")[2])
            elif line.startswith("distinct_pcs="):
                out["distinct_pcs"] = int(line.partition("=")[2])
            elif line.strip():
                pc, n = line.split()
                out["top"].append((int(pc, 16), int(n)))
        return out

    # ------------------------------------------------------------ health

    def is_alive(self) -> bool:
        """Process up AND the Lua frame loop still turning."""
        if self.proc is None or self.proc.poll() is not None:
            return False
        try:
            a = self.status()["frame"]
            self.run_frames(2)
            return self.status()["frame"] > a
        except EmuError:
            return False

    def status(self) -> dict:
        out = {}
        for line in self.cmd("STATUS").splitlines():
            k, _, v = line.partition("=")
            out[k] = int(v) if v.isdigit() else v
        if isinstance(out.get("pc"), str) and out["pc"].startswith("0x"):
            out["pc"] = int(out["pc"], 16)
        return out

    def heartbeat(self) -> tuple[int, int] | None:
        """(frame, pc) from the file the Lua loop rewrites every 5 frames.

        Unlike status() this needs no round trip, so it still reports when
        the command loop itself is wedged.
        """
        hb = self.dir / "hb.txt"
        for _ in range(3):
            try:
                parts = hb.read_text().split()
                if len(parts) >= 2:
                    return int(parts[0]), int(parts[1])
            except (OSError, ValueError):
                time.sleep(0.02)
        return None

    def detect_crash(self, samples: int = 30, gap: int = 3) -> dict:
        """Classify the run: ok / hung / crashed / dead.

        Samples the PC and the framebuffer over `samples * gap` frames and
        looks for the documented failure sites, a frozen frame counter, or a
        frozen picture.
        """
        if self.proc is None or self.proc.poll() is not None:
            return {"verdict": "dead", "reason": "EmuHawk process is not running"}

        pcs, frames = [], []
        for _ in range(samples):
            try:
                st = self.status()
            except EmuError as exc:
                return {"verdict": "dead", "reason": str(exc)}
            frames.append(st["frame"])
            if isinstance(st.get("pc"), int):
                pcs.append(st["pc"])
            try:
                self.run_frames(gap, timeout=20)
            except EmuTimeout:
                return {"verdict": "hung", "reason": "frame advance stopped responding",
                        "pcs": pcs}

        info = {"pc_samples": len(pcs), "pc_unique": len(set(pcs)),
                "frames_advanced": frames[-1] - frames[0]}

        if info["frames_advanced"] <= 0:
            return {"verdict": "hung", "reason": "frame counter frozen", **info}

        # The PC is the trustworthy signal and it takes precedence. A varied PC
        # means the CPU is executing varied code, which is the definition of
        # alive -- so when we have it we do NOT consult the framebuffer at all.
        # StarCraft 64 parks on Advisor dialog boxes waiting for input, and on
        # those screens the picture is legitimately frozen for as long as you
        # let it sit. Judging that as a hang misreports perfectly good maps
        # (measured: indices 1, 2, 0x30 and 0x57 all false-positived this way).
        if pcs:
            bad = sum(1 for p in pcs if p in WAIT_LOOP or p == EXCEPTION_VECTOR)
            info["pc_in_failure_sites"] = bad
            info["pc_min"], info["pc_max"] = min(pcs), max(pcs)
            if bad == len(pcs):
                return {"verdict": "crashed",
                        "reason": "PC pinned to documented wait loop / exception vector",
                        **info}
            if bad > len(pcs) * 0.8:
                return {"verdict": "crashed",
                        "reason": f"PC in failure sites for {bad}/{len(pcs)} samples",
                        **info}
            if info["pc_unique"] == 1:
                return {"verdict": "hung",
                        "reason": f"PC never left {pcs[0]:#010x}", **info}
            return {"verdict": "ok",
                    "reason": f"frames advancing, PC over {info['pc_unique']} sites",
                    **info}

        # No PC from this core: fall back to the picture, which is weaker.
        h = self.framebuffer_hashes(3, 20)
        info["fb_hashes"] = len(set(h))
        if len(set(h)) == 1:
            return {"verdict": "unknown",
                    "reason": "no PC available and framebuffer static -- "
                              "could be a hang or a dialog box awaiting input", **info}
        return {"verdict": "ok", "reason": "frames advancing, picture changing", **info}

    def is_idle_screen(self, n: int = 3, gap: int = 20) -> bool:
        """True if the picture is frozen. Advisory only -- on this game it means
        'waiting for input' at least as often as it means 'stuck'."""
        return len(set(self.framebuffer_hashes(n, gap))) == 1

    def framebuffer_hashes(self, n: int = 3, gap: int = 20) -> list[str]:
        out = []
        tmp = self.dir / "_fb.png"
        for _ in range(n):
            tmp.unlink(missing_ok=True)
            self.screenshot(tmp)
            out.append(hashlib.sha256(tmp.read_bytes()).hexdigest())
            self.run_frames(gap)
        tmp.unlink(missing_ok=True)
        return out

    # ------------------------------------------------------------ game-level

    def map_index(self) -> int:
        return self.read_u16(MAP_INDEX_ADDR)

    def set_map_index(self, idx: int):
        self.write_u16(MAP_INDEX_ADDR, idx)

    # Button sequence from the title screen to the first mission briefing.
    # Frame waits are generous; the menus are not timing-sensitive.
    BRIEFING_ROUTE = [("Start", 120), ("Start", 120), ("A", 150),
                      ("A", 150), ("DPad D", 60), ("A", 180)]

    def to_briefing(self, title_frames: int = 900):
        """Boot screen -> Single-Player -> mission briefing (Start begins it)."""
        self.run_frames(title_frames)
        for btn, wait in self.BRIEFING_ROUTE:
            self.press([btn], 6)
            self.run_frames(wait)

    def load_map(self, idx: int, settle: int = 900):
        """From a briefing savestate: force `idx` and begin the mission."""
        self.set_map_index(idx)
        self.run_frames(30)
        self.press(["Start"], 6)
        self.run_frames(settle)
