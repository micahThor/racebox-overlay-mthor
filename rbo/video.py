"""ffmpeg plumbing: probe the video, stream overlay panels in, encode the result."""
from __future__ import annotations

import io
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from fractions import Fraction

from PIL import Image

from .hud import Hud


def need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        sys.exit(f"{tool} not found. On macOS: brew install ffmpeg")
    return path


def probe(path: str) -> dict:
    out = subprocess.run([need("ffprobe"), "-v", "error", "-show_streams", "-show_format",
                          "-of", "json", path], capture_output=True, text=True)
    if out.returncode:
        sys.exit(f"ffprobe couldn't read {path}:\n{out.stderr}")
    info = json.loads(out.stdout)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if v is None:
        sys.exit(f"{path} has no video stream")
    rate = v.get("avg_frame_rate") or v.get("r_frame_rate")
    if not rate or rate.startswith("0"):
        rate = v.get("r_frame_rate", "30/1")
    fps = Fraction(rate)
    w, h = int(v["width"]), int(v["height"])
    rot = int(float(v.get("tags", {}).get("rotate", 0) or 0))
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if abs(rot) in (90, 270):
        w, h = h, w
    ct = (info["format"].get("tags", {}) or {}).get("creation_time") or \
         (v.get("tags", {}) or {}).get("creation_time")
    created = None
    if ct:
        try:
            created = datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return {
        "width": w, "height": h, "fps": fps,
        "duration": float(info["format"].get("duration") or v.get("duration") or 0),
        "created": created,
        "has_audio": any(s["codec_type"] == "audio" for s in info["streams"]),
    }


def grab_frame(path: str, t: float) -> Image.Image:
    png = subprocess.run([need("ffmpeg"), "-v", "error", "-ss", f"{t:.3f}", "-i", path,
                          "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                         capture_output=True).stdout
    if not png:
        sys.exit(f"couldn't read a frame at {t:.2f}s from {path}")
    return Image.open(io.BytesIO(png)).convert("RGB")


def encoder_args(codec: str, width: int, height: int, fps: float, bitrate: str | None,
                 quality: str) -> list[str]:
    encoders = subprocess.run([need("ffmpeg"), "-hide_banner", "-encoders"],
                              capture_output=True, text=True).stdout
    hw = f"{codec}_videotoolbox"
    if quality != "best" and hw in encoders:
        if not bitrate:
            mbps = max(12, round(width * height * fps * 0.09 / 1e6))
            bitrate = f"{mbps}M"
        args = ["-c:v", hw, "-b:v", bitrate, "-allow_sw", "1"]
    else:
        sw = "libx265" if codec == "hevc" else "libx264"
        preset = "slow" if quality == "best" else "veryfast"
        args = ["-c:v", sw, "-preset", preset, "-crf", "18"]
        if bitrate:
            args = ["-c:v", sw, "-preset", preset, "-b:v", bitrate]
    if codec == "hevc":
        args += ["-tag:v", "hvc1"]       # so QuickTime / Photos will play it
    return args + ["-pix_fmt", "yuv420p"]


def render(video: str, hud: Hud, offset: float, out: str, start: float = 0.0,
           end: float | None = None, codec: str = "h264", bitrate: str | None = None,
           quality: str = "fast") -> None:
    info = probe(video)
    fps = info["fps"]
    end = min(end or info["duration"], info["duration"])
    n_frames = int((end - start) * fps)
    if n_frames <= 0:
        sys.exit("nothing to render: check --start/--end")

    # one rawvideo stream carries all panels stacked; crop each back out
    parts = [f"[1:v]split={len(hud.panels)}" + "".join(f"[a{i}]" for i in range(len(hud.panels)))]
    prev = "0:v"
    for i, p in enumerate(hud.panels):
        parts.append(f"[a{i}]crop={p.w}:{p.h}:0:{p.ay}[p{i}]")
        nxt = f"v{i}"
        parts.append(f"[{prev}][p{i}]overlay={p.x}:{p.y}:format=auto[{nxt}]")
        prev = nxt
    parts.append(f"[{prev}]format=yuv420p[vout]")

    cmd = [need("ffmpeg"), "-v", "error", "-nostats", "-y",
           "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", video,
           "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{hud.atlas_w}x{hud.atlas_h}",
           "-r", f"{fps.numerator}/{fps.denominator}", "-i", "pipe:0",
           "-filter_complex", ";".join(parts), "-map", "[vout]"]
    if info["has_audio"]:
        cmd += ["-map", "0:a:0", "-c:a", "copy"]
    cmd += encoder_args(codec, info["width"], info["height"], float(fps), bitrate, quality)
    cmd += ["-movflags", "+faststart", "-shortest", out]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    q: queue.Queue = queue.Queue(maxsize=8)
    err: list[str] = []

    def writer():
        try:
            while (buf := q.get()) is not None:
                proc.stdin.write(buf)
        except BrokenPipeError:
            pass
        finally:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass

    def drain():
        err.append(proc.stderr.read().decode(errors="replace"))

    threads = [threading.Thread(target=writer, daemon=True), threading.Thread(target=drain, daemon=True)]
    for th in threads:
        th.start()

    t0 = time.time()
    spf = 1 / float(fps)
    try:
        for i in range(n_frames):
            if proc.poll() is not None:
                break
            q.put(hud.render_atlas(start + i * spf + offset).tobytes())
            if i % 60 == 0 or i == n_frames - 1:
                el = time.time() - t0
                rate = (i + 1) / el if el else 0
                eta = (n_frames - i - 1) / rate if rate else 0
                print(f"\r  frame {i + 1}/{n_frames}  {rate:5.1f} fps  ETA {eta / 60:4.1f} min ",
                      end="", flush=True)
    except KeyboardInterrupt:
        print("\n  stopped; the partial file may not play")
    finally:
        q.put(None)
        threads[0].join()
        proc.wait()
        threads[1].join(timeout=5)
    print()
    if proc.returncode:
        tail = "".join(err)[-2000:]
        sys.exit(f"ffmpeg failed (exit {proc.returncode}):\n{tail}")
    print(f"  done in {(time.time() - t0) / 60:.1f} min -> {out}")
