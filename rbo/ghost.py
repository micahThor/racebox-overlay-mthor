"""Ghost lap: your best lap's footage in a picture-in-picture window,
matched to where you are on track rather than to the clock.

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
from dataclasses import dataclass

import numpy as np

from .timing import Timing


def choose_ref(timing: Timing, offset: float, duration: float) -> int | None:
    """The fastest lap that's entirely inside the video (its footage is needed)."""
    best = None
    for i, lap in enumerate(timing.laps):
        v0, v1 = lap.t_start - offset, lap.t_end - offset
        if v0 >= 0 and v1 <= duration and (best is None or lap.time < timing.laps[best].time):
            best = i
    return best


@dataclass
class GhostState:
    active: bool
    ref_vt: float = 0.0       # video time of the best-lap frame to show
    gap_m: float = 0.0        # + = the ghost is ahead
    ref_number: int = 0
    ref_time: float = 0.0


def ghost_state(timing: Timing, ref: int | None, offset: float, duration: float,
                t: float) -> GhostState:
    if ref is None:
        return GhostState(False)
    k = timing.lap_index_at(t)
    if k is None or k == ref:
        return GhostState(False)
    lap, best = timing.laps[k], timing.laps[ref]
    el_k, d_k = timing._lap_profiles[k]
    el_r, d_r = timing._lap_profiles[ref]
    e = t - lap.t_start
    where = float(np.interp(e, el_k, d_k)) / max(d_k[-1], 1.0) * d_r[-1]   # metres, on the best lap
    e_ref = float(np.interp(where, d_r, el_r))
    ref_vt = best.t_start + e_ref - offset
    if not 0 <= ref_vt <= duration:
        return GhostState(False)
    ghost_at = float(np.interp(e, el_r, d_r))       # where the ghost is after the same time
    return GhostState(True, ref_vt, ghost_at - where, best.number, best.time)


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
