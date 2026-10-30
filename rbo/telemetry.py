"""Load RaceBox exports (CSV or VBO) into numpy arrays.

RaceBox CSV layout (export from racebox.pro -> My Sessions -> Export -> CSV):

    Data Source,RaceBox Mini
    Date UTC,2024-06-15T10:00:00Z
    Track,...                      <- optional metadata lines
    Record,Time,Latitude,Longitude,Altitude,Speed,GForceX,GForceY,GForceZ,Lap,GyroX,GyroY,GyroZ
    1,2024-06-15T10:00:00.000Z,50.4372,5.9719,400.0,0.0,...

Bike mode exports drop GForceY and add LeanAngle. Speed is km/h, Lap is the
logger's lap number (0 for the out-lap and the in-lap).

Column matching is forgiving (case/spacing/units in the name are ignored), so
the "Custom" export presets and other apps' CSVs usually load too.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

EARTH_R = 6371000.0


@dataclass
class Telemetry:
    t: np.ndarray                 # seconds since Unix epoch, UTC
    lat: np.ndarray
    lon: np.ndarray
    speed_kmh: np.ndarray
    alt: np.ndarray | None = None
    g_long: np.ndarray | None = None   # + accelerating, - braking
    g_lat: np.ndarray | None = None    # + toward the right (right-hand corner)
    lean_deg: np.ndarray | None = None  # + leaning right
    yaw_rate: np.ndarray | None = None  # deg/s
    lap: np.ndarray | None = None       # logger lap number, if exported
    meta: dict = field(default_factory=dict)
    start_line: tuple | None = None     # ((lat,lon),(lat,lon)) from a VBO [laptiming] section
    source: str = ""

    @property
    def is_bike(self) -> bool:
        return self.lean_deg is not None

    @property
    def duration(self) -> float:
        return float(self.t[-1] - self.t[0])

    def at(self, name: str, t):
        arr = getattr(self, name)
        if arr is None:
            return None
        return np.interp(t, self.t, arr)


# ---------------------------------------------------------------- helpers

def _norm(name: str) -> str:
    name = re.sub(r"\(.*?\)|\[.*?\]", "", name)      # drop "(km/h)" etc.
    return re.sub(r"[^a-z0-9]", "", name.lower())


ALIASES = {
    "time": ["time", "utctime", "timestamp", "datetime", "gpstime"],
    "lat": ["latitude", "lat"],
    "lon": ["longitude", "lon", "long", "lng"],
    "alt": ["altitude", "alt", "height", "elevation"],
    "speed": ["speed", "velocity", "gpsspeed"],
    "g_long": ["gforcex", "longacc", "longitudinalg", "longg", "accelx", "inlineacc"],
    "g_lat": ["gforcey", "latacc", "lateralg", "latg", "corneringg", "accely"],
    "lean": ["leanangle", "lean", "bankangle"],
    "yaw": ["gyroz", "yawrate", "yaw"],
    "lap": ["lap", "lapnumber", "lapindex"],
}


def _find(cols: dict[str, str], key: str) -> str | None:
    for alias in ALIASES[key]:
        if alias in cols:
            return cols[alias]
    return None


def _speed_factor(header: str) -> float:
    h = header.lower()
    if "mph" in h:
        return 1.609344
    if "m/s" in h or "ms-1" in h:
        return 3.6
    if "knot" in h or "kts" in h:
        return 1.852
    return 1.0  # RaceBox exports km/h


def _parse_time(values: list[str]) -> np.ndarray:
    """ISO timestamps, HH:MM:SS(.sss) or plain seconds -> epoch seconds."""
    sample = values[0].strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", sample):
        out = []
        for v in values:
            v = v.strip().replace("Z", "+00:00").replace(" ", "T", 1)
            d = datetime.fromisoformat(v)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            out.append(d.timestamp())
        return np.array(out)
    if ":" in sample:
        out = []
        for v in values:
            parts = [float(p) for p in v.strip().split(":")]
            s = 0.0
            for p in parts:
                s = s * 60 + p
            out.append(s)
        return _unwrap_midnight(np.array(out))
    return np.array([float(v) for v in values])


def _unwrap_midnight(t: np.ndarray) -> np.ndarray:
    jumps = np.where(np.diff(t) < -43200)[0]
    for j in jumps:
        t[j + 1:] += 86400
    return t


def _arr(rows, col, factor=1.0):
    if col is None:
        return None
    try:
        return np.array([float(r[col]) if r[col].strip() != "" else np.nan for r in rows]) * factor
    except (ValueError, KeyError):
        return None


# ---------------------------------------------------------------- CSV

def load_csv(path: str | Path) -> Telemetry:
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()

    # the data header is the first line that names a time AND a latitude column
    hdr_idx = None
    for i, line in enumerate(lines[:200]):
        cells = {_norm(c) for c in line.split(",")}
        if cells & set(ALIASES["lat"]) and cells & set(ALIASES["time"]):
            hdr_idx = i
            break
    if hdr_idx is None:
        raise ValueError(f"{path}: couldn't find a header row with Time and Latitude columns")

    meta = {}
    for line in lines[:hdr_idx]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0]:
            meta[parts[0]] = parts[1]

    reader = csv.DictReader(io.StringIO("\n".join(lines[hdr_idx:])))
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    cols = {_norm(h): h for h in headers}
    rows = [r for r in reader if r and all(r.get(h) is not None for h in headers[:3])]

    c_time, c_lat, c_lon = _find(cols, "time"), _find(cols, "lat"), _find(cols, "lon")
    c_speed = _find(cols, "speed")
    if not (c_time and c_lat and c_lon):
        raise ValueError(f"{path}: need Time, Latitude and Longitude columns; found {headers}")

    rows = [r for r in rows if r[c_lat].strip() and r[c_lon].strip()]
    if not rows:
        raise ValueError(f"{path}: no data rows")

    t = _parse_time([r[c_time] for r in rows])
    lat = _arr(rows, c_lat)
    lon = _arr(rows, c_lon)
    speed = _arr(rows, c_speed, _speed_factor(c_speed)) if c_speed else None

    tel = Telemetry(
        t=t, lat=lat, lon=lon,
        speed_kmh=speed if speed is not None else np.zeros_like(t),
        alt=_arr(rows, _find(cols, "alt")),
        g_long=_arr(rows, _find(cols, "g_long")),
        g_lat=_arr(rows, _find(cols, "g_lat")),
        lean_deg=_arr(rows, _find(cols, "lean")),
        yaw_rate=_arr(rows, _find(cols, "yaw")),
        lap=_arr(rows, _find(cols, "lap")),
        meta=meta,
        source=str(path),
    )
    if speed is None:
        tel.speed_kmh = _speed_from_positions(tel)
    return _finish(tel)


# ---------------------------------------------------------------- VBO

def load_vbo(path: str | Path) -> Telemetry:
    """Racelogic .vbo text format (RaceBox can export this too)."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    sections: dict[str, list[str]] = {}
    cur = "_preamble"
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].lower()
            sections.setdefault(cur, [])
        elif s:
            sections.setdefault(cur, []).append(s)

    names = " ".join(sections.get("column names", [])).split()
    data = [ln.split() for ln in sections.get("data", [])]
    data = [d for d in data if len(d) >= len(names)]
    if not names or not data:
        raise ValueError(f"{path}: missing [column names] or [data]")
    idx = {n.lower(): i for i, n in enumerate(names)}

    def col(*keys, factor=1.0):
        for k in keys:
            if k in idx:
                return np.array([float(d[idx[k]]) for d in data]) * factor
        return None

    # date from "File created on 15/04/2024 @ 18:49:01"
    day0 = 0.0
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", " ".join(sections.get("_preamble", [])))
    if m:
        day0 = datetime(int(m[3]), int(m[2]), int(m[1]), tzinfo=timezone.utc).timestamp()

    raw_t = [d[idx["time"]] for d in data]
    secs = np.array([int(v[0:2]) * 3600 + int(v[2:4]) * 60 + float(v[4:]) for v in raw_t])
    t = day0 + _unwrap_midnight(secs)

    # VBO stores minutes; longitude is positive-WEST
    lat = col("lat") / 60.0
    lon = -col("long") / 60.0

    start_line = None
    for ln in sections.get("laptiming", []):
        if ln.lower().startswith("start"):
            nums = [float(x) for x in re.findall(r"[-+]?\d+\.\d+", ln)[:4]]
            if len(nums) == 4:
                start_line = ((nums[1] / 60, -nums[0] / 60), (nums[3] / 60, -nums[2] / 60))

    tel = Telemetry(
        t=t, lat=lat, lon=lon,
        speed_kmh=col("velocity", "speed"),
        alt=col("height", "altitude"),
        g_long=col("longacc", "long_accel", "longitudinal_acceleration"),
        g_lat=col("latacc", "lat_accel", "lateral_acceleration"),
        lean_deg=col("lean_angle", "leanangle", "lean"),
        yaw_rate=col("yaw_rate", "yawrate", "gyroz"),
        lap=col("lap", "lap_number"),
        start_line=start_line,
        source=str(path),
    )
    if tel.speed_kmh is None:
        tel.speed_kmh = _speed_from_positions(tel)
    return _finish(tel)


# ---------------------------------------------------------------- common

def load(path: str | Path) -> Telemetry:
    p = Path(path)
    if p.suffix.lower() == ".vbo":
        return load_vbo(p)
    return load_csv(p)


def _speed_from_positions(tel: Telemetry) -> np.ndarray:
    x, y = enu(tel.lat, tel.lon, tel.lat.mean(), tel.lon.mean())
    d = np.hypot(np.gradient(x), np.gradient(y))
    return d / np.maximum(np.gradient(tel.t), 1e-3) * 3.6


def enu(lat, lon, lat0, lon0):
    """Local flat-earth projection in metres (x east, y north)."""
    x = np.radians(np.asarray(lon) - lon0) * EARTH_R * np.cos(np.radians(lat0))
    y = np.radians(np.asarray(lat) - lat0) * EARTH_R
    return x, y


def _finish(tel: Telemetry) -> Telemetry:
    # drop no-fix rows, sort, de-duplicate timestamps
    ok = np.isfinite(tel.lat) & np.isfinite(tel.lon) & ~((tel.lat == 0) & (tel.lon == 0))
    order = np.argsort(tel.t[ok], kind="stable")
    keep = np.flatnonzero(ok)[order]
    _, uniq = np.unique(tel.t[keep], return_index=True)
    keep = keep[np.sort(uniq)]
    for name in ("t", "lat", "lon", "speed_kmh", "alt", "g_long", "g_lat",
                 "lean_deg", "yaw_rate", "lap"):
        arr = getattr(tel, name)
        if arr is not None:
            arr = arr[keep]
            if name != "t" and np.isnan(arr).any():
                good = np.isfinite(arr)
                arr = np.interp(tel.t[keep], tel.t[keep][good], arr[good]) if good.any() else None
            setattr(tel, name, arr)
    if len(tel.t) < 10:
        raise ValueError(f"{tel.source}: fewer than 10 usable GPS samples")
    _orient_axes(tel)
    return tel


def _orient_axes(tel: Telemetry) -> None:
    """Loggers disagree on axis signs. Check each channel against what the GPS
    track says actually happened and flip it if it's backwards."""
    x, y = enu(tel.lat, tel.lon, tel.lat.mean(), tel.lon.mean())
    v = tel.speed_kmh / 3.6
    moving = v > 5
    if moving.sum() < 20:
        return
    dt = np.maximum(np.gradient(tel.t), 1e-3)
    heading = np.unwrap(np.arctan2(np.gradient(x), np.gradient(y)))   # clockwise from north
    turn = np.gradient(heading) / dt                                   # + = turning right
    lat_gps = v * turn / 9.81
    long_gps = np.gradient(v) / dt / 9.81

    def corr(a, b):
        a, b = a[moving], b[moving]
        if a.std() < 1e-6 or b.std() < 1e-6:
            return 0.0
        return float(np.corrcoef(a, b)[0, 1])

    if tel.g_long is not None and corr(tel.g_long, long_gps) < 0:
        tel.g_long = -tel.g_long
    if tel.g_lat is not None and corr(tel.g_lat, lat_gps) < 0:
        tel.g_lat = -tel.g_lat
    if tel.lean_deg is not None and corr(tel.lean_deg, lat_gps) < 0:
        tel.lean_deg = -tel.lean_deg
    if tel.yaw_rate is not None and corr(tel.yaw_rate, turn) < 0:
        tel.yaw_rate = -tel.yaw_rate
