"""GoPro sensor data (GPMF): the gyro GoPro cameras record inside every MP4.

HERO5 and later write a data track ("GoPro MET", codec tag gpmd) next to the
video: about one packet a second, each holding that second's accelerometer
(ACCL), gyro (GYRO) and, on most models, GPS. auto-sync uses the gyro the same
way it uses an Insta360's gyro log. Only the original file from the camera has
the track; exports from GoPro Quik and most editors drop it.

GPMF is KLV: a 4-character key, a type character, a size byte (bytes per
sample) and a big-endian uint16 repeat count, then the data padded to a
multiple of 4 bytes. Type 0 means the data is more KLV (DEVC > STRM > ...).
See github.com/gopro/gpmf-parser.
"""
from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path

import numpy as np

_NUM = {"b": "b", "B": "B", "s": "h", "S": "H", "l": "i", "L": "I", "j": "q", "J": "Q",
        "f": "f", "d": "d"}
_FIXED = {"q": ("i", 65536.0), "Q": ("q", 4294967296.0)}       # Q15.16, Q31.32


class GoProError(Exception):
    pass


def klv(buf: bytes, pos: int = 0, end: int | None = None):
    """(key, type, size, repeat, data) for each item in buf[pos:end]."""
    end = len(buf) if end is None else end
    while pos + 8 <= end:
        key = buf[pos:pos + 4]
        typ = chr(buf[pos + 4])
        size = buf[pos + 5]
        rep = struct.unpack_from(">H", buf, pos + 6)[0]
        n = size * rep
        data = buf[pos + 8:pos + 8 + n]
        if key == b"\x00\x00\x00\x00":
            break
        yield key.decode("latin-1"), typ, size, rep, data
        pos += 8 + ((n + 3) & ~3)


def values(typ: str, size: int, rep: int, data: bytes) -> np.ndarray | str | None:
    """A KLV item's data: rows of numbers (one per sample), or text."""
    if typ == "c":
        return data.split(b"\x00")[0].decode("latin-1")
    if typ in _FIXED:
        code, div = _FIXED[typ]
    elif typ in _NUM:
        code, div = _NUM[typ], 1.0
    else:
        return None
    w = struct.calcsize(code)
    if size % w:
        return None
    a = np.frombuffer(data[: size * rep], dtype=">" + code).astype(float) / div
    return a.reshape(rep, size // w)


def streams(payload: bytes) -> list[dict]:
    """Each sensor stream in one GPMF payload: {"key": "GYRO", "data": rows, "scal": ...}."""
    out = []

    def walk(buf, pos, end):
        for key, typ, size, rep, data in klv(buf, pos, end):
            if typ == "\x00" and key in ("DEVC", "STRM"):
                if key == "STRM":
                    out.append(_stream(data))
                else:
                    walk(data, 0, len(data))
    walk(payload, 0, len(payload))
    return [s for s in out if s.get("key")]


def _stream(buf: bytes) -> dict:
    s: dict = {}
    for key, typ, size, rep, data in klv(buf):
        if key == "SCAL":
            s["scal"] = values(typ, size, rep, data)
        elif key == "ORIN":
            s["orin"] = data.decode("latin-1")[: size * rep]
        elif key == "STNM":
            s["name"] = values(typ, size, rep, data)
        elif key in ("ACCL", "GYRO", "GPS5", "GPS9"):
            v = values(typ, size, rep, data)
            if isinstance(v, np.ndarray):
                s["key"], s["data"] = key, v
    if "data" in s and s.get("scal") is not None:
        sc = np.asarray(s["scal"], float).ravel()
        if sc.size in (1, s["data"].shape[1]):
            s["data"] = s["data"] / np.where(sc == 0, 1, sc)
    return s


# ---------------------------------------------------------------- reading a file

def _gpmd_stream(path: str) -> int | None:
    from .video import need
    out = subprocess.run([need("ffprobe"), "-v", "error", "-show_entries",
                          "stream=index,codec_type,codec_tag_string", "-of", "json", path],
                         capture_output=True, text=True).stdout
    try:
        for st in json.loads(out).get("streams", []):
            if st.get("codec_tag_string") == "gpmd":
                return int(st["index"])
    except (ValueError, KeyError):
        pass
    return None


def has_gpmf(path: str | Path) -> bool:
    """Whether this video has GoPro sensor data (a quick look at its tracks)."""
    try:
        return _gpmd_stream(str(path)) is not None
    except Exception:  # noqa: BLE001 - no ffprobe, unreadable file, ...
        return False


def packets(path: str | Path) -> list[tuple[float, float, bytes]]:
    """(start s, duration s, payload) for each GPMF packet, in video time."""
    from .video import need
    path = str(path)
    idx = _gpmd_stream(path)
    if idx is None:
        raise GoProError(f"{Path(path).name} has no GoPro sensor data. Use the original file from "
                         "the camera's memory card, not one exported from GoPro Quik or an editor")
    meta = subprocess.run([need("ffprobe"), "-v", "error", "-select_streams", str(idx),
                           "-show_entries", "packet=pts_time,duration_time,size", "-of", "json", path],
                          capture_output=True, text=True).stdout
    pk = json.loads(meta).get("packets", [])
    raw = subprocess.run([need("ffmpeg"), "-v", "error", "-i", path, "-map", f"0:{idx}", "-c", "copy",
                          "-f", "data", "-"], capture_output=True).stdout
    out, pos = [], 0
    for p in pk:
        n = int(p["size"])
        t = float(p.get("pts_time") or 0)
        dur = float(p.get("duration_time") or 1.0)
        out.append((t, dur, raw[pos:pos + n]))
        pos += n
    if pos != len(raw):
        raise GoProError("couldn't split the GoPro sensor data into its packets")
    return out


def sensor(path: str | Path, key: str) -> tuple[np.ndarray, np.ndarray, str | None]:
    """Times (s, video time) and samples of one sensor stream, e.g. "GYRO"."""
    ts, rows, orin = [], [], None
    for t0, dur, payload in packets(path):
        for s in streams(payload):
            if s["key"] != key:
                continue
            d = s["data"]
            if len(d):
                ts.append(t0 + (np.arange(len(d)) + 0.5) * dur / len(d))   # spread across the packet
                rows.append(d)
                orin = orin or s.get("orin")
    if not rows:
        raise GoProError(f"{Path(path).name} has no {key} data")
    return np.concatenate(ts), np.concatenate(rows), orin


def _camera_axes(rows: np.ndarray, orin: str | None) -> np.ndarray:
    """Put samples in camera X, Y, Z order using the stream's ORIN (e.g. "ZXY")."""
    if not orin or len(orin) != rows.shape[1]:
        return rows
    out = np.zeros_like(rows)
    for i, ch in enumerate(orin):
        axis = "XYZ".index(ch.upper()) if ch.upper() in "XYZ" else i
        out[:, axis] = rows[:, i] * (-1 if ch.islower() else 1)
    return out


def yaw_rate(path: str | Path, export_start: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """Camera yaw rate (rad/s) vs. video time: rotation about gravity, so the
    camera can be mounted at any angle."""
    tg, gyr, og = sensor(path, "GYRO")
    _, acc, oa = sensor(path, "ACCL")
    gyr, acc = _camera_axes(gyr[:, :3], og), _camera_axes(acc[:, :3], oa)
    up = acc.mean(axis=0)
    norm = np.linalg.norm(up)
    if norm < 1e-6:
        raise GoProError("couldn't find the gravity direction in the GoPro data")
    return tg - export_start, gyr @ (up / norm)
