"""Lap, sector and live-delta computation from GPS telemetry.

Lap boundaries come from (in order of preference):
  1. a track file's start/finish point
  2. a VBO [laptiming] start line
  3. the RaceBox "Lap" column (lap number changes), refined to the exact
     moment the car crossed the line by interpolating between GPS samples.

Sectors come from a track file's sector points, or are auto-split into equal
distances on the fastest lap (--sectors N). Auto sectors won't match the
RaceBox app's sector definitions; put the real ones in a track file for that.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .telemetry import Telemetry, enu

HOLD_SECONDS = 4.0   # how long a finished lap time stays on screen


@dataclass
class Gate:
    x: float
    y: float
    dx: float          # unit vector, direction of travel
    dy: float
    half_width: float
    lat: float = 0.0
    lon: float = 0.0


@dataclass
class Lap:
    number: int
    t_start: float
    t_end: float
    splits: list[float] | None = None   # absolute times of each sector gate

    @property
    def time(self) -> float:
        return self.t_end - self.t_start

    @property
    def sectors(self) -> list[float] | None:
        if self.splits is None:
            return None
        pts = [self.t_start, *self.splits, self.t_end]
        return [b - a for a, b in zip(pts, pts[1:])]


@dataclass
class SectorCell:
    label: str
    time: float | None
    status: str        # "done", "running", "pending"
    color: str         # "purple", "green", "yellow", "white", "dim"


@dataclass
class HudState:
    phase: str                     # "none", "pre", "lap", "post"
    lap_number: int | None = None
    lap_total: int = 0
    elapsed: float | None = None   # running lap time
    finished: Lap | None = None    # lap just completed (held on screen)
    finished_color: str = "white"
    last_lap: float | None = None
    best_lap: float | None = None
    delta: float | None = None
    sectors: list[SectorCell] = field(default_factory=list)


class Timing:
    def __init__(self, tel: Telemetry, track: dict | None = None, n_sectors: int = 3,
                 gate_width: float | None = None, min_lap: float = 15.0,
                 delta_ref: str = "best-so-far"):
        self.tel = tel
        self.track = track or {}
        self.delta_ref = delta_ref
        self.min_lap = min_lap
        self.half_width = float(gate_width or self.track.get("gate_width_m", 15.0))

        self.lat0, self.lon0 = float(np.mean(tel.lat)), float(np.mean(tel.lon))
        self.x, self.y = enu(tel.lat, tel.lon, self.lat0, self.lon0)
        kk = 5                                          # smooth out GPS jitter first
        sx = np.convolve(np.pad(self.x, kk // 2, mode="edge"), np.ones(kk) / kk, mode="valid")
        sy = np.convolve(np.pad(self.y, kk // 2, mode="edge"), np.ones(kk) / kk, mode="valid")
        step = np.hypot(np.diff(sx), np.diff(sy))
        self.dist = np.concatenate([[0.0], np.cumsum(step)])

        self.start_gate: Gate | None = self._start_gate()
        self.laps: list[Lap] = self._find_laps()
        self.sector_gates: list[Gate] = self._sector_gates(n_sectors)
        self._assign_splits()
        self._lap_profiles = [self._profile(l) for l in self.laps]

    # ------------------------------------------------------------ gates

    def _gate_at(self, lat: float, lon: float, heading_deg: float | None = None) -> Gate:
        gx, gy = enu(lat, lon, self.lat0, self.lon0)
        gx, gy = float(gx), float(gy)
        if heading_deg is not None:
            h = np.radians(heading_deg)
            dx, dy = np.sin(h), np.cos(h)
        else:
            moving = self.tel.speed_kmh > 10
            d2 = (self.x - gx) ** 2 + (self.y - gy) ** 2
            d2[~moving] = np.inf
            i = int(np.argmin(d2))
            a, b = max(i - 2, 0), min(i + 2, len(self.x) - 1)
            dx, dy = self.x[b] - self.x[a], self.y[b] - self.y[a]
            n = np.hypot(dx, dy) or 1.0
            dx, dy = dx / n, dy / n
        return Gate(gx, gy, float(dx), float(dy), self.half_width, lat, lon)

    def _start_gate(self) -> Gate | None:
        tr = self.track
        if "start_finish" in tr:
            lat, lon = tr["start_finish"]
            return self._gate_at(lat, lon, tr.get("start_heading"))
        if self.tel.start_line:
            (la1, lo1), (la2, lo2) = self.tel.start_line
            g = self._gate_at((la1 + la2) / 2, (lo1 + lo2) / 2)
            x1, y1 = enu(la1, lo1, self.lat0, self.lon0)
            x2, y2 = enu(la2, lo2, self.lat0, self.lon0)
            g.half_width = max(float(np.hypot(x2 - x1, y2 - y1)) / 2, 5.0)
            return g
        lap = self.tel.lap
        if lap is not None:
            ch = np.flatnonzero((np.diff(lap) != 0) & (lap[1:] >= 1))
            if len(ch):
                i = ch[0]
                return self._gate_at((self.tel.lat[i] + self.tel.lat[i + 1]) / 2,
                                     (self.tel.lon[i] + self.tel.lon[i + 1]) / 2)
        # Last resort: no lap info at all. Put a line where the car was halfway
        # through the session; lap *times* are still right, but laps start
        # there instead of at the real start/finish.
        moving = np.flatnonzero(self.tel.speed_kmh > 30)
        if len(moving) > 100:
            i = int(moving[len(moving) // 2])
            g = self._gate_at(float(self.tel.lat[i]), float(self.tel.lon[i]))
            if len(self.crossings(g)) >= 3:
                self.auto_line = True
                return g
        return None

    auto_line = False

    def crossings(self, g: Gate, half_width: float | None = None) -> np.ndarray:
        hw = half_width or g.half_width
        a = (self.x - g.x) * g.dx + (self.y - g.y) * g.dy
        idx = np.flatnonzero((a[:-1] < 0) & (a[1:] >= 0))
        if not len(idx):
            return np.array([])
        s = -a[idx] / (a[idx + 1] - a[idx])
        px = self.x[idx] + s * (self.x[idx + 1] - self.x[idx])
        py = self.y[idx] + s * (self.y[idx + 1] - self.y[idx])
        lateral = (px - g.x) * -g.dy + (py - g.y) * g.dx
        t = self.tel.t
        dt = t[idx + 1] - t[idx]
        ok = (np.abs(lateral) <= hw) & (dt < 2.0)
        return (t[idx] + s * dt)[ok]

    # ------------------------------------------------------------ laps

    def _find_laps(self) -> list[Lap]:
        lap = self.tel.lap
        if lap is not None and "start_finish" not in self.track and (lap >= 1).any():
            return self._laps_from_logger(lap)
        if self.start_gate is None:
            return []
        times = []
        for c in self.crossings(self.start_gate):
            if not times or c - times[-1] >= self.min_lap:
                times.append(float(c))
        return [Lap(i + 1, a, b) for i, (a, b) in enumerate(zip(times, times[1:]))]

    def _laps_from_logger(self, lap: np.ndarray) -> list[Lap]:
        t = self.tel.t
        edges = np.flatnonzero(np.diff(lap) != 0) + 1          # first sample of each run
        starts = np.concatenate([[0], edges])
        ends = np.concatenate([edges, [len(t)]])
        cross = self.crossings(self.start_gate, self.half_width * 2) if self.start_gate else np.array([])

        def refine(i):  # boundary between sample i-1 and i
            guess = (t[i - 1] + t[i]) / 2
            near = cross[np.abs(cross - guess) < 3.0]
            return float(near[np.argmin(np.abs(near - guess))]) if len(near) else float(guess)

        laps = []
        for s, e in zip(starts, ends):
            if lap[s] < 1 or s == 0 or e >= len(t):
                continue          # out/in laps, or a lap cut off by the recording
            laps.append(Lap(len(laps) + 1, refine(s), refine(e)))
        return [l for l in laps if l.time >= self.min_lap]

    # ------------------------------------------------------------ sectors

    def _sector_gates(self, n: int) -> list[Gate]:
        if "sectors" in self.track:
            return [self._gate_at(la, lo) for la, lo in self.track["sectors"]]
        if n < 2 or not self.laps:
            return []
        best = min(self.laps, key=lambda l: l.time)
        d0, d1 = np.interp([best.t_start, best.t_end], self.tel.t, self.dist)
        gates = []
        for k in range(1, n):
            tk = np.interp(d0 + (d1 - d0) * k / n, self.dist, self.tel.t)
            gates.append(self._gate_at(float(np.interp(tk, self.tel.t, self.tel.lat)),
                                       float(np.interp(tk, self.tel.t, self.tel.lon))))
        return gates

    def _assign_splits(self) -> None:
        if not self.sector_gates:
            return
        cross = [self.crossings(g) for g in self.sector_gates]
        for lap in self.laps:
            prev, splits = lap.t_start, []
            for c in cross:
                cand = c[(c > prev) & (c < lap.t_end)]
                if not len(cand):
                    splits = None
                    break
                prev = float(cand[0])
                splits.append(prev)
            lap.splits = splits

    # ------------------------------------------------------------ delta

    def _profile(self, lap: Lap):
        t = self.tel.t
        m = (t > lap.t_start) & (t < lap.t_end)
        d_s, d_e = np.interp([lap.t_start, lap.t_end], t, self.dist)
        el = np.concatenate([[0.0], t[m] - lap.t_start, [lap.time]])
        d = np.concatenate([[0.0], self.dist[m] - d_s, [d_e - d_s]])
        return el, d

    def _ref_index(self, k: int) -> int | None:
        if self.delta_ref == "session-best":
            return min(range(len(self.laps)), key=lambda j: self.laps[j].time)
        done = range(k)
        return min(done, key=lambda j: self.laps[j].time) if k > 0 else None

    def _delta(self, k: int, t: float) -> float | None:
        r = self._ref_index(k)
        if r is None:
            return None
        el_k, d_k = self._lap_profiles[k]
        el_r, d_r = self._lap_profiles[r]
        e = t - self.laps[k].t_start
        d = np.interp(e, el_k, d_k) / max(d_k[-1], 1.0) * d_r[-1]   # normalise lap length
        return float(e - np.interp(d, d_r, el_r))

    # ------------------------------------------------------------ state

    def lap_index_at(self, t: float) -> int | None:
        for i, l in enumerate(self.laps):
            if l.t_start <= t < l.t_end:
                return i
        return None

    def _sector_color(self, k: int, i: int, val: float) -> str:
        prev = [l.sectors[i] for l in self.laps[:k] if l.sectors]
        if not prev or val <= min(prev):
            return "purple"
        r = self._ref_index(k)
        if r is not None and self.laps[r].sectors and val < self.laps[r].sectors[i]:
            return "green"
        return "yellow"

    def _cells(self, k: int, t: float) -> list[SectorCell]:
        n = len(self.sector_gates) + 1 if self.sector_gates else 0
        if n == 0:
            return []
        lap = self.laps[k]
        secs = lap.sectors
        bounds = [lap.t_start, *(lap.splits or []), lap.t_end]
        cells = []
        for i in range(n):
            label = f"S{i + 1}"
            if secs is None:
                cells.append(SectorCell(label, None, "pending", "dim"))
            elif t >= bounds[i + 1]:
                cells.append(SectorCell(label, secs[i], "done", self._sector_color(k, i, secs[i])))
            elif t >= bounds[i]:
                cells.append(SectorCell(label, t - bounds[i], "running", "white"))
            else:
                cells.append(SectorCell(label, None, "pending", "dim"))
        return cells

    def state(self, t: float) -> HudState:
        if not self.laps:
            return HudState("none")
        laps = self.laps
        done = [l for l in laps if l.t_end <= t]
        st = HudState("lap", lap_total=len(laps))
        if done:
            st.last_lap = done[-1].time
            st.best_lap = min(l.time for l in done)

        k = self.lap_index_at(t)
        recent = done[-1] if done and t - done[-1].t_end < HOLD_SECONDS else None
        if recent is not None:
            j = laps.index(recent)
            st.finished = recent
            prev = [l.time for l in laps[:j]]
            st.finished_color = "purple" if not prev or recent.time <= min(prev) else (
                "green" if recent.time < prev[-1] else "white")
            st.sectors = self._cells(j, t)
            st.delta = self._delta(j, recent.t_end - 1e-6)

        if k is not None:
            st.lap_number = laps[k].number
            st.elapsed = t - laps[k].t_start
            if recent is None:
                st.sectors = self._cells(k, t)
                st.delta = self._delta(k, t)
        elif t < laps[0].t_start:
            st.phase = "pre"
        else:
            st.phase = "post"
        return st

    # ------------------------------------------------------------ export

    def track_dict(self) -> dict:
        d = {}
        if self.start_gate:
            g = self.start_gate
            d["start_finish"] = [round(float(g.lat), 7), round(float(g.lon), 7)]
            d["start_heading"] = round(float(np.degrees(np.arctan2(g.dx, g.dy))) % 360, 1)
        if self.sector_gates:
            d["sectors"] = [[round(float(g.lat), 7), round(float(g.lon), 7)] for g in self.sector_gates]
        d["gate_width_m"] = self.half_width
        return d


def load_track(path: str | Path | None) -> dict | None:
    if not path:
        return None
    return json.loads(Path(path).read_text())


def fmt_time(s: float | None, plus: bool = False, digits: int = 2) -> str:
    if s is None:
        return "--:--.--"
    sign = ""
    if plus:
        sign = "+" if s >= 0 else "-"
        s = abs(s)
    elif s < 0:
        sign, s = "-", abs(s)
    s = round(s, digits)
    m, sec = divmod(s, 60)
    body = f"{sec:0{3 + digits}.{digits}f}" if m else f"{sec:.{digits}f}"
    return f"{sign}{int(m)}:{body}" if m else f"{sign}{body}"
