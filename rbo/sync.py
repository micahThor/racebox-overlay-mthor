"""Automatic video <-> RaceBox sync by matching how the car rotates.

Every corner shows up twice: as yaw rate in the RaceBox data, and as rotation
seen by the camera. Sliding one signal along the other and finding where they
line up best gives the offset, typically to within a frame or two.

Two sources for the camera side:

* gyro  - the IMU log Insta360 cameras append to the original .insv file.
          Precise, but only valid for an export that starts where the .insv
          starts (or pass how far in it starts with --export-start).
* video - sideways image motion measured from the exported MP4 itself.
          Works on any clip, trimmed or not, from any camera. Slower (it has
          to decode the video) and a bit noisier.

Insta360 trailer layout (from ExifTool's QuickTimeStream.pl): the file ends
with a 78-byte tail whose last 32 bytes are an ASCII magic string; the
trailer length is a uint32 at offset 38. Records run backwards from the
tail: each is [data][id:uint16][len:uint32], little-endian. Record 0x300 is
IMU (uint64 ms + accel xyz + gyro xyz, as 6 doubles or 6 offset uint16s),
0x600 is per-frame video timestamps, 0x400 per-frame exposure (+ timestamp).
"""
from __future__ import annotations

import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .telemetry import Telemetry, enu

MAGIC = b"8db42d694ccc418790edff439fe026bf"
GRID_HZ = 20.0


class SyncError(Exception):
    pass


@dataclass
class SyncResult:
    offset: float            # telemetry_time = video_time + offset
    score: float             # correlation at the best match, 0..1
    ratio: float             # best score / best competing score; ~1 means ambiguous
    method: str
    alternatives: list[tuple[float, float]] = field(default_factory=list)  # (offset, score)

    @property
    def confident(self) -> bool:
        return self.score >= 0.5 and self.ratio >= 1.15


# ---------------------------------------------------------------- insv gyro

def has_trailer(path: str | Path) -> bool:
    try:
        with open(path, "rb") as f:
            f.seek(-32, 2)
            return f.read(32) == MAGIC
    except OSError:
        return False


def read_insv_trailer(path: str | Path) -> dict[int, list[bytes]]:
    """Sensor records from the Insta360 trailer of an .insv or camera .mp4.

    Older cameras chain records back to back from the end of the file. Newer
    ones (Ace Pro, GO 3 era) add a directory record (id 0) listing every
    record's size and offset from the start of the trailer; when one turns
    up, follow it instead."""
    recs: dict[int, list[bytes]] = {}
    with open(path, "rb") as f:
        f.seek(0, 2)
        end = f.tell()
        if end < 78:
            raise SyncError(f"{path} is too small to be an Insta360 file")
        f.seek(end - 78)
        tail = f.read(78)
        if tail[-32:] != MAGIC:
            raise SyncError(f"{Path(path).name} has no Insta360 sensor data. Use the original "
                            "file from the camera's SD card, not one exported from the app or "
                            "Studio (or drop --gyro to measure motion from the video instead)")
        tlen = struct.unpack_from("<I", tail, 38)[0]
        start = end - tlen
        pos, hdr = end - 78, tail[:6]
        directory = None
        while True:
            rid, ln = struct.unpack("<HI", hdr)
            if ln == 0 or pos - ln < start:
                break
            f.seek(pos - ln)
            data = f.read(ln)
            if rid == 0:
                directory = data
                break
            recs.setdefault(rid, []).append(data)
            pos = pos - ln - 6
            if pos < start:
                break
            f.seek(pos)
            hdr = f.read(6)
        if directory:
            for i in range(0, len(directory) - 9, 10):
                rid, siz, off = struct.unpack_from("<HII", directory, i)
                if rid and siz and off + siz < tlen:
                    f.seek(start + off)
                    data = f.read(siz)
                    if data not in recs.get(rid, []):
                        recs.setdefault(rid, []).append(data)
    return recs


def _plausible_clock(chunk: bytes, size: int) -> float:
    """Fraction of steps where the leading uint64 ms counter advances by a
    sensible IMU interval if records are `size` bytes long."""
    n = min(len(chunk) // size, 2000)
    if n < 3:
        return 0.0
    ms = np.array([struct.unpack_from("<Q", chunk, i * size)[0] for i in range(n)], dtype=float)
    step = np.diff(ms)
    return float(np.mean((step >= 0) & (step <= 50)))


def _imu_rows(chunk: bytes) -> np.ndarray:
    n = len(chunk)
    if n % 56 == 0 and n % 20:
        size = 56
    elif n % 20 == 0 and n % 56:
        size = 20
    else:
        size = 56 if _plausible_clock(chunk, 56) >= _plausible_clock(chunk, 20) else 20
    if size == 56:
        a = np.frombuffer(chunk[: n - n % 56], dtype=[("ms", "<u8"), ("v", "<f8", 6)])
        return np.column_stack([a["ms"].astype(float), a["v"]])
    a = np.frombuffer(chunk[: n - n % 20], dtype=[("ms", "<u8"), ("v", "<u2", 6)])
    return np.column_stack([a["ms"].astype(float), (a["v"].astype(float) - 0x8000) / 1000])


def insv_yaw_rate(path: str | Path, export_start: float = 0.0):
    """Camera yaw rate vs. video time, from the .insv IMU log.

    The camera can be mounted at any angle, so 'yaw' is rotation about the
    average gravity direction rather than about any one camera axis."""
    recs = read_insv_trailer(path)
    if 0x300 not in recs:
        raise SyncError(f"{Path(path).name} has an Insta360 trailer but no gyro records")
    rows = np.concatenate([_imu_rows(c) for c in recs[0x300]])
    rows = rows[np.argsort(rows[:, 0], kind="stable")]
    rows = rows[np.concatenate([[True], np.diff(rows[:, 0]) > 0])]

    frame0 = None
    for rid in (0x600, 0x400):
        if rid in recs:
            stamps = [struct.unpack_from("<Q", c, 0)[0] for c in recs[rid] if len(c) >= 8]
            if stamps:
                frame0 = float(min(stamps))
                break
    if frame0 is None:
        frame0 = rows[0, 0]

    t = (rows[:, 0] - frame0) / 1000.0 - export_start
    acc, gyr = rows[:, 1:4], rows[:, 4:7]
    up = acc.mean(axis=0)
    norm = np.linalg.norm(up)
    if norm < 1e-6:
        raise SyncError("couldn't find the gravity direction in the IMU data")
    return t, gyr @ (up / norm)


# ---------------------------------------------------------------- video motion

def video_yaw_proxy(path: str, max_seconds: float = 420.0, width: int = 192,
                    progress: bool = True):
    """Horizontal image shift per second, measured by phase correlation on a
    band around the horizon. When the car turns, the whole scene slides
    sideways; that slide tracks yaw rate."""
    from .video import need, probe
    info = probe(path)
    h = int(round(width * info["height"] / info["width"] / 2) * 2)
    cmd = [need("ffmpeg"), "-v", "error", "-hwaccel", "auto", "-t", f"{max_seconds:.1f}",
           "-i", path, "-an", "-vf", f"fps={GRID_HZ:g},scale={width}:{h},format=gray",
           "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    y0, y1 = int(h * 0.25), int(h * 0.62)
    win = np.outer(np.hanning(y1 - y0), np.hanning(width))
    fsize = width * h
    prev, shifts = None, []
    total = int(min(max_seconds, info["duration"]) * GRID_HZ)
    while True:
        buf = proc.stdout.read(fsize)
        if len(buf) < fsize:
            break
        band = np.frombuffer(buf, np.uint8).reshape(h, width)[y0:y1].astype(np.float32)
        band = (band - band.mean()) * win
        F = np.fft.rfft2(band)
        if prev is not None:
            shifts.append(_phase_shift_x(prev, F, width))
        prev = F
        if progress and len(shifts) % 200 == 0:
            print(f"\r  measuring video motion {len(shifts) / GRID_HZ:5.0f}s / {total / GRID_HZ:.0f}s",
                  end="", flush=True)
    proc.wait()
    if progress:
        print()
    if len(shifts) < GRID_HZ * 20:
        raise SyncError("video too short to measure motion (need at least ~20 s)")
    s = np.array(shifts) * GRID_HZ
    t = (np.arange(len(s)) + 0.5) / GRID_HZ     # shift happens between frames i and i+1
    return t, s


def _phase_shift_x(Fa, Fb, width: int) -> float:
    R = Fa * np.conj(Fb)
    R /= np.abs(R) + 1e-9
    r = np.fft.irfft2(R, s=(Fa.shape[0], width))
    prof = r.max(axis=0)                      # strongest response per x shift
    i = int(np.argmax(prof))
    l, c, rr = prof[i - 1], prof[i], prof[(i + 1) % width]
    den = l - 2 * c + rr
    sub = 0.5 * (l - rr) / den if abs(den) > 1e-12 else 0.0
    dx = i + sub
    return dx - width if dx > width / 2 else dx


# ---------------------------------------------------------------- data side

def data_yaw_rate(tel: Telemetry) -> np.ndarray:
    if tel.yaw_rate is not None and np.nanstd(tel.yaw_rate) > 0.5:
        yaw = tel.yaw_rate.astype(float)
    else:
        x, y = enu(tel.lat, tel.lon, tel.lat.mean(), tel.lon.mean())
        k = max(3, int(round(0.4 / max(np.median(np.diff(tel.t)), 1e-3))) | 1)   # ~0.4 s
        x = np.convolve(x, np.ones(k) / k, mode="same")
        y = np.convolve(y, np.ones(k) / k, mode="same")
        heading = np.unwrap(np.arctan2(np.gradient(x), np.gradient(y)))
        yaw = np.degrees(np.gradient(heading) / np.maximum(np.gradient(tel.t), 1e-3))
    yaw = np.where(tel.speed_kmh > 8, yaw, 0.0)
    return yaw


# ---------------------------------------------------------------- matching

def _prep(t, v, t0, t1):
    # average down to the grid rate first; plain interpolation of a 200+ Hz
    # gyro would alias its noise and shift the match by a frame or two
    dt = float(np.median(np.diff(t)))
    k = int(round(1 / GRID_HZ / dt)) if dt > 0 else 1
    if k > 1:
        v = np.convolve(v, np.ones(k) / k, mode="same")
    grid = np.arange(t0, t1, 1 / GRID_HZ)
    g = np.interp(grid, t, v)
    valid = (grid >= t[0]) & (grid <= t[-1])
    k = max(1, int(0.25 * GRID_HZ))
    g = np.convolve(g, np.ones(k) / k, mode="same")
    m, s = g[valid].mean(), g[valid].std() or 1.0
    g = np.clip((g - m) / s, -5, 5)
    g[~valid] = 0
    return g, valid.astype(float)


def _xcorr(a, b):
    """sum_i a[i] * b[i + k] for k = -(len(a)-1) .. len(b)-1"""
    n = len(a) + len(b)
    nfft = 1 << (n - 1).bit_length()
    r = np.fft.irfft(np.conj(np.fft.rfft(a, nfft)) * np.fft.rfft(b, nfft), nfft)
    return np.concatenate([r[nfft - (len(a) - 1):], r[: len(b)]])


def match(cam_t, cam_v, tel: Telemetry, method: str, prior: float | None = None,
          min_overlap: float = 60.0, prior_window: float = 30.0) -> SyncResult:
    """Find the offset where camera rotation best matches the data.

    Laps repeat, so there's a near-equal match one lap away in either
    direction. A rough `prior` (e.g. from the camera clock) picks the right
    one: only matches within `prior_window` seconds of it are considered."""
    c, mc = _prep(cam_t, cam_v, 0.0, float(cam_t[-1]))
    d, md = _prep(tel.t, data_yaw_rate(tel), float(tel.t[0]), float(tel.t[-1]))
    c *= mc

    N = _xcorr(mc, md)
    Scd = _xcorr(c, d)
    Sc = _xcorr(c, md)
    Sd = _xcorr(mc, d)
    Scc = _xcorr(c * c, md)
    Sdd = _xcorr(mc, d * d)
    with np.errstate(invalid="ignore", divide="ignore"):
        num = Scd - Sc * Sd / N
        den = np.sqrt(np.maximum(Scc - Sc ** 2 / N, 0) * np.maximum(Sdd - Sd ** 2 / N, 0))
        ncc = np.abs(num / den)
    need = min(min_overlap, 0.6 * mc.sum() / GRID_HZ) * GRID_HZ
    ncc[(N < need) | ~np.isfinite(ncc)] = 0
    # a match that only half-overlaps the data is weaker evidence
    ncc *= np.sqrt(np.clip(N / mc.sum(), 0, 1))

    lags = np.arange(-(len(c) - 1), len(d))
    offsets = float(tel.t[0]) + lags / GRID_HZ

    # local peaks, best first
    peaks = np.flatnonzero((ncc[1:-1] >= ncc[:-2]) & (ncc[1:-1] > ncc[2:])) + 1
    peaks = peaks[np.argsort(-ncc[peaks])]
    if not len(peaks) or ncc[peaks[0]] <= 0:
        raise SyncError("couldn't find any match between the video and the data")
    chosen = []
    for p in peaks:
        if all(abs(p - q) > 3 * GRID_HZ for q in chosen):
            chosen.append(p)
        if len(chosen) == 5:
            break

    best = chosen[0]
    used_prior = False
    if prior is not None:
        near = [p for p in peaks if abs(offsets[p] - prior) <= prior_window]
        if near and ncc[near[0]] >= 0.6 * ncc[chosen[0]]:
            best, used_prior = near[0], True
            if best not in chosen:
                chosen.insert(0, best)

    l, cc, r = ncc[best - 1], ncc[best], ncc[best + 1]
    den2 = l - 2 * cc + r
    sub = 0.5 * (l - r) / den2 if abs(den2) > 1e-12 else 0.0
    off = offsets[best] + sub / GRID_HZ

    if used_prior:   # competition only from matches the camera clock allows
        others = [ncc[p] for p in chosen
                  if p != best and abs(offsets[p] - prior) <= prior_window]
        method += " + camera clock"
    else:
        others = [ncc[p] for p in chosen if p != best]
    ratio = float(ncc[best] / max(others)) if others and max(others) > 0 else float("inf")
    alts = [(float(offsets[p]), float(ncc[p])) for p in chosen if p != best][:3]
    return SyncResult(float(off), float(ncc[best]), ratio, method, alts)


def auto_sync(video_path: str, tel: Telemetry, gyro: str | None = None,
              export_start: float = 0.0, prior: float | None = None,
              max_seconds: float = 600.0) -> SyncResult:
    # Insta360 camera MP4s (GO 3S, Ace Pro, ...) carry their own gyro log
    if not gyro and has_trailer(video_path):
        try:
            if 0x300 in read_insv_trailer(video_path):
                gyro = video_path
        except SyncError:
            pass
    if gyro:
        t, v = insv_yaw_rate(gyro, export_start)
        keep = (t >= 0) & (t <= max_seconds * 4)
        if keep.sum() < 100:
            raise SyncError("the gyro log doesn't cover the start of this video")
        return match(t[keep], v[keep], tel, "camera gyro", prior)
    t, v = video_yaw_proxy(video_path, max_seconds)
    return match(t, v, tel, "video motion", prior)
