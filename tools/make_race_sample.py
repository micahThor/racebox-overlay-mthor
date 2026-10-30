"""A RaceBox session that matches a real race, for testing race mode.

    python tools/make_race_sample.py sample/ [speedhive.json] [rider-number]

Rides the sample circuit with exactly the lap times rider #96 did in Speedhive
session 12935135 (or the rider and file you give): sits on the grid for 20 s,
standing start just past the line, five race laps, part of a cool-down lap.
Writes race_session.csv. Like a real RaceBox with the track set, its Lap
column counts line crossings, so the five race laps show up as an out lap
from the grid plus four timed laps.
"""
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_sample import LAT0, LON0, R, track_shape, speed_profile  # noqa: E402

# Speedhive session 12935135, #96: lap times (s)
DEFAULT_LAPS = [68.152, 65.150, 65.280, 65.483, 65.661]
SCALE = 0.5            # half-size circuit, roughly MiniGP lap length
GRID_WAIT = 20.0


def lap_times(path, number):
    data = json.loads(Path(path).read_text())
    rows = {r["position"]: r for r in data["classification"]["rows"]}
    for c in data["alllaptimes"]:
        if str(rows[c["position"]]["startNumber"]).strip() == number:
            out = []
            for l in sorted(c["laps"], key=lambda l: l["lap"]):
                v = 0.0
                for p in l["lapTime"].split(":"):
                    v = v * 60 + float(p)
                out.append(v)
            return out
    raise SystemExit(f"no rider {number} in {path}")


def main(out_dir, laps):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    x, y, L = track_shape()
    x, y, L = x * SCALE, y * SCALE, L * SCALE
    n = len(x)
    ds = L / n
    v_base = speed_profile(x, y, L, vmax=27.0, alat=9.0, acc=3.5, brk=8.0)
    dx, dy = np.gradient(x), np.gradient(y)
    ddx, ddy = np.gradient(dx), np.gradient(dy)
    k = (dx * ddy - dy * ddx) / np.maximum((dx * dx + dy * dy) ** 1.5, 1e-9)

    grid = int(0.03 * n)
    S, T = [float(grid)], [0.0]                      # track position (samples) vs time
    S.append(float(grid)); T.append(GRID_WAIT)
    t = GRID_WAIT
    for j, target in enumerate(laps):
        a, b = (grid, n) if j == 0 else (n * j, n * (j + 1))
        idx = np.arange(a, b)
        v = v_base[idx % n].copy()
        if j == 0:                                    # standing start
            v = np.minimum(v, np.sqrt(2 * 3.5 * (idx - a + 1) * ds) + 0.3)
        dt = ds / v
        dt *= target / dt.sum()                       # exactly the real lap time
        S.extend((idx + 1).astype(float)); T.extend(t + np.cumsum(dt))
        t += target
    a = n * len(laps)                                 # cool-down
    idx = np.arange(a, a + int(0.4 * n))
    dt = ds / (v_base[idx % n] * 0.6)
    S.extend((idx + 1).astype(float)); T.extend(t + np.cumsum(dt))
    S, T = np.array(S), np.array(T)

    rng = np.random.default_rng(96)
    hz = 25.0
    ts = np.arange(0, T[-1], 1 / hz)
    s = np.interp(ts, T, S)
    spd = np.gradient(s, ts) * ds
    t0 = datetime(2026, 10, 4, 16, 37, 0, tzinfo=timezone.utc)
    path = out / "race_session.csv"
    with open(path, "w") as fh:
        fh.write("Data Source,RaceBox Mini S\nTrack,Sample Circuit\n")
        fh.write("Record,Time,Latitude,Longitude,Altitude,Speed,GForceX,GForceY,GForceZ,Lap,GyroX,GyroY,GyroZ\n")
        for r, (tt, si, v) in enumerate(zip(ts, s, spd), 1):
            i0 = int(si) % n
            f = si - int(si)
            px = x[i0] + (x[(i0 + 1) % n] - x[i0]) * f
            py = y[i0] + (y[(i0 + 1) % n] - y[i0]) * f
            lat = LAT0 + math.degrees((py + rng.normal(0, 0.2)) / R)
            lon = LON0 + math.degrees((px + rng.normal(0, 0.2)) / (R * math.cos(math.radians(LAT0))))
            lap = int(si // n)
            yaw = math.degrees(v * k[i0])
            stamp = (t0 + timedelta(seconds=float(tt))).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            fh.write(f"{r},{stamp},{lat:.7f},{lon:.7f},180.0,{v * 3.6:.2f},0,0,1,{lap},0,0,{yaw:.2f}\n")
    cross = [GRID_WAIT + sum(laps[:j + 1]) for j in range(len(laps))]
    print(f"wrote {path}: {len(ts)} samples, {ts[-1]:.0f} s; race start at {GRID_WAIT:.0f} s, "
          f"line crossings at " + ", ".join(f"{c:.2f}" for c in cross))


if __name__ == "__main__":
    a = sys.argv[1:]
    laps = lap_times(a[1], a[2] if len(a) > 2 else "96") if len(a) > 1 else DEFAULT_LAPS
    main(a[0] if a else "sample", laps)
