"""Build test inputs for auto-sync from a sample session (see make_sample.py):

  turning_video.mp4   a camera view that rotates with the car (scene slides
                      sideways in corners) - for video-motion sync
  turning_video.insv  the same video with an Insta360-style IMU trailer from a
                      tilted camera - for gyro sync

    python tools/make_sync_sample.py sample/racebox_session.csv sample/ --offset 12.5
"""
import argparse
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rbo import telemetry  # noqa: E402
from rbo.sync import MAGIC  # noqa: E402


def heading_at(tel, t):
    x, y = telemetry.enu(tel.lat, tel.lon, tel.lat.mean(), tel.lon.mean())
    k = 9
    xs = np.convolve(x, np.ones(k) / k, mode="same")
    ys = np.convolve(y, np.ones(k) / k, mode="same")
    h = np.unwrap(np.arctan2(np.gradient(xs), np.gradient(ys)))
    return np.interp(t, tel.t, h)


def panorama(width, height, seed=3):
    rng = np.random.default_rng(seed)
    noise = rng.normal(size=(height, width))
    f = np.fft.fft2(noise)
    fy = np.fft.fftfreq(height)[:, None]
    fx = np.fft.fftfreq(width)[None, :]
    f *= 1.0 / (1e-3 + np.hypot(fx, fy)) ** 1.1
    img = np.real(np.fft.ifft2(f))
    img = (img - img.min()) / (img.max() - img.min())
    return (40 + 180 * img).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("out_dir")
    ap.add_argument("--offset", type=float, default=12.5, help="video starts this far into the data")
    ap.add_argument("--duration", type=float, default=150)
    ap.add_argument("--imu-format", choices=["56", "20"], default="56")
    ap.add_argument("--layout", choices=["chain", "directory"], default="chain",
                    help="directory = newer cameras' trailer with a record table")
    a = ap.parse_args()

    tel = telemetry.load(a.session)
    out = Path(a.out_dir)
    W, H, fps = 640, 360, 30
    pano_w = 4096
    pano = panorama(pano_w, H)
    px_per_rad = pano_w / (2 * np.pi)
    rng = np.random.default_rng(1)

    n = int(a.duration * fps)
    vt = np.arange(n) / fps
    head = heading_at(tel, tel.t[0] + a.offset + vt)
    shake = np.cumsum(rng.normal(0, 0.002, n))           # camera mount wobble (rad)
    video = out / "turning_video.mp4"
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray",
                          "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
                          "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(video)],
                         stdin=subprocess.PIPE)
    for i in range(n):
        x0 = int((head[i] + shake[i]) * px_per_rad) % pano_w
        cols = (np.arange(W) + x0) % pano_w
        frame = pano[:, cols].copy()
        road = H * 2 // 3                                   # road surface: changes every frame
        frame[road:] = rng.integers(60, 120, size=(H - road, W), dtype=np.uint8)
        p.stdin.write(frame.tobytes())
    p.stdin.close()
    p.wait()

    # --- fake .insv: same video + IMU trailer from a camera tilted on its mount
    imu_hz = 200
    cam_clock0 = 7_000_000           # camera's own ms clock, arbitrary
    frame0_ms = cam_clock0 + 850     # IMU starts logging before the first frame
    it = np.arange(0, a.duration + 0.85, 1 / imu_hz)
    ms = cam_clock0 + it * 1000
    video_t = (ms - frame0_ms) / 1000
    h = heading_at(tel, tel.t[0] + a.offset + video_t)
    yaw = np.gradient(h) * imu_hz + rng.normal(0, 0.02, len(it))
    tilt = np.radians(35)            # pitched down 35 deg, rolled 10 deg
    roll = np.radians(10)
    Rx = np.array([[1, 0, 0], [0, np.cos(tilt), -np.sin(tilt)], [0, np.sin(tilt), np.cos(tilt)]])
    Ry = np.array([[np.cos(roll), 0, np.sin(roll)], [0, 1, 0], [-np.sin(roll), 0, np.cos(roll)]])
    R = Ry @ Rx
    gyro = (R @ np.vstack([np.zeros_like(yaw), np.zeros_like(yaw), yaw])).T
    gyro += rng.normal(0, 0.03, gyro.shape)
    acc = np.tile(R @ np.array([0, 0, 1.0]), (len(it), 1)) + rng.normal(0, 0.05, (len(it), 3))

    if a.imu_format == "56":
        rec = np.zeros(len(it), dtype=[("ms", "<u8"), ("v", "<f8", 6)])
        # cameras store float32-precision values in doubles
        rec["ms"], rec["v"] = ms.astype(np.uint64), np.hstack([acc, gyro]).astype(np.float32)
    else:
        rec = np.zeros(len(it), dtype=[("ms", "<u8"), ("v", "<u2", 6)])
        rec["ms"] = ms.astype(np.uint64)
        rec["v"] = np.clip(np.hstack([acc, gyro]) * 1000 + 0x8000, 0, 65535).astype(np.uint16)
    imu = rec.tobytes()
    stamps = (frame0_ms + np.arange(n) * 1000 / fps).astype("<u8").tobytes()

    if a.layout == "chain":
        body = imu + struct.pack("<HI", 0x300, len(imu)) + stamps
        last = struct.pack("<HI", 0x600, len(stamps))
    else:
        # [imu][hdr][stamps][hdr][directory] with the directory record last
        body = imu + struct.pack("<HI", 0x300, len(imu)) + stamps + struct.pack("<HI", 0x600, len(stamps))
        table = (struct.pack("<HII", 0, 0, 0) + struct.pack("<HII", 0x300, len(imu), 0)
                 + struct.pack("<HII", 0x600, len(stamps), len(imu) + 6))
        body += table
        last = struct.pack("<HI", 0, len(table))
    total = len(body) + 78
    trailer = body + last + b"\0" * 32 + struct.pack("<I", total) + b"\0" * 4 + MAGIC
    insv = out / ("turning_video.insv" if a.layout == "chain" else "GO3S_camera.mp4")
    insv.write_bytes(video.read_bytes() + trailer)
    print(f"wrote {video} and {insv}: video t=0 is {a.offset}s into the data")


if __name__ == "__main__":
    main()
