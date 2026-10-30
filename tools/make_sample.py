"""Generate a fake RaceBox session (CSV + VBO) and a matching test video.

    python tools/make_sample.py sample/

Useful for trying the overlay before you have real track footage.
"""
import math
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

LAT0, LON0 = 44.0500, -86.1500   # an empty spot; any location works
R = 6371000.0


def track_shape(n=4000):
    """Closed loop: a long straight, a hairpin, an esses section, a sweeper."""
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    x = 420 * np.cos(t) + 60 * np.cos(3 * t)
    y = 230 * np.sin(t) + 50 * np.sin(2 * t) - 30 * np.sin(5 * t)
    # resample by arc length
    seg = np.hypot(np.diff(x, append=x[0]), np.diff(y, append=y[0]))
    s = np.concatenate([[0], np.cumsum(seg)])
    L = s[-1]
    ss = np.linspace(0, L, n, endpoint=False)
    xx = np.interp(ss, s, np.append(x, x[0]))
    yy = np.interp(ss, s, np.append(y, y[0]))
    return xx, yy, L


def speed_profile(x, y, L, vmax=52.0, alat=11.0, acc=5.0, brk=10.0):
    n = len(x)
    ds = L / n
    dx, dy = np.gradient(x), np.gradient(y)
    ddx, ddy = np.gradient(dx), np.gradient(dy)
    curv = np.abs(dx * ddy - dy * ddx) / np.maximum((dx * dx + dy * dy) ** 1.5, 1e-9)
    v = np.minimum(vmax, np.sqrt(alat / np.maximum(curv, 1e-6)))
    for _ in range(2):                       # acceleration / braking limits, around the loop
        for i in range(1, 2 * n):
            a, b = (i - 1) % n, i % n
            v[b] = min(v[b], math.sqrt(v[a] ** 2 + 2 * acc * ds))
        for i in range(2 * n, 0, -1):
            a, b = i % n, (i - 1) % n
            v[b] = min(v[b], math.sqrt(v[a] ** 2 + 2 * brk * ds))
    return v


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    x, y, L = track_shape()
    n = len(x)
    v_track = speed_profile(x, y, L)
    rng = np.random.default_rng(7)

    start_idx = int(n * 0.92)               # pit exit, a bit before the line
    pace = [0.80, 1.00, 0.985, 1.02, 0.995, 1.01, 0.70]   # out lap, 5 laps, in lap
    hz, t, s_pos, rows = 25.0, 0.0, float(start_idx), []
    lap_no, laps_done, total_laps = 0, 0, 5
    prev_v = 0.0
    gx, gy = np.gradient(x), np.gradient(y)
    ggx, ggy = np.gradient(gx), np.gradient(gy)
    while True:
        i = int(s_pos) % n
        phase = 0 if laps_done == 0 else (laps_done if laps_done <= total_laps else total_laps + 1)
        f = pace[min(phase, len(pace) - 1)]
        v = v_track[i] * f
        if t < 3:
            v = v * t / 3                      # pulling away
        if laps_done > total_laps and (s_pos - start_idx) % n > n * 0.35:
            break                               # end of in-lap
        dxs, dys, ddx, ddy = gx[i], gy[i], ggx[i], ggy[i]
        k = (dxs * ddy - dys * ddx) / max((dxs * dxs + dys * dys) ** 1.5, 1e-9)
        g_lat = -(v * v * k) / 9.81            # sign flipped on purpose: the loader must fix it
        g_long = (v - prev_v) * hz / 9.81
        lat = LAT0 + math.degrees((y[i] + rng.normal(0, 0.3)) / R)
        lon = LON0 + math.degrees((x[i] + rng.normal(0, 0.3)) / (R * math.cos(math.radians(LAT0))))
        rows.append((t, lat, lon, 180 + 3 * math.sin(i / n * 2 * math.pi), v * 3.6,
                     g_long, g_lat, lap_no, math.degrees(v * k)))
        prev_v = v
        old = s_pos
        s_pos += v / hz / (L / n)
        # line crossing at index 0
        if (old % n) > (s_pos % n):
            laps_done += 1
            lap_no = laps_done if laps_done <= total_laps else 0
        t += 1 / hz

    t0 = datetime(2026, 10, 24, 14, 30, 0, tzinfo=timezone.utc)
    csv_path = out / "racebox_session.csv"
    with open(csv_path, "w") as fh:
        fh.write("Data Source,RaceBox Mini S\n")
        fh.write(f"Date UTC,{t0.strftime('%Y-%m-%dT%H:%M:%SZ')}\n")
        fh.write("Track,Sample Circuit\nConfiguration,Full\nSession Type,Track\n")
        fh.write("Record,Time,Latitude,Longitude,Altitude,Speed,GForceX,GForceY,GForceZ,Lap,GyroX,GyroY,GyroZ\n")
        for r, (tt, la, lo, alt, spd, gl, gt, lp, yaw) in enumerate(rows, 1):
            ts = (t0 + timedelta(seconds=tt)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            fh.write(f"{r},{ts},{la:.7f},{lo:.7f},{alt:.1f},{spd:.2f},{gl:.3f},{gt:.3f},1.000,"
                     f"{lp},0.00,0.00,{yaw:.2f}\n")

    vbo_path = out / "racebox_session.vbo"
    with open(vbo_path, "w") as fh:
        fh.write(f"File created on {t0.strftime('%d/%m/%Y')} @ {t0.strftime('%H:%M:%S')}\n\n")
        fh.write("[header]\nsatellites\ntime\nlatitude\nlongitude\nvelocity kmh\nheight\n\n")
        fh.write("[column names]\nsats time lat long velocity height\n\n[data]\n")
        for tt, la, lo, alt, spd, *_ in rows:
            d = t0 + timedelta(seconds=tt)
            hms = f"{d.hour:02d}{d.minute:02d}{d.second + d.microsecond / 1e6:06.3f}"
            fh.write(f"012 {hms} {la * 60:+.6f} {-lo * 60:+.6f} {spd:07.3f} {alt:+.2f}\n")

    duration = rows[-1][0]
    video = out / "test_video.mp4"
    # video starts 12.5 s after logging started, so sync actually matters
    vid_len = min(duration - 12.5, 120)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"testsrc2=size=1920x1080:rate=30:duration={vid_len:.2f}",
                    "-f", "lavfi", "-i", f"sine=frequency=220:duration={vid_len:.2f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(video)], check=True)
    print(f"wrote {csv_path} ({len(rows)} samples, {duration:.0f}s), {vbo_path}, {video}")
    print("video t=0 corresponds to 12.5 s into the data  ->  --offset 12.5")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sample")
