"""Crew chief radio: spoken calls generated from the RaceBox session.

Calls (each lap is compared with your best lap so far):
  * every lap:      lap time, and how it compares ("purple lap, three tenths quicker")
  * purple sector:  a session-best sector
  * theoretical:    best sectors added up, when it's well under your best lap
  * corners:        run wide, braked early, slow apex speed (when it cost time),
                    and good late braking (when it gained time)
  * top speed:      a new session top speed
  * consistency:    several laps in a row within a few tenths

Corners are found automatically from the curvature of your best lap and
numbered from the start/finish line, so "turn 3" means the third corner
the app found, which may not match the track's official numbering.

Speech comes from macOS's built-in `say` voices, or ffmpeg's simple `flite`
voice where `say` isn't available. Calls get a team-radio sound (narrow
band, a squelch burst before and after) and are mixed over the video's own
audio, which dips under each call.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .telemetry import Telemetry
from .timing import Timing

RATE = 48000


@dataclass
class Call:
    t: float              # telemetry time the call relates to (speak at or after this)
    text: str
    kind: str             # lap, sector, corner, topspeed, consistency
    priority: int         # 1 = must say ... 3 = nice to have
    lap: int | None = None
    corner: int | None = None
    start: float | None = None     # scheduled start, telemetry time
    duration: float = 0.0
    audio: np.ndarray | None = field(default=None, repr=False)


# ---------------------------------------------------------------- speech text

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
         "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def words(n: int) -> str:
    n = int(n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] + ("-" + _ONES[o] if o else "")
    h, r = divmod(n, 100)
    if r == 0:
        return _ONES[h] + " hundred"
    if r < 10:                       # 105 -> "one oh five"
        return f"{_ONES[h]} oh {_ONES[r]}"
    return f"{_ONES[h]} {words(r)}"  # 112 -> "one twelve"


def say_laptime(s: float) -> str:
    """89.84 -> 'one twenty-nine eight'; 57.63 -> 'fifty-seven point six'."""
    s = round(s, 1)
    m, sec = divmod(s, 60)
    whole, tenth = int(sec), int(round((sec - int(sec)) * 10)) % 10
    if m < 1:
        return f"{words(whole)} point {words(tenth)}"
    secs = f"oh {words(whole)}" if whole < 10 else words(whole)
    tail = "flat" if tenth == 0 else words(tenth)
    return f"{words(int(m))} {secs} {tail}"


def say_gap(d: float) -> str:
    """A time difference, the way an engineer says it."""
    d = abs(d)
    if d < 0.095:
        return "a few hundredths"
    if d < 0.95:
        n = int(round(d * 10))
        return {1: "a tenth", 5: "half a second"}.get(n, f"{words(n)} tenths")
    if abs(d - 1) < 0.05:
        return "a second"
    r = round(d, 1)
    whole, tenth = int(r), int(round((r - int(r)) * 10))
    return f"{words(whole)} point {words(tenth)} seconds" if tenth else f"{words(whole)} seconds"


# ---------------------------------------------------------------- analysis

@dataclass
class Corner:
    number: int
    s0: float       # entry, metres from the line along the best lap
    s1: float       # exit
    apex: float
    sign: int       # +1 right-hander, -1 left-hander


# Kinds of call that can be switched on and off, and the sensitivity settings.
KINDS = {
    "lap": "Lap times",
    "sector": "Purple sectors",
    "theoretical": "Theoretical best",
    "corner": "Corner mistakes",
    "good": "Good braking",
    "topspeed": "Top speed",
    "consistency": "Consistency",
    "ghost": "Ghost gap",
}
DEFAULTS = {
    "wide_m": 2.5,            # this far outside the best lap's line counts as running wide
    "early_brake_m": 12.0,    # braking this much earlier than on the best lap
    "slow_corner_kmh": 6.0,   # carrying this much less speed through a corner
    "min_loss": 0.1,          # a corner mistake has to cost at least this much time
    "max_corner_calls": 2,    # most mistake calls per lap
    "consistency_s": 0.35,    # laps within this spread count as consistent
    "ghost_gap_m": 5.0,       # ghost calls: gap has to change by this many metres
    "off": [],                # kinds of call switched off
}


class CrewChief:
    def __init__(self, tel: Telemetry, timing: Timing, units: str = "mph",
                 wide_m: float = 2.5, early_brake_m: float = 12.0, slow_apex_kmh: float = 6.0,
                 min_loss: float = 0.1, max_corner_calls: int = 2, consistency_s: float = 0.35,
                 off: set[str] | None = None, ghost_ref: int | None = None,
                 ghost_gap_m: float = 5.0, race=None):
        """ghost_ref: index of the ghost lap when the ghost is on. Corners are then
        judged against the ghost (so the voice and the picture agree), and the
        ghost-gap calls are made."""
        self.tel, self.tm = tel, timing
        self.ghost_ref, self.ghost_gap_m = ghost_ref, ghost_gap_m
        self.units = units
        self.wide_m, self.early_brake_m = wide_m, early_brake_m
        self.slow_apex_kmh, self.min_loss = slow_apex_kmh, min_loss
        self.max_corner_calls = int(max_corner_calls)
        self.consistency_s = consistency_s
        from .race import RACE_KINDS
        self.race = race      # race mode: race calls replace the plain lap-time call
        self.on = (set(KINDS) | set(RACE_KINDS)) - set(off or ())
        self.laps = timing.laps
        self.corners: list[Corner] = []
        if len(self.laps) >= 2:
            self.best_i = min(range(len(self.laps)), key=lambda i: self.laps[i].time)
            self._profiles = [self._lap_profile(l) for l in self.laps]
            self.L = self._profiles[self.best_i]["s"][-1]
            self.corners = self._find_corners()

    @classmethod
    def from_settings(cls, tel: Telemetry, timing: Timing, units: str = "mph",
                      settings: dict | None = None, ghost_ref: int | None = None,
                      race=None) -> "CrewChief":
        s = dict(DEFAULTS)
        s.update({k: v for k, v in (settings or {}).items() if v is not None})
        return cls(tel, timing, units, wide_m=float(s["wide_m"]),
                   early_brake_m=float(s["early_brake_m"]), slow_apex_kmh=float(s["slow_corner_kmh"]),
                   min_loss=float(s["min_loss"]), max_corner_calls=int(s["max_corner_calls"]),
                   consistency_s=float(s["consistency_s"]), off=set(s["off"] or ()),
                   ghost_ref=ghost_ref, ghost_gap_m=float(s["ghost_gap_m"]), race=race)

    # per-lap arrays against distance (metres), normalised to the best lap's length
    def _lap_profile(self, lap) -> dict:
        tel, tm = self.tel, self.tm
        m = (tel.t >= lap.t_start) & (tel.t <= lap.t_end)
        t = tel.t[m] - lap.t_start
        s = tm.dist[m] - np.interp(lap.t_start, tel.t, tm.dist)
        return {"s": s, "t": t, "x": tm.x[m], "y": tm.y[m], "v": tel.speed_kmh[m]}

    def _grid(self, prof: dict, step: float = 1.0) -> dict:
        """Resample a lap every metre of (normalised) distance."""
        L = self.L
        s = prof["s"] * (L / max(prof["s"][-1], 1.0))
        g = np.arange(0, L, step)
        out = {"s": g}
        for k in ("t", "x", "y", "v"):
            out[k] = np.interp(g, s, prof[k])
        k = max(3, int(9 / step) | 1)
        xs = np.convolve(out["x"], np.ones(k) / k, mode="same")
        ys = np.convolve(out["y"], np.ones(k) / k, mode="same")
        h = np.unwrap(np.arctan2(np.gradient(xs), np.gradient(ys)))   # clockwise from north
        out["h"] = h
        return out

    def _find_corners(self) -> list[Corner]:
        g = self._grid(self._profiles[self.best_i])
        k = np.gradient(g["h"]) / 1.0                      # rad per metre, + = right
        w = 15
        k = np.convolve(k, np.ones(w) / w, mode="same")
        on = np.abs(k) > 1 / 150                          # tighter than a 150 m radius
        runs, i = [], 0
        while i < len(on):
            if on[i]:
                j = i
                while j < len(on) and on[j] and np.sign(k[j]) == np.sign(k[i]):
                    j += 1
                runs.append([i, j - 1, int(np.sign(k[i]))])
                i = j
            else:
                i += 1
        merged = []
        for r in runs:                                     # join pieces of one corner
            if merged and r[2] == merged[-1][2] and r[0] - merged[-1][1] < 25:
                merged[-1][1] = r[1]
            else:
                merged.append(r)
        corners = []
        for a, b, sign in merged:
            turn = abs(g["h"][b] - g["h"][a])
            if b - a < 12 or math.degrees(turn) < 25:
                continue
            apex = a + int(np.argmin(g["v"][a:b + 1]))
            corners.append(Corner(len(corners) + 1, float(g["s"][a]), float(g["s"][b]),
                                  float(g["s"][apex]), sign))
        return corners

    # ------------------------------------------------------------ calls

    def calls(self) -> list[Call]:
        out: list[Call] = []
        if self.race is not None:
            from .race import race_calls
            out += race_calls(self.race, self.on, self.units)
        laps = self.laps
        if not laps:
            return sorted(out, key=lambda c: (c.t, c.priority))
        lap_on = "lap" in self.on and self.race is None
        best_so_far = None
        best_sectors: list[float] | None = None
        top = None
        last_theo = None
        streak_said = 0
        grids = [self._grid(p) for p in self._profiles] if len(laps) >= 2 else []

        for k, lap in enumerate(laps):
            n = lap.number
            secs = lap.sectors

            # ---- purple sectors (not on the first lap: everything is a best then)
            final_purple = False
            if secs and best_sectors and k > 0:
                for i, sv in enumerate(secs):
                    if sv < best_sectors[i] - 0.005:
                        if "sector" not in self.on:
                            continue
                        if i == len(secs) - 1 and lap_on:
                            final_purple = True          # said as part of the lap call
                        else:
                            at = lap.splits[i] if i < len(secs) - 1 else lap.t_end
                            out.append(Call(at + 0.6, f"Purple sector {words(i + 1)}.", "sector", 2, n))

            # ---- corners, against the ghost when it's on, else the best lap so far
            ghosted = self.ghost_ref is not None and k != self.ghost_ref
            ref_i = self.ghost_ref if ghosted else (laps.index(best_so_far) if best_so_far else None)
            mistakes = []
            if self.corners and ref_i is not None and ref_i != k and ({"corner", "good"} & self.on):
                mistakes = self._corner_calls(k, ref_i, grids)
                out += mistakes
            if ghosted and "ghost" in self.on and grids:
                out += self._ghost_calls(k, grids, {c.corner for c in mistakes})

            # ---- top speed on this lap
            m = (self.tel.t >= lap.t_start) & (self.tel.t <= lap.t_end)
            if m.any():
                i = int(np.argmax(np.where(m, self.tel.speed_kmh, -1)))
                vmax = float(self.tel.speed_kmh[i])
                f = 0.621371 if self.units == "mph" else 1.0
                if top is not None and (vmax - top) * f >= 1.0 and "topspeed" in self.on:
                    out.append(Call(float(self.tel.t[i]) + 0.8,
                                    f"New top speed, {words(round(vmax * f))}.", "topspeed", 3, n))
                top = vmax if top is None else max(top, vmax)

            # ---- the lap call at the line
            parts = [f"Lap {words(n)}, {say_laptime(lap.time)}."]
            if best_so_far is not None:
                gap = lap.time - best_so_far.time
                if gap < 0:
                    parts.append(f"Purple lap, {say_gap(gap)} quicker.")
                elif gap < 0.3:
                    parts.append(f"Just {say_gap(gap)} off.")
                else:
                    parts.append(f"{say_gap(gap).capitalize()} off your best.")
            if final_purple:
                parts.append("Purple final sector.")
            if ghosted and "ghost" in self.on:
                parts.append(self._ghost_result(k))
            if secs:
                best_sectors = list(secs) if best_sectors is None else \
                    [min(a, b) for a, b in zip(best_sectors, secs)]
            if best_so_far is None or lap.time < best_so_far.time:
                best_so_far = lap
            if best_sectors and k > 0 and "theoretical" in self.on:
                theo = sum(best_sectors)
                if theo < best_so_far.time - 0.15 and (last_theo is None or theo < last_theo - 0.05):
                    msg = f"There's a {say_laptime(theo)} in you."
                    if lap_on:
                        parts.append(msg)
                    else:
                        out.append(Call(lap.t_end + 1.2, msg, "theoretical", 2, n))
                    last_theo = theo
            if lap_on:
                out.append(Call(lap.t_end + 0.8, " ".join(parts), "lap", 1, n))

            # ---- consistency streaks
            times = [l.time for l in laps[:k + 1]]
            streak = 1
            while streak < len(times):
                win = times[-(streak + 1):]
                if max(win) - min(win) <= self.consistency_s:
                    streak += 1
                else:
                    break
            if streak >= 3 and streak > streak_said and streak in (3, 5, 8, 12) and \
                    "consistency" in self.on:
                spread = max(times[-streak:]) - min(times[-streak:])
                out.append(Call(lap.t_end + 1.0,
                                f"{words(streak).capitalize()} laps within {say_gap(spread)}. Nice and consistent.",
                                "consistency", 3, n))
                streak_said = streak
            elif streak < 3:
                streak_said = 0
        return sorted(out, key=lambda c: (c.t, c.priority))

    # ------------------------------------------------------------ ghost

    def _gap(self, k: int, t: float) -> float:
        """Metres the ghost is ahead (+) or behind (-) at time t in lap k, as if
        you'd both started the lap together."""
        lap = self.laps[k]
        el_k, d_k = self.tm._lap_profiles[k]
        el_r, d_r = self.tm._lap_profiles[self.ghost_ref]
        e = t - lap.t_start
        where = float(np.interp(e, el_k, d_k)) / max(d_k[-1], 1.0) * d_r[-1]
        if e <= el_r[-1]:
            ghost_at = float(np.interp(e, el_r, d_r))
        else:                       # the ghost has finished: keep it going at its finishing speed
            ref = self.laps[self.ghost_ref]
            v_end = float(np.interp(ref.t_end, self.tel.t, self.tel.speed_kmh)) / 3.6
            ghost_at = d_r[-1] + (e - el_r[-1]) * v_end
        return ghost_at - where

    @staticmethod
    def _metres(m: float) -> str:
        n = max(1, int(round(abs(m))))
        return "one metre" if n == 1 else f"{words(n)} metres"

    def _ghost_result(self, k: int) -> str:
        lap, ref = self.laps[k], self.laps[self.ghost_ref]
        dt = lap.time - ref.time
        v = float(np.interp(ref.t_end, self.tel.t, self.tel.speed_kmh)) / 3.6   # speed at the line
        m = abs(dt) * v
        if m < 1:
            return "Dead level with the ghost at the line."
        return f"Beat the ghost by {self._metres(m)}." if dt < 0 else \
            f"The ghost beat you by {self._metres(m)}."

    def _ghost_calls(self, k: int, grids: list[dict], skip_corners: set) -> list[Call]:
        """Gap to the ghost: overtakes, corners where it changed, and updates at
        the sector splits. At most one per sector, best first."""
        lap = self.laps[k]
        A = grids[k]
        splits = list(lap.splits or [])
        sector_of = lambda t: sum(1 for sp in splits if sp <= t)
        cand = []                                       # (sector, rank, size, Call)
        # overtakes, with a little hysteresis so GPS noise doesn't flip-flop
        state = None
        for t in np.arange(lap.t_start + 2.0, lap.t_end - 1.0, 0.2):
            g = self._gap(k, float(t))
            new = "ghost" if g > 1.5 else "you" if g < -1.5 else state
            if state is not None and new != state:
                text = "You're ahead of the ghost!" if new == "you" else "The ghost is back in front."
                cand.append((sector_of(t), 0, 99, Call(float(t) + 0.3, text, "ghost", 2, lap.number)))
            state = new
        # corners where time was won or lost against the ghost (time, not metres:
        # a metre gap shrinks in slow corners and stretches on straights by itself)
        R = grids[self.ghost_ref]
        for c in self.corners:
            if c.number in skip_corners:
                continue                               # a mistake call already covers it
            i0 = int(max(c.s0 - 100, 0))
            i1 = int(min(c.s1 + 30, len(A["s"]) - 1))
            change = (A["t"][i1] - R["t"][i1]) - (A["t"][i0] - R["t"][i0])   # + = lost time
            t1 = lap.t_start + float(A["t"][i1])
            if abs(change) >= max(self.min_loss, 0.1):
                text = (f"Lost {say_gap(change)} to the ghost through turn {words(c.number)}."
                        if change > 0 else
                        f"Gained {say_gap(change)} on the ghost through turn {words(c.number)}.")
                cand.append((sector_of(t1), 1, abs(change) * 100, Call(t1 + 0.3, text, "ghost", 3,
                                                                       lap.number, c.number)))
        # where you stand at each split
        last_said = 0.0
        for i, sp in enumerate(splits):
            g = self._gap(k, sp)
            if i > 0 and abs(g - last_said) < self.ghost_gap_m:
                continue
            if abs(g) < 1.5:
                text = "Level with the ghost."
            elif g > 0:
                text = f"Ghost {self._metres(g)} ahead."
            else:
                text = f"{self._metres(g).capitalize()} up on the ghost."
            last_said = g
            cand.append((i + 1, 2, abs(g), Call(sp + 1.2, text, "ghost", 3, lap.number)))
        best = {}
        for sec, rank, size, call in cand:
            key = (rank, -size)
            if sec not in best or key < best[sec][0]:
                best[sec] = (key, call)
        return [c for _, c in best.values()]

    def _cross_track(self, A: dict, R: dict, a: int, b: int) -> np.ndarray:
        """Sideways distance (m, + = right of travel) of lap A from the reference
        line, using the nearest point on that line rather than the point at the
        same distance, which drifts along the track and fakes offsets in hairpins."""
        lo, hi = max(a - 80, 0), min(b + 80, len(R["s"]))
        rx, ry, rh = R["x"][lo:hi], R["y"][lo:hi], R["h"][lo:hi]
        ax, ay = A["x"][a:b, None], A["y"][a:b, None]
        j = np.argmin((ax - rx[None, :]) ** 2 + (ay - ry[None, :]) ** 2, axis=1)
        nx, ny = np.cos(rh[j]), -np.sin(rh[j])
        return (A["x"][a:b] - rx[j]) * nx + (A["y"][a:b] - ry[j]) * ny

    @staticmethod
    def _brake_point(v: np.ndarray, lo: int, apex: int) -> int:
        """Where braking for this corner began: walk back from the apex while
        speed keeps rising, and stop at the peak."""
        vs = np.convolve(v, np.ones(7) / 7, mode="same")
        i = apex
        while i > lo and vs[i - 1] >= vs[i] - 0.3:
            i -= 1
        # on a flat-out straight the speed plateaus: braking starts at the last
        # point that was still within 2 km/h of the straight's top speed
        seg = vs[i: apex + 1]
        return i + int(np.flatnonzero(seg >= seg.max() - 2.0)[-1])

    def _corner_calls(self, k: int, r: int, grids: list[dict]) -> list[Call]:
        A, R = grids[k], grids[r]
        lap = self.laps[k]
        # steady GPS drift between laps, measured on the straights, is removed
        straight = np.ones(len(A["s"]), bool)
        for c in self.corners:
            straight[int(max(c.s0 - 20, 0)): int(c.s1) + 20] = False
        idx = np.flatnonzero(straight)[::10]
        drift = float(np.median(self._cross_track(A, R, 0, len(A["s"]))[idx])) if len(idx) else 0.0
        f = 0.621371 if self.units == "mph" else 1.0
        unit = "miles an hour" if self.units == "mph" else "K P H"
        found = []
        for c in self.corners:
            i0 = int(max(c.s0 - 100, 0))
            i1 = int(min(c.s1 + 30, len(A["s"]) - 1))
            lost = (A["t"][i1] - R["t"][i1]) - (A["t"][i0] - R["t"][i0])
            a, b = int(c.s0), int(c.s1) + 1
            outside = float(np.max(-c.sign * (self._cross_track(A, R, a, b) - drift)))
            prev_exit = max([p.s1 for p in self.corners if p.s1 < c.s0], default=0.0)
            wb = int(max(c.apex - 350, prev_exit + 5, 0))     # braking zone: from the last corner's exit
            if int(c.apex) - wb >= 20:
                brake_diff = self._brake_point(R["v"], wb, int(c.apex)) - \
                    self._brake_point(A["v"], wb, int(c.apex))   # + = this lap braked earlier
            else:
                brake_diff = 0                                # braking zone is in the previous lap
            apex_drop = float(np.max(R["v"][a:b] - A["v"][a:b]))   # biggest speed shortfall
            turn = f"turn {words(c.number)}"
            text = None
            if outside > self.wide_m:               # worth knowing even if it cost nothing
                text = f"Wide at {turn}, cost you {say_gap(lost)}." if lost > self.min_loss \
                    else f"Ran wide at {turn}."
            elif brake_diff > self.early_brake_m and lost > self.min_loss:
                text = f"Braked early into {turn}, {say_gap(lost)} lost."
            elif apex_drop > self.slow_apex_kmh and lost > self.min_loss:
                text = f"{turn.capitalize()}, carried {words(round(apex_drop * f))} {unit} less, {say_gap(lost)} lost."
            elif -brake_diff > self.early_brake_m * 0.8 and lost < -0.15 and "good" in self.on:
                found.append((-1, Call(lap.t_start + float(A["t"][i1]) + 0.3,
                                       f"Good braking into {turn}, picked up {say_gap(lost)}.",
                                       "corner", 3, lap.number, c.number)))
                continue
            if text and "corner" in self.on:
                found.append((max(lost, 0.001), Call(lap.t_start + float(A["t"][i1]) + 0.3, text, "corner", 2,
                                         lap.number, c.number)))
        # only the costliest few mistakes per lap, plus at most one good-news call
        bad = sorted([f for f in found if f[0] > 0], key=lambda f: -f[0])[: self.max_corner_calls]
        good = [f for f in found if f[0] < 0][:1]
        return [c for _, c in bad + good]


# ---------------------------------------------------------------- speech + audio

def tts_backend() -> str | None:
    if shutil.which("say"):
        return "say"
    if shutil.which("ffmpeg"):
        filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True,
                                 text=True).stdout
        if " flite " in filters:
            return "flite"
    return None


def list_voices() -> list[str]:
    if not shutil.which("say"):
        return []
    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    voices = []
    for line in out.split("\n"):
        parts = line.split("#")[0].split()
        # "Daniel   en_GB" or "Eddy (English (UK))  en_GB": the locale is the last column
        if len(parts) >= 2 and parts[-1].lower().startswith("en"):
            voices.append(" ".join(parts[:-1]))
    return voices


def default_voice() -> str | None:
    vs = list_voices()
    for pref in ("Daniel", "Alex", "Tom", "Fred"):
        if pref in vs:
            return pref
    return vs[0] if vs else None


def synthesize(text: str, voice: str | None = None, rate: int = 190) -> np.ndarray:
    """Speech as float32 mono at 48 kHz."""
    backend = tts_backend()
    if backend is None:
        raise RuntimeError("no speech engine: on macOS `say` is built in")
    with tempfile.TemporaryDirectory() as td:
        if backend == "say":
            aiff = Path(td) / "v.aiff"
            cmd = ["say", "-r", str(rate), "-o", str(aiff)]
            if voice:
                cmd += ["-v", voice]
            subprocess.run(cmd + [text], check=True, capture_output=True)
            src = ["-i", str(aiff)]
        else:
            tf = Path(td) / "t.txt"
            tf.write_text(text)
            src = ["-f", "lavfi", "-i", f"flite=textfile={tf}:voice=kal"]
        raw = subprocess.run(["ffmpeg", "-v", "error", *src, "-ac", "1", "-ar", str(RATE),
                              "-f", "f32le", "-"], capture_output=True, check=True).stdout
    a = np.frombuffer(raw, np.float32).copy()
    nz = np.flatnonzero(np.abs(a) > 0.01)                    # trim silence
    if len(nz):
        a = a[max(nz[0] - 480, 0): nz[-1] + 2400]
    return a


def _band(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Crude band-pass via FFT, good enough for a radio voice."""
    n = len(x)
    if n == 0:
        return x
    F = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1 / RATE)
    F[(f < lo) | (f > hi)] = 0
    return np.fft.irfft(F, n).astype(np.float32)


def radioize(voice: np.ndarray, seed: int = 0) -> np.ndarray:
    """Team-radio sound: narrow band, a bit of drive, squelch before and after."""
    rng = np.random.default_rng(seed)
    v = _band(voice, 320, 3300)
    peak = np.max(np.abs(v)) or 1.0
    v = np.tanh(v / peak * 2.2) / np.tanh(2.2) * 0.7            # gentle saturation
    hiss = _band(rng.normal(0, 1, len(v)).astype(np.float32), 500, 3800)
    v += hiss / (np.max(np.abs(hiss)) or 1) * 0.025
    def burst(ms, decay):
        n = int(RATE * ms / 1000)
        nz = _band(rng.normal(0, 1, n).astype(np.float32), 800, 4500)
        env = np.exp(-np.linspace(0, decay, n))
        return (nz / (np.max(np.abs(nz)) or 1) * 0.22 * env).astype(np.float32)
    click = np.zeros(int(RATE * 0.012), np.float32)
    click[: int(RATE * 0.004)] = 0.35
    gap = np.zeros(int(RATE * 0.06), np.float32)
    return np.concatenate([click, burst(110, 2.5), gap, v.astype(np.float32), gap, burst(160, 4)])


def build_track(calls: list[Call], t0: float, t1: float, voice: str | None = None,
                rate: int = 190, progress=None) -> tuple[np.ndarray, list[Call]]:
    """Synthesise, schedule (no overlaps) and place calls between telemetry
    times t0..t1. Returns the audio and the calls actually placed."""
    cand = [c for c in calls if t0 - 1 <= c.t <= t1]
    for i, c in enumerate(cand):
        c.audio = radioize(synthesize(c.text, voice, rate), seed=i)
        c.duration = len(c.audio) / RATE
        if progress:
            progress(i + 1, len(cand))
    placed: list[Call] = []
    gap = 0.4
    for prio in (1, 2, 3):                                  # important calls get first pick of time
        for c in sorted([c for c in cand if c.priority == prio], key=lambda c: c.t):
            start = max(c.t, t0)
            limit = c.t + {"lap": 6.0, "sector": 3.0, "corner": 5.0, "topspeed": 3.0,
                           "consistency": 12.0, "racepos": 4.0, "raceline": 7.0,
                           "racegap": 7.0}.get(c.kind, 4.0)
            moved = True
            while moved:
                moved = False
                for p in placed:
                    if start < p.start + p.duration + gap and p.start < start + c.duration + gap:
                        start = p.start + p.duration + gap
                        moved = True
            if start <= limit and start + c.duration <= t1 + 2:
                c.start = start
                placed.append(c)
    placed.sort(key=lambda c: c.start)
    n = int((t1 - t0) * RATE) + 1
    track = np.zeros(n, np.float32)
    for c in placed:
        i = int((c.start - t0) * RATE)
        seg = c.audio[: max(0, min(len(c.audio), n - i))]
        track[i: i + len(seg)] += seg
    return np.clip(track, -1, 1), placed


def write_wav(path: str | Path, audio: np.ndarray) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
