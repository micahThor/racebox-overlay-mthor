"""Ghost lap: a lap's footage in a picture-in-picture window, matched to
where you are on track rather than to the clock. The ghost is your fastest
lap in this video, or any lap from another RaceBox session and its video at
the same track (another day, or a friend's lap).

At any moment in a lap, the app knows how far round the lap you are. It
finds the moment your best lap was at the same point and shows that frame,
so the ghost is always in the same corner as you. The gap label says how far
ahead or behind the ghost would be if you'd both started the lap together.

The best lap's frames are always needed in forward order (as you move on
round the lap, so does the ghost), so they're streamed from one ffmpeg
decoder instead of seeking for every frame.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .timing import Timing


class GhostError(Exception):
    pass


@dataclass
class GhostRef:
    """The lap the ghost replays: its distance-vs-time profile, where its
    footage is, and how to label it."""
    el: np.ndarray                 # seconds into the lap
    d: np.ndarray                  # metres from the line
    t_start: float                 # lap start, in that session's data clock
    offset: float                  # that session's data time at video t=0
    duration: float                # that video's length
    video: str
    fps: float
    number: int                    # lap number as that session counts it
    time: float                    # lap time
    v_end: float                   # m/s at the line, to keep the ghost going after it finishes
    label: str                     # "BEST LAP 2", "OCT 12 · LAP 4"
    index: int | None = None       # lap index in this session (own-video ghost), else None
    profile: dict | None = field(default=None, repr=False)   # crew chief: s, t, x, y, v (external)

    @property
    def external(self) -> bool:
        return self.index is None


def own_ref(timing: Timing, index: int, offset: float, duration: float, video: str, fps: float) -> GhostRef:
    lap = timing.laps[index]
    el, d = timing._lap_profiles[index]
    v_end = float(np.interp(lap.t_end, timing.tel.t, timing.tel.speed_kmh)) / 3.6
    return GhostRef(el, d, lap.t_start, offset, duration, video, float(fps), lap.number, lap.time, v_end,
                    f"BEST LAP {lap.number}", index)


def session_laps(session: str) -> list[dict]:
    """Laps in another RaceBox file, numbered the way that file numbers them."""
    from .telemetry import load
    from .timing import fmt_time
    tm = Timing(load(session))
    return [{"n": l.number, "time": l.time, "time_str": fmt_time(l.time)} for l in tm.laps]


def external_ref(main: Timing, session: str, video: str, lap_number: int,
                 offset: float | None = None) -> tuple[GhostRef, str]:
    """A lap from another session and its video as the ghost. offset: that
    video's sync (seconds into its data at video t=0); None = reuse the sync
    saved for that video in the editor, else auto-sync it. Returns the ghost
    and a note on how the video was synced."""
    from . import project, sync
    from . import video as vid
    from .telemetry import enu, load
    if not Path(session).expanduser().exists():
        raise GhostError(f"ghost: can't find the RaceBox file {session}")
    if not Path(video).expanduser().exists():
        raise GhostError(f"ghost: can't find the video {video}")
    session, video = str(Path(session).expanduser()), str(Path(video).expanduser())
    gtel = load(session)
    gtm = Timing(gtel)
    lap = next((l for l in gtm.laps if l.number == int(lap_number)), None)
    if lap is None:
        raise GhostError(f"ghost: no lap {lap_number} in {Path(session).name}; laps: "
                         + ", ".join(str(l.number) for l in gtm.laps))
    if main.start_gate is None:
        raise GhostError("ghost: this session has no start/finish line to line the ghost lap up with")

    # Cut the ghost lap at *this* session's start/finish line, so both laps are
    # measured from the same spot even if the two RaceBoxes put the line apart.
    tr = main.track_dict()
    line = Timing(gtel, track={"start_finish": tr["start_finish"], "start_heading": tr["start_heading"],
                               "gate_width_m": 25})
    cross = line.crossings(line.start_gate)
    near = lambda t0: cross[np.argmin(np.abs(cross - t0))] if len(cross) and \
        np.min(np.abs(cross - t0)) < 8 else None
    a, b = near(lap.t_start), near(lap.t_end)
    if a is None or b is None or b - a < main.min_lap:
        raise GhostError(f"ghost: lap {lap_number} of {Path(session).name} doesn't cross this session's "
                         "start/finish line. Is it the same track, ridden the same way round?")
    t, dist = gtel.t, gtm.dist
    m = (t > a) & (t < b)
    d0, d1 = np.interp([a, b], t, dist)
    el = np.concatenate([[0.0], t[m] - a, [b - a]])
    d = np.concatenate([[0.0], dist[m] - d0, [d1 - d0]])

    # same track? the ghost's line should lie on top of this session's best lap
    gx, gy = enu(gtel.lat, gtel.lon, main.lat0, main.lon0)
    if main.laps:
        best = min(main.laps, key=lambda l: l.time)
        bm = (main.tel.t >= best.t_start) & (main.tel.t <= best.t_end)
        bx, by = main.x[bm][::3], main.y[bm][::3]
        px, py = gx[m][::10], gy[m][::10]
        if len(bx) and len(px):
            dmin = np.sqrt(((px[:, None] - bx[None, :]) ** 2 + (py[:, None] - by[None, :]) ** 2).min(axis=1))
            if np.median(dmin) > 15:
                raise GhostError(f"ghost: lap {lap_number} of {Path(session).name} doesn't follow this track "
                                 f"(typically {np.median(dmin):.0f} m away from your line)")
    profile = {"s": d[1:-1], "t": el[1:-1], "x": gx[m], "y": gy[m], "v": gtel.speed_kmh[m]}

    info = vid.probe(video)
    how = "your setting"
    if offset is None:
        proj = project.load(video)
        same = proj.get("session") and Path(proj["session"]).resolve() == Path(session).resolve()
        if proj.get("offset") is not None and (same or not proj.get("session")):
            offset, how = float(proj["offset"]), "saved in the editor for that video"
        else:
            from .cli import _auto_offset
            prior = None
            if info.get("created") is not None:
                off, note = _auto_offset(info["created"], gtel, info["duration"])
                prior = None if "doesn't match" in note else off
            try:
                r = sync.auto_sync(video, gtel, prior=prior)
                offset, how = float(r.offset - gtel.t[0]), f"auto-sync ({r.method})"
            except sync.SyncError as e:
                raise GhostError(f"ghost: couldn't sync {Path(video).name} to its RaceBox file ({e}). "
                                 "Open it in the editor once to set its sync.") from e
    off_abs = gtel.t[0] + float(offset)
    if a - off_abs < 0 or b - off_abs > info["duration"]:
        raise GhostError(f"ghost: lap {lap_number} isn't inside {Path(video).name} with that sync "
                         f"(it would start at {a - off_abs:.1f} s). Check that video's sync: open it in the editor, or give --ghost-offset")
    day = datetime.fromtimestamp(a, timezone.utc)
    same_day = abs(a - main.tel.t[0]) < 12 * 3600
    label = f"LAP {lap.number}" if same_day else f"{day.strftime('%b').upper()} {day.day} · LAP {lap.number}"
    v_end = float(np.interp(b, t, gtel.speed_kmh)) / 3.6
    ref = GhostRef(el, d, float(a), off_abs, float(info["duration"]), video, float(info["fps"]),
                   lap.number, float(lap.time), v_end, label, None, profile)
    ref.offset_rel = float(offset)            # what to save back to the project
    return ref, how


def choose_ref(timing: Timing, offset: float, duration: float, avoid=None) -> int | None:
    """The fastest lap that's entirely inside the video (its footage is needed).
    avoid: lap indexes to skip if any other lap qualifies (race mode: laps
    spent in traffic)."""
    def pick(skip):
        best = None
        for i, lap in enumerate(timing.laps):
            v0, v1 = lap.t_start - offset, lap.t_end - offset
            if i not in skip and v0 >= 0 and v1 <= duration and \
                    (best is None or lap.time < timing.laps[best].time):
                best = i
        return best
    return pick(set(avoid or ())) if pick(set(avoid or ())) is not None else pick(set())


@dataclass
class GhostState:
    active: bool
    ref_vt: float = 0.0       # time in the ghost's video of the frame to show
    gap_m: float = 0.0        # + = the ghost is ahead
    ref_number: int = 0
    ref_time: float = 0.0
    label: str = ""


def ghost_state(timing: Timing, ref: GhostRef | None, t: float) -> GhostState:
    if ref is None:
        return GhostState(False)
    k = timing.lap_index_at(t)
    if k is None or k == ref.index:
        return GhostState(False)
    lap = timing.laps[k]
    el_k, d_k = timing._lap_profiles[k]
    el_r, d_r = ref.el, ref.d
    e = t - lap.t_start
    where = float(np.interp(e, el_k, d_k)) / max(d_k[-1], 1.0) * d_r[-1]   # metres, on the ghost's lap
    e_ref = float(np.interp(where, d_r, el_r))
    ref_vt = ref.t_start + e_ref - ref.offset
    if not 0 <= ref_vt <= ref.duration:
        return GhostState(False)
    if e <= el_r[-1]:
        ghost_at = float(np.interp(e, el_r, d_r))   # where the ghost is after the same time
    else:
        ghost_at = d_r[-1] + (e - el_r[-1]) * ref.v_end
    return GhostState(True, ref_vt, ghost_at - where, ref.number, ref.time, ref.label)


class GhostFeed:
    """Frames of the source video, read forward on demand, scaled to w x h."""

    def __init__(self, video: str, fps: float, w: int, h: int):
        self.video, self.fps, self.w, self.h = video, float(fps), int(w), int(h)
        self.proc = None
        self.t_next = None        # video time of the next frame the decoder will give
        self.frame = None

    def _start(self, vt: float):
        self.close()
        self.proc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-hwaccel", "auto", "-ss", f"{max(vt, 0):.3f}", "-i", self.video,
             "-an", "-vf", f"scale={self.w}:{self.h}:flags=bilinear", "-pix_fmt", "rgb24",
             "-f", "rawvideo", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.t_next = max(vt, 0)
        self.frame = None

    def get(self, vt: float) -> np.ndarray | None:
        step = 1 / self.fps
        if self.proc is None or vt < self.t_next - 2 * step or vt > self.t_next + 2.0:
            self._start(vt)
        size = self.w * self.h * 3
        while self.frame is None or self.t_next <= vt + step / 2:
            buf = self.proc.stdout.read(size)
            if len(buf) < size:
                break
            self.frame = np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3)
            self.t_next += step
        return self.frame

    def close(self):
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait()
            self.proc = None
