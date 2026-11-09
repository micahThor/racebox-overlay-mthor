"""ffmpeg plumbing: probe the video, stream overlay panels in, encode the result."""
from __future__ import annotations

import io
import json
import multiprocessing
import os
import queue
import shutil
import subprocess
import tempfile
import sys
import threading
import time
from datetime import datetime
from fractions import Fraction

import numpy as np
from PIL import Image

from .hud import Hud


class RenderError(Exception):
    pass


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


# The Mac's hardware H.264 encoder stops at 4096x2304; past that VideoToolbox
# quietly falls back to a slow software encoder. Its HEVC encoder goes to 8K.
H264_HW_MAX = (4096, 2304)


def _ffmpeg_list(flag: str) -> str:
    return subprocess.run([need("ffmpeg"), "-hide_banner", flag], capture_output=True, text=True).stdout


def pick_codec(codec: str, width: int, height: int, quality: str) -> tuple[str, str]:
    """(codec, note): H.264 frames too big for the hardware encoder go out as
    HEVC instead, which the hardware can do (many times faster)."""
    if codec != "h264" or quality == "best":
        return codec, ""
    big = max(width, height) > H264_HW_MAX[0] or width * height > H264_HW_MAX[0] * H264_HW_MAX[1]
    if big and "hevc_videotoolbox" in _ffmpeg_list("-encoders"):
        return "hevc", (f"{width}x{height} is too big for the Mac's H.264 hardware encoder; "
                        "encoding HEVC on the hardware instead (much faster; plays on Macs, iPhones and YouTube)")
    return codec, ""


def decode_args() -> list[str]:
    """Decode the camera's video on the Mac's media engine when there is one."""
    return ["-hwaccel", "videotoolbox"] if "videotoolbox" in _ffmpeg_list("-hwaccels") else []


def encoder_args(codec: str, width: int, height: int, fps: float, bitrate: str | None,
                 quality: str) -> list[str]:
    encoders = _ffmpeg_list("-encoders")
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


_HUD = None


def _radio_mix(has_audio: bool) -> list[str]:
    """Filter graph: radio calls over the video's audio, which ducks under them."""
    if not has_audio:
        return ["[2:a]aresample=48000,apad[aout]"]
    return ["[0:a]aresample=48000,aformat=channel_layouts=stereo[eng]",
            "[2:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[vox][key]",
            "[eng][key]sidechaincompress=threshold=0.02:ratio=3:attack=15:release=350:makeup=1[duck]",
            "[duck][vox]amix=inputs=2:duration=first:normalize=0[aout]"]


def _init_worker(spec: dict) -> None:
    global _HUD
    _HUD = Hud(**spec)


def _render_one(t: float) -> bytes:
    return _HUD.render_atlas(t).tobytes()


def default_workers() -> int:
    return max(1, min((os.cpu_count() or 2) - 1, 8))


def render(video: str, hud: Hud, offset: float, out: str, start: float = 0.0,
           end: float | None = None, codec: str = "h264", bitrate: str | None = None,
           quality: str = "fast", progress=None, cancel: threading.Event | None = None,
           workers: int | None = None, radio: str | None = None) -> None:
    """radio: a WAV starting at the clip's start (the crew chief track) to mix
    over the video's audio, which dips under each call.
    progress(done, total, fps) is called every ~60 frames (default: print).
    Setting `cancel` stops the render early. Overlay frames are drawn on
    `workers` processes in parallel (default: one per CPU core, minus one)."""
    info = probe(video)
    fps = info["fps"]
    end = min(end or info["duration"], info["duration"])
    n_frames = int((end - start) * fps)
    if n_frames <= 0:
        raise RenderError("nothing to render: check --start/--end")

    def show(i, n, rate):
        eta = (n - i) / rate if rate else 0
        print(f"\r  frame {i}/{n}  {rate:5.1f} fps  ETA {eta / 60:4.1f} min ", end="", flush=True)
    progress = progress or show

    # one rawvideo stream carries all panels stacked; crop each back out
    parts = [f"[1:v]split={len(hud.panels)}" + "".join(f"[a{i}]" for i in range(len(hud.panels)))]
    prev = "0:v"
    out_w, out_h = info["width"], info["height"]
    # the frame round the video: the themed frame (a still image, converted
    # once and repeated) is the background; the video is laid into its opening,
    # then the frame's shaped corners go back over the video's corners. (Laying
    # a full-size see-through frame over every video frame cost far more.)
    out_w, out_h = hud.canvas_w, hud.canvas_h
    frame_dir = tempfile.mkdtemp(prefix="rbo-frame-")
    frame_png = os.path.join(frame_dir, "frame.png")
    corners_png = os.path.join(frame_dir, "corners.png")
    fimg = hud.frame_image()
    fimg.save(frame_png)
    cs, corners = hud.frame_corners()
    strip = Image.new("RGBA", (cs * 4, cs), (0, 0, 0, 0))
    for k, (cx, cy) in enumerate(corners):
        strip.paste(fimg.crop((cx, cy, cx + cs, cy + cs)), (k * cs, 0))
    strip.save(corners_png)
    fi = 3 if radio else 2                     # the frame's input number (the corners follow it)
    fr = f"{fps.numerator}/{fps.denominator}"
    parts.append(f"[{fi}:v]format=yuv420p,loop=loop=-1:size=1:start=0[bg]")
    parts.append(f"[bg][0:v]overlay={hud.vid_x0}:{hud.vid_y0}:shortest=1[vid]")
    parts.append(f"[{fi + 1}:v]format=yuva420p,loop=loop=-1:size=1:start=0,split=4[c0][c1][c2][c3]")
    prev = "vid"
    for k, (cx, cy) in enumerate(corners):
        parts.append(f"[c{k}]crop={cs}:{cs}:{k * cs}:0[cc{k}]")
        parts.append(f"[{prev}][cc{k}]overlay={cx}:{cy}:shortest=1[vc{k}]")
        prev = f"vc{k}"
    parts.append(f"[{prev}]null[base]")
    prev = "base"
    # an earlier session's results, big over the picture for the first seconds:
    # the panels on the picture wait until it fades (the strips' stay put)
    res = getattr(hud, "results", None)
    res_in = []
    hold = ""
    if res is not None and res.rows:
        from .results import FADE_IN, FADE_OUT, RESULTS_SECONDS
        res_png = os.path.join(frame_dir, "results.png")
        hud.results_card(res).save(res_png)
        rx, ry, _, _, _ = hud.results_rect(len(res.rows))
        res_in = ["-loop", "1", "-framerate", fr,
                  "-t", f"{RESULTS_SECONDS:.2f}", "-i", res_png]
        hold = f":enable='gte(t,{RESULTS_SECONDS - FADE_OUT / 2:.2f})'"
    for i, p in enumerate(hud.panels):
        parts.append(f"[a{i}]crop={p.vw}:{p.vh}:{p.vx}:{p.ay + p.vy}[p{i}]")
        nxt = f"v{i}"
        en = hold if hold and not hud.in_side(p.key) else ""
        parts.append(f"[{prev}][p{i}]overlay={p.x}:{p.y}:format=auto{en}[{nxt}]")
        prev = nxt
    if res_in:
        ri = fi + 2
        parts.append(f"[{ri}:v]format=rgba,fade=t=in:st=0:d={FADE_IN}:alpha=1,"
                     f"fade=t=out:st={RESULTS_SECONDS - FADE_OUT:.2f}:d={FADE_OUT}:alpha=1[res]")
        parts.append(f"[{prev}][res]overlay={rx}:{ry}:format=auto:eof_action=pass[vres]")
        prev = "vres"
    parts.append(f"[{prev}]format=yuv420p[vout]")

    cmd = [need("ffmpeg"), "-v", "error", "-nostats", "-y",
           *decode_args(), "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", video,
           "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{hud.atlas_w}x{hud.atlas_h}",
           "-r", f"{fps.numerator}/{fps.denominator}", "-i", "pipe:0",
           *(["-i", radio] if radio else []),
           "-framerate", fr, "-i", frame_png, "-framerate", fr, "-i", corners_png,
           *res_in,
           "-filter_complex", ";".join(parts + _radio_mix(info["has_audio"]) if radio else parts),
           "-map", "[vout]"]
    if radio:
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
    elif info["has_audio"]:
        cmd += ["-map", "0:a:0", "-c:a", "copy"]
    codec, note = pick_codec(codec, out_w, out_h, quality)
    if note:
        print(f"  {note}", flush=True)
    cmd += encoder_args(codec, out_w, out_h, float(fps), bitrate, quality)
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
    times = [start + i * spf + offset for i in range(n_frames)]
    workers = default_workers() if workers is None else max(1, workers)
    pool = None
    if workers > 1 and n_frames > 60:
        pool = multiprocessing.get_context("spawn").Pool(
            workers, initializer=_init_worker, initargs=(hud.spec,))
        frames = pool.imap(_render_one, times, chunksize=4)
    else:
        frames = (hud.render_atlas(t).tobytes() for t in times)
    feed = None
    slot = hud.ghost_slot() if hud.ghost else None
    if slot:
        from .ghost import GhostFeed
        feed = GhostFeed(hud.ghost.video or video, hud.ghost.fps or float(fps), slot[2], slot[3])
    wait_draw = wait_ffmpeg = 0.0          # where the time goes (for the summary line)
    frames = iter(frames)
    try:
        for i in range(n_frames):
            a = time.time()
            try:
                buf = next(frames)
            except StopIteration:
                break
            wait_draw += time.time() - a
            if proc.poll() is not None or (cancel is not None and cancel.is_set()):
                break
            if feed is not None:
                g = hud.ghost_info(times[i])
                if g.active:
                    gf = feed.get(g.ref_vt)
                    if gf is not None:
                        arr = np.frombuffer(buf, np.uint8).reshape(hud.atlas_h, hud.atlas_w, 4).copy()
                        hud.paste_ghost(arr, gf)
                        buf = arr.tobytes()
            a = time.time()
            q.put(buf)
            wait_ffmpeg += time.time() - a
            if i % 60 == 0 or i == n_frames - 1:
                el = time.time() - t0
                progress(i + 1, n_frames, (i + 1) / el if el else 0.0)
    except KeyboardInterrupt:
        print("\n  stopped; the partial file may not play")
    finally:
        if pool is not None:
            pool.terminate()
            pool.join()
        if feed is not None:
            feed.close()
        q.put(None)
        threads[0].join()
        proc.wait()
        threads[1].join(timeout=5)
    shutil.rmtree(frame_dir, ignore_errors=True)
    if progress is show:
        print()
    total = time.time() - t0
    if total > 5:
        slow = ("ffmpeg (decoding, laying in the panels and encoding)" if wait_ffmpeg > wait_draw
                else f"drawing the panels ({workers} worker{'s' if workers > 1 else ''})")
        print(f"  speed: {n_frames / total:.1f} fps at {out_w}x{out_h}; waited on drawing "
              f"{wait_draw / total:.0%}, on ffmpeg {wait_ffmpeg / total:.0%} -> the slow part is {slow}",
              flush=True)
    if cancel is not None and cancel.is_set():
        raise RenderError("render cancelled; the partial file may not play")
    if proc.returncode:
        tail = "".join(err)[-2000:]
        raise RenderError(f"ffmpeg failed (exit {proc.returncode}):\n{tail}")
    if progress is show:
        print(f"  done in {(time.time() - t0) / 60:.1f} min -> {out}")
