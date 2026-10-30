"""A RaceBox session with known mistakes, for testing crew chief calls.

    python tools/make_crewchief_sample.py sample/

Lap plan (timed laps 1-7):
  1  baseline
  2  faster everywhere                      -> purple lap, purple sectors
  3  slow through one corner's apex         -> slow apex call
  4  runs ~4.5 m wide through a corner      -> wide call
  5  brakes ~30 m early into a corner       -> braked early call
  6  fastest lap                            -> purple lap
  7  close to lap 6                         -> consistency (with 5 and 6? no: 5 had a mistake)
Writes crewchief_session.csv and prints which corners got which mistake.
"""
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_sample import LAT0, LON0, R, track_shape, speed_profile  # noqa: E402


def corners(x, y):
    """Indices of the tightest bends (local curvature peaks)."""
    dx, dy = np.gradient(x), np.gradient(y)
    ddx, ddy = np.gradient(dx), np.gradient(dy)
    k = (dx * ddy - dy * ddx) / np.maximum((dx * dx + dy * dy) ** 1.5, 1e-9)
    k = np.convolve(k, np.ones(31) / 31, mode="same")
    n = len(k)
    peaks = [i for i in range(n) if abs(k[i]) > abs(k[i - 1]) and abs(k[i]) >= abs(k[(i + 1) % n])
             and abs(k[i]) > 1 / 400]
    return peaks, k


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    x, y, L = track_shape()
    n = len(x)
    ds = L / n
    v_base = speed_profile(x, y, L)
    v_soft_brake = speed_profile(x, y, L, brk=5.0)        # brakes much earlier
    peaks, k = corners(x, y)
    # corners well away from the start/finish line (index 0)
    order = [p for p in sorted(peaks) if 0.12 * n < p < 0.88 * n and abs(k[p]) > 1 / 250]
    # three different corners, spread round the lap
    c_apex, c_brake, c_wide = order[0], order[len(order) // 2], order[-1]
    print("corner apex indices:", order, " slow-apex:", c_apex, " wide:", c_wide, " early-brake:", c_brake)

    def window(c, before_m, after_m):
        idx = (np.arange(n) - c) % n
        idx = np.where(idx > n / 2, idx - n, idx) * ds
        return (idx >= -before_m) & (idx <= after_m), idx

    dx, dy = np.gradient(x), np.gradient(y)
    norm = np.hypot(dx, dy)
    nx, ny = dy / norm, -dx / norm                      # right of travel

    plan = {1: 1.0, 2: 1.015, 3: 1.015, 4: 1.018, 5: 1.018, 6: 1.022, 7: 1.021}
    laps = {}
    for lap, pace in plan.items():
        v = v_base * pace
        ox = np.zeros(n)
        if lap == 3:                                    # slow apex: scrub speed through the corner
            m, _ = window(c_apex, 25, 25)
            v[m] *= 0.86
        if lap == 4:                                    # run wide: drift to the outside, lift
            m, d = window(c_wide, 30, 60)
            bump = np.where(m, np.sin(np.clip((d + 30) / 90, 0, 1) * np.pi), 0)
            outside = np.sign(k[c_wide])                # k>0 = left turn -> outside is to the right
            ox = bump * 4.5 * outside
            v[m] *= 1 - 0.07 * bump[m]
        if lap == 5:                                    # early braking
            m, _ = window(c_brake, 260, 0)
            v[m] = np.minimum(v[m], v_soft_brake[m] * pace)
        laps[lap] = (v, ox)

    rng = np.random.default_rng(11)
    hz, t, rows = 25.0, 0.0, []
    s_pos = float(int(n * 0.9))
    lapno = 0
    total = len(plan)
    done = 0
    while True:
        i = int(s_pos) % n
        if done == 0:
            v = v_base[i] * 0.8 * min(1, t / 3 + 0.05)
            off = 0.0
        elif done <= total:
            vv, ox = laps[done]
            v, off = vv[i], ox[i]
        else:
            v, off = v_base[i] * 0.7, 0.0
            if (s_pos % n) > n * 0.3:
                break
        px, py = x[i] + nx[i] * off, y[i] + ny[i] * off
        lat = LAT0 + math.degrees((py + rng.normal(0, 0.25)) / R)
        lon = LON0 + math.degrees((px + rng.normal(0, 0.25)) / (R * math.cos(math.radians(LAT0))))
        rows.append((t, lat, lon, v * 3.6, lapno, math.degrees(v * k[i])))
        old = s_pos
        s_pos += v / hz / ds
        if (old % n) > (s_pos % n):
            done += 1
            lapno = done if done <= total else 0
        t += 1 / hz

    t0 = datetime(2026, 10, 25, 15, 0, 0, tzinfo=timezone.utc)
    path = out / "crewchief_session.csv"
    with open(path, "w") as fh:
        fh.write("Data Source,RaceBox Mini S\nTrack,Sample Circuit\n")
        fh.write("Record,Time,Latitude,Longitude,Altitude,Speed,GForceX,GForceY,GForceZ,Lap,GyroX,GyroY,GyroZ\n")
        for r, (tt, la, lo, spd, lp, yaw) in enumerate(rows, 1):
            ts = (t0 + timedelta(seconds=tt)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            fh.write(f"{r},{ts},{la:.7f},{lo:.7f},180.0,{spd:.2f},0,0,1,{lp},0,0,{yaw:.2f}\n")
    print(f"wrote {path}: {len(rows)} samples, {rows[-1][0]:.0f} s")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sample")
