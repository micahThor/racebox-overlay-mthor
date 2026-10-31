"""Local web app for setting the sync point and editing the overlay layout.

    racebox-overlay gui VIDEO.mp4 SESSION.csv

Starts a small server on 127.0.0.1 and opens the editor in your browser. The
browser plays the actual video file; the overlay panels are drawn by the same
code that renders the final video, so what you see is what you get.
Everything you change is saved to VIDEO.mp4.overlay.json, which `render`
reads automatically.
"""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import re
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import crewchief, project, sync, telemetry, timing, video
from .hud import Hud
from .themes import THEMES
from .timing import fmt_time

HERE = Path(__file__).parent


class Studio:
    def __init__(self, video_path: str, session_path: str | None):
        self.video_path = str(Path(video_path).resolve())
        self.vinfo = video.probe(self.video_path)
        self.proj = project.load(self.video_path)
        session_path = session_path or self.proj.get("session")
        if not session_path:
            sys.exit("give the RaceBox session file too:  racebox-overlay gui VIDEO SESSION.csv")
        self.proj["session"] = str(Path(session_path).resolve())
        self.tel = telemetry.load(self.proj["session"])
        self.tm = timing.Timing(self.tel, track=timing.load_track(self.proj.get("track")),
                                n_sectors=int(self.proj.get("sectors") or 3))
        self.lock = threading.Lock()
        self._huds: dict[str, Hud] = {}
        self.render_state: dict = {"state": "idle"}
        self._cancel: threading.Event | None = None
        if self.proj.get("offset") is None:
            self.proj["offset"] = self._clock_guess()
            self.offset_source = "camera clock" if self.proj["offset"] else "none yet"
        else:
            self.offset_source = "saved"

    # ------------------------------------------------------------ helpers

    @property
    def t0(self) -> float:
        return float(self.tel.t[0])

    def _clock_guess(self) -> float:
        from .cli import _auto_offset
        created = self.vinfo.get("created")
        if created is None:
            return 0.0
        off, how = _auto_offset(created, self.tel, self.vinfo["duration"])
        return 0.0 if "doesn't match" in how else round(off - self.t0, 3)

    def radio_style(self) -> str:
        from .themes import load_theme
        try:
            return load_theme(self.proj.get("theme")).get("radio", "classic")
        except ValueError:
            return "classic"

    def save(self) -> str:
        return str(project.save(self.video_path, self.proj))

    def ghost_cfg(self) -> dict | None:
        if not self.proj.get("ghost"):
            return None
        from .ghost import choose_ref
        off = self.t0 + float(self.proj["offset"])
        ref = choose_ref(self.tm, off, self.vinfo["duration"], avoid=self._avoid())
        return None if ref is None else {"ref": ref, "offset": off, "duration": self.vinfo["duration"]}

    def _race_key(self):
        p = self.proj
        return [bool(p.get("race")), p.get("race_source"), str(p.get("race_number") or "")]

    def race(self):
        """The Race when race mode is on and loads, else None (see race_error)."""
        from . import race as racemod
        key = json.dumps(self._race_key())
        if getattr(self, "_race_k", None) == key:
            return self._race
        self._race, self.race_error, self._race_data = None, None, None
        p = self.proj
        if p.get("race_source"):
            try:
                cache = Path(self.video_path).with_name(Path(self.video_path).name + ".race.json")
                self._race_data = racemod.load(p["race_source"], cache)
                if p.get("race") and p.get("race_number"):
                    self._race = racemod.Race(self._race_data, self.tm, str(p["race_number"]))
                elif p.get("race"):
                    self.race_error = "pick your rider"
            except (racemod.RaceError, OSError, ValueError, KeyError) as e:
                self.race_error = str(e)
        elif p.get("race"):
            self.race_error = "paste your Speedhive session link"
        self._race_k = key
        return self._race

    def _traffic_s(self) -> float:
        return float((self.proj.get("call_settings") or {}).get("traffic_s", crewchief.DEFAULTS["traffic_s"]))

    def _avoid(self):
        r = self.race()
        return r.traffic_laps(self._traffic_s()) if r is not None else None

    def race_info(self) -> dict:
        r = self.race()
        data = getattr(self, "_race_data", None)
        riders = []
        if data:
            riders = [{"number": str(x.get("startNumber", "")).strip(), "name": " ".join(str(x.get("name", "")).split()),
                       "cls": str(x.get("resultClass", "")).strip(), "pos": x.get("position")}
                      for x in sorted(data["classification"]["rows"], key=lambda x: x["position"])]
        out = {"error": self.race_error, "riders": riders, "loaded": r is not None}
        if r is not None:
            out.update(me={"number": r.me.number, "name": r.me.name, "cls": r.me.cls,
                           "grid": r.me.grid, "finish": r.me.finish_pos},
                       laps=r.total_laps, aligned=r.aligned_laps, align_error=round(r.align_error, 3),
                       summary=r.summary().splitlines(),
                       start_vt=r.start - self.t0 - float(self.proj["offset"]),
                       lap_traffic=[round(x, 2) for x in r.clean_laps(self._traffic_s())])
        return out

    def hud(self, width: int, height: int) -> Hud:
        key = json.dumps([width, height, self._traffic_s(), self.proj.get("theme"), self.proj.get("units"),
                          self.proj.get("scale"), self.proj.get("speed_max"),
                          self.proj.get("layout"), self.ghost_cfg(), self._race_key(),
                          bool(self.proj.get("race_class")), self.proj.get("race_field", True)],
                         sort_keys=True)
        with self.lock:
            h = self._huds.get(key)
            if h is None:
                if len(self._huds) > 6:
                    self._huds.clear()
                h = Hud(self.tel, self.tm, width, height, units=self.proj.get("units") or "mph",
                        scale=float(self.proj.get("scale") or 1.0), theme=self.proj.get("theme"),
                        layout=self.proj.get("layout") or {}, speed_max=self.proj.get("speed_max"),
                        ghost=self.ghost_cfg(), race=self.race(),
                        race_class=bool(self.proj.get("race_class")),
                        race_field=self.proj.get("race_field", True) is not False)
                self._huds[key] = h
            return h

    # ------------------------------------------------------------ API

    def state(self) -> dict:
        v = self.vinfo
        laps = [{"n": l.number, "start": l.t_start - self.t0, "time": l.time,
                 "time_str": fmt_time(l.time),
                 "sectors": [fmt_time(s) for s in l.sectors] if l.sectors else None}
                for l in self.tm.laps]
        best = min(self.tm.laps, key=lambda l: l.time).number if self.tm.laps else None
        return {
            "video": {"name": Path(self.video_path).name, "width": v["width"], "height": v["height"],
                      "fps": float(v["fps"]), "duration": v["duration"],
                      "has_gyro": sync.has_trailer(self.video_path)},
            "data": {"name": Path(self.proj["session"]).name, "duration": self.tel.duration,
                     "laps": laps, "best": best, "bike": self.tel.is_bike,
                     "panels": self._panel_keys()},
            "project": self.proj,
            "project_file": str(project.path_for(self.video_path)),
            "offset_source": self.offset_source,
            "ghost_ref": self._ghost_ref(),
            "themes": [{"id": k, "label": t.get("label", k.title()),
                        "swatch": [list(t.get(c, THEMES["carbon"][c])) for c in ("bg", "text", "accent")]}
                       for k, t in THEMES.items()],
            "call_kinds": crewchief.KINDS,
            "race_kinds": __import__("rbo.race", fromlist=["RACE_KINDS"]).RACE_KINDS,
            "race": self.race_info(),
            "call_defaults": crewchief.DEFAULTS,
            "speech": {"available": crewchief.tts_backend() is not None,
                       "engine": crewchief.tts_backend(),
                       "voices": crewchief.list_voices(),
                       "default_voice": crewchief.default_voice()},
        }

    # ------------------------------------------------------------ crew chief

    def _calls(self) -> list:
        g = self.ghost_cfg()
        key = json.dumps([self.proj.get("units"), self.proj.get("call_settings"), g, self._race_key()],
                         sort_keys=True)
        if getattr(self, "_calls_key", None) != key:
            self._calls_cache = crewchief.CrewChief.from_settings(
                self.tel, self.tm, units=self.proj.get("units") or "mph",
                settings=self.proj.get("call_settings"), ghost_ref=g["ref"] if g else None,
                race=self.race()).calls()
            self._calls_key = key
        return self._calls_cache

    def calls(self) -> list[dict]:
        off = self.t0 + float(self.proj["offset"])
        D = self.vinfo["duration"]
        return [{"i": i, "vt": c.t - off, "text": c.text, "kind": c.kind}
                for i, c in enumerate(self._calls()) if 0 <= c.t - off <= D]

    def call_audio(self, i: int) -> bytes:
        c = self._calls()[i]
        audio = crewchief.radioize(crewchief.synthesize(
            c.text, self.proj.get("voice") or crewchief.default_voice(),
            int(self.proj.get("voice_rate") or 190)), style=self.radio_style())
        buf = io.BytesIO()
        import wave
        pcm = (audio.clip(-1, 1) * 32767).astype("<i2")
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(crewchief.RATE)
            w.writeframes(pcm.tobytes())
        return buf.getvalue()

    def _ghost_ref(self) -> dict | None:
        from .ghost import choose_ref
        ref = choose_ref(self.tm, self.t0 + float(self.proj["offset"]), self.vinfo["duration"],
                         avoid=self._avoid())
        if ref is None:
            return None
        lap = self.tm.laps[ref]
        return {"lap": lap.number, "time": fmt_time(lap.time)}

    def _panel_keys(self) -> list[dict]:
        names = []
        if self.tm.laps:
            names.append(("laps", "Lap timer & sectors"))
        names.append(("map", "Track map"))
        names.append(("speed", "Speed"))
        if self.proj.get("ghost"):
            names.append(("ghost", "Ghost lap"))
        if self.race() is not None:
            names.append(("race", "Race standings"))
        if self.tel.is_bike:
            names.append(("gforce", "Lean angle"))
        elif self.tel.g_lat is not None:
            names.append(("gforce", "G-force"))
        return [{"key": k, "label": lbl} for k, lbl in names]

    def hud_frame(self, vt: float, pw: int) -> dict:
        W, H = self.vinfo["width"], self.vinfo["height"]
        pw = int(max(320, min(pw, W, 1920)))
        ph = int(round(pw * H / W))
        h = self.hud(pw, ph)
        t = vt + self.t0 + float(self.proj["offset"])
        img = h.render_atlas(t)
        buf = io.BytesIO()
        img.save(buf, "PNG", compress_level=1)
        st = self.tm.state(t)
        speed = float(self.tel.at("speed_kmh", t))
        if (self.proj.get("units") or "mph") == "mph":
            speed_s = f"{speed * 0.621371:.0f} mph"
        else:
            speed_s = f"{speed:.0f} km/h"
        if st.phase == "lap":
            lap_s = f"Lap {st.lap_number}  {fmt_time(st.elapsed)}"
        else:
            lap_s = {"pre": "Out lap", "post": "In lap", "none": "No laps"}[st.phase]
        inside = self.t0 <= t <= float(self.tel.t[-1])
        g = h.ghost_info(t)
        slot = h.ghost_slot()
        gp = next((p for p in h.panels if p.name == "ghost"), None)
        ghost = {"active": g.active, "vt": g.ref_vt, "gap": g.gap_m}
        if slot and gp:
            ghost["slot"] = [slot[0], slot[1] - gp.ay, slot[2], slot[3]]   # relative to the panel
        return {"pw": pw, "ph": ph, "atlas_w": h.atlas_w, "atlas_h": h.atlas_h,
                "panels": [p for p in h.describe() if p["name"] != "none"],
                "atlas": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
                "readout": {"lap": lap_s, "speed": speed_s, "data_time": t - self.t0,
                            "in_data": inside},
                "ghost": ghost}

    def update_project(self, changes: dict) -> dict:
        allowed = {"offset", "theme", "units", "scale", "layout", "speed_max",
                   "crew_chief", "voice", "voice_rate", "call_settings", "ghost",
                   "race", "race_source", "race_number", "race_class", "race_field"}
        for k, v in changes.items():
            if k in allowed:
                self.proj[k] = v
        if "offset" in changes:
            self.offset_source = "set by you"
        return {"saved": self.save(), "offset": self.proj["offset"], "ghost_ref": self._ghost_ref(),
                "panels": self._panel_keys(), "race": self.race_info()}

    def sync_lap(self, n: int, vt: float) -> dict:
        lap = next((l for l in self.tm.laps if l.number == n), None)
        if lap is None:
            raise ValueError(f"no lap {n}")
        return self.update_project({"offset": round(lap.t_start - self.t0 - vt, 3)})

    def auto_sync(self) -> dict:
        prior = None
        guess = self._clock_guess()
        if self.vinfo.get("created") is not None and guess:
            prior = self.t0 + guess
        try:
            r = sync.auto_sync(self.video_path, self.tel, prior=prior)
        except sync.SyncError as e:
            return {"ok": False, "error": str(e)}
        res = self.update_project({"offset": round(r.offset - self.t0, 3)})
        self.offset_source = f"auto ({r.method})"
        return {"ok": True, "offset": res["offset"], "score": r.score, "confident": r.confident,
                "method": r.method,
                "alternatives": [{"offset": round(o - self.t0, 3), "score": s} for o, s in r.alternatives]}

    # ------------------------------------------------------------ rendering

    def start_render(self, start: float | None, end: float | None) -> dict:
        if self.render_state.get("state") == "running":
            return self.render_state
        out = str(Path(self.video_path).with_name(Path(self.video_path).stem + "_overlay.mp4"))
        self._cancel = threading.Event()
        self.render_state = {"state": "running", "done": 0, "total": 0, "fps": 0, "out": out}

        def work():
            try:
                h = Hud(self.tel, self.tm, self.vinfo["width"], self.vinfo["height"],
                        units=self.proj.get("units") or "mph", scale=float(self.proj.get("scale") or 1),
                        theme=self.proj.get("theme"), layout=self.proj.get("layout") or {},
                        speed_max=self.proj.get("speed_max"), ghost=self.ghost_cfg(),
                        race=self.race(), race_class=bool(self.proj.get("race_class")),
                        race_field=self.proj.get("race_field", True) is not False)

                def prog(i, n, rate):
                    self.render_state.update(done=i, total=n, fps=rate, stage="frames")

                off = self.t0 + float(self.proj["offset"])
                s0, s1 = start or 0.0, end or self.vinfo["duration"]
                radio = None
                if self.proj.get("crew_chief"):
                    import tempfile
                    radio = str(Path(tempfile.mkdtemp()) / "crew_chief.wav")

                    def vprog(i, n):
                        self.render_state.update(done=i, total=n, stage="voice")
                    track, placed = crewchief.build_track(
                        self._calls(), s0 + off, s1 + off,
                        voice=self.proj.get("voice") or crewchief.default_voice(),
                        rate=int(self.proj.get("voice_rate") or 190), progress=vprog,
                        style=self.radio_style())
                    crewchief.write_wav(radio, track)
                    self.render_state["calls"] = len(placed)
                video.render(self.video_path, h, off, out, s0, end, progress=prog,
                             cancel=self._cancel, radio=radio)
                self.render_state["state"] = "done"
            except Exception as e:  # noqa: BLE001 - surfaced to the UI
                self.render_state.update(state="error", error=str(e))

        threading.Thread(target=work, daemon=True).start()
        return self.render_state

    def cancel_render(self) -> dict:
        if self._cancel:
            self._cancel.set()
        return self.render_state


class Handler(BaseHTTPRequestHandler):
    studio: Studio

    def log_message(self, *a):
        pass

    def _json(self, obj, status=HTTPStatus.OK):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                body = (HERE / "gui.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/video":
                self._send_video()
            elif u.path == "/api/state":
                self._json(self.studio.state())
            elif u.path == "/api/hud":
                self._json(self.studio.hud_frame(float(q["vt"][0]), int(float(q.get("pw", ["960"])[0]))))
            elif u.path == "/api/render":
                self._json(self.studio.render_state)
            elif u.path == "/api/calls":
                self._json(self.studio.calls())
            elif u.path == "/api/call-audio":
                body = self.studio.call_audio(int(q["i"][0]))
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            b = self._body()
            s = self.studio
            if u.path == "/api/project":
                self._json(s.update_project(b))
            elif u.path == "/api/sync-lap":
                self._json(s.sync_lap(int(b["lap"]), float(b["vt"])))
            elif u.path == "/api/autosync":
                self._json(s.auto_sync())
            elif u.path == "/api/render":
                self._json(s.start_render(b.get("start"), b.get("end")))
            elif u.path == "/api/render/cancel":
                self._json(s.cancel_render())
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)

    def _send_video(self):
        path = self.studio.video_path
        size = Path(path).stat().st_size
        ctype = mimetypes.guess_type(path)[0] or "video/mp4"
        if not ctype.startswith("video/"):
            ctype = "video/mp4"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):
                    start = max(size - int(m.group(2)), 0)
            if start > end or start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


def serve(video_path: str, session_path: str | None, port: int = 8765,
          open_browser: bool = True) -> None:
    studio = Studio(video_path, session_path)
    Handler.studio = studio
    httpd = None
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        sys.exit(f"no free port between {port} and {port + 19}")
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"editor running at {url}  (Ctrl-C to stop)")
    print(f"settings save to {project.path_for(studio.video_path)}")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
