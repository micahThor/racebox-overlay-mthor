"""Local web app for setting the sync point and editing the overlay layout.

    racebox-overlay gui                      (choose the files in the editor)
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
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import crewchief, project, sync, telemetry, timing, video
from .hud import Hud
from . import results as resultsmod
from .frame import frame_colour
from .themes import THEMES, load_theme
from .timing import fmt_time

HERE = Path(__file__).parent
RENDER_SLOT: threading.Semaphore | None = None      # the web version: one render at a time


class Studio:
    def __init__(self, video_path: str, session_path: str | None):
        self.video_path = str(Path(video_path).resolve())
        self.vinfo = video.probe(self.video_path)
        self.proj = project.load(self.video_path)
        session_path = session_path or self.proj.get("session")
        if not session_path:
            raise ValueError("choose the RaceBox file for this video too")
        self.proj["session"] = str(Path(session_path).resolve())
        self.tel = telemetry.load(self.proj["session"])
        self.tm = timing.Timing(self.tel, track=timing.load_track(self.proj.get("track")),
                                n_sectors=int(self.proj.get("sectors") or 3))
        self.lock = threading.Lock()
        self._huds: dict[str, Hud] = {}
        self.render_state: dict = {"state": "idle"}
        self._cancel: threading.Event | None = None
        self.autosync_state: dict = {"state": "idle"}
        self.last_used = time.time()
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

    def _ghost_src(self) -> dict | None:
        src = self.proj.get("ghost_source") or None
        return src if src and src.get("session") and src.get("video") and src.get("lap") is not None else None

    def _ghost_key(self) -> str:
        return json.dumps([bool(self.proj.get("ghost")), self._ghost_src(), self.proj.get("offset"),
                           self._traffic_s(), self._race_key()], sort_keys=True)

    def ghost_cfg(self):
        """The ghost lap (a ghost.GhostRef) or None; see ghost_error."""
        key = self._ghost_key()
        if getattr(self, "_gk", None) == key:
            return self._ghost
        from . import ghost as gh
        self._ghost, self.ghost_error, self.ghost_sync = None, None, None
        if self.proj.get("ghost"):
            src = self._ghost_src()
            if src:
                try:
                    ref, how = gh.external_ref(self.tm, src["session"], src["video"], int(src["lap"]),
                                               src.get("offset"))
                    self._ghost, self.ghost_sync = ref, how
                    print(f"ghost sync: lap {ref.number} of {Path(src['session']).name}, "
                          f"{Path(src['video']).name} synced by {how} "
                          f"({ref.offset_rel:.2f} s into its data). OK.", flush=True)
                    if src.get("offset") is None:          # keep the sync it found
                        src["offset"] = round(ref.offset_rel, 3)
                        self.proj["ghost_source"] = src
                        self.save()
                        key = self._ghost_key()
                except gh.GhostError as e:
                    self.ghost_error = str(e).removeprefix("ghost: ")
                    print(f"ghost sync FAILED: {self.ghost_error}", flush=True)
            else:
                off = self.t0 + float(self.proj["offset"])
                i = gh.choose_ref(self.tm, off, self.vinfo["duration"], avoid=self._avoid())
                if i is not None:
                    self._ghost = gh.own_ref(self.tm, i, off, self.vinfo["duration"], self.video_path,
                                             float(self.vinfo["fps"]),
                                             aspect=self.vinfo["height"] / max(self.vinfo["width"], 1))
        self._gk = key
        return self._ghost

    def ghost_laps(self, session: str) -> dict:
        from . import ghost as gh
        p = Path(session.strip().strip("'\"")).expanduser()
        if not p.exists():
            return {"error": f"can't find {p}"}
        try:
            return {"laps": gh.session_laps(str(p)), "session": str(p)}
        except Exception as e:  # noqa: BLE001
            return {"error": f"couldn't read {p.name}: {e}"}

    def _race_key(self):
        p = self.proj
        return [bool(p.get("race")), p.get("race_source"), str(p.get("race_number") or ""),
                p.get("heat_source") or "", p.get("qual_source") or ""]

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
                    try:              # the grid: the heat's results (a main) or qualifying (a heat)
                        grid = resultsmod.grid_for_video(p, self.video_path)
                    except (resultsmod.ResultsError, OSError, ValueError, KeyError):
                        grid = None
                    self._race = racemod.Race(self._race_data, self.tm, str(p["race_number"]),
                                              grid_order=grid)
                elif p.get("race"):
                    self.race_error = "pick your rider"
            except (racemod.RaceError, OSError, ValueError, KeyError) as e:
                self.race_error = str(e)
        elif p.get("race"):
            self.race_error = "paste your Speedhive session link"
        self._race_k = key
        return self._race

    def _results_key(self) -> list:
        p = self.proj
        return [p.get("qual_source") or "", p.get("heat_source") or "", str(p.get("race_number") or "")]

    def results(self):
        """An earlier session's results to show at the start (see results.py), or None."""
        key = json.dumps(self._results_key())
        if getattr(self, "_res_k", None) == key:
            return self._res
        self._res, self.results_error = None, None
        try:
            self._res = resultsmod.from_project(self.proj, self.video_path)
        except (resultsmod.ResultsError, OSError, ValueError, KeyError) as e:
            self.results_error = str(e)
        self._res_k = key
        return self._res

    def results_info(self) -> dict:
        r = self.results()
        if r is None:
            return {"error": getattr(self, "results_error", None)}
        return {"kind": r.kind, "title": r.title.title(), "riders": len(r.rows),
                "me": next((x["pos"] for x in r.rows if r.me and x["number"] == r.me), None)}

    def _strips(self) -> list:
        """The strips round the video (always all three; only their sizes change)."""
        return ["left", "right", "top", "bottom"]

    def info(self) -> dict:
        """The event name, track name, date and logos (see project.session_info)."""
        return project.session_info(self.tel, self.proj.get("info"))

    def canvas(self) -> dict:
        """The output frame at full size, with its strips (output pixels)."""
        from .hud import bottom_height, sidebar_width
        W, H = self.vinfo["width"], self.vinfo["height"]
        ss = self.proj.get("strip_size") or {}
        mult = lambda k: min(max(float(ss.get(k, 1.0) or 1.0), 0.1), 2.5)
        even = lambda v: max(2, int(round(v / 2)) * 2)
        sw = sidebar_width(W, H)
        lw, rw = even(sw * mult("left")), even(sw * mult("right"))
        th, bh = even(bottom_height(W, H) * mult("top")), even(bottom_height(W, H) * mult("bottom"))
        cw = W + lw + rw
        return {"w": cw, "h": th + H + bh, "vx": lw, "vy": th, "vw": W, "vh": H,
                "strips": {"left": [0, th, lw, H], "right": [lw + W, th, rw, H], "top": [0, 0, cw, th],
                           "bottom": [0, th + H, cw, bh]}}

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
                          self.proj.get("layout"), self._ghost_key(), self._race_key(),
                          bool(self.proj.get("race_class")), self.proj.get("race_field", True),
                          self._strips(), self.proj.get("strip_size"), self.info(),
                          self.proj.get("frame_color"), self.proj.get("strip_align"), self._results_key()],
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
                        race_field=self.proj.get("race_field", True) is not False, strips=self._strips(),
                        strip_size=self.proj.get("strip_size") or {}, info=self.info(),
                        frame_color=self.proj.get("frame_color"),
                        strip_align=self.proj.get("strip_align") or {}, results=self.results())
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
                      "has_gyro": self.has_gyro(), "gyro_kind": self._gyro_kind, "canvas": self.canvas()},
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
            "info": self.info(),
            "results": self.results_info(),
            "call_defaults": crewchief.DEFAULTS,
            "speech": {"available": crewchief.tts_backend() is not None,
                       "engine": crewchief.tts_backend(),
                       "voices": crewchief.list_voices(),
                       "default_voice": crewchief.default_voice()},
        }

    # ------------------------------------------------------------ crew chief

    def _calls(self) -> list:
        g = self.ghost_cfg()
        key = json.dumps([self.proj.get("units"), self.proj.get("call_settings"), self._ghost_key(),
                          self._race_key()], sort_keys=True)
        if getattr(self, "_calls_key", None) != key:
            self._calls_cache = crewchief.CrewChief.from_settings(
                self.tel, self.tm, units=self.proj.get("units") or "mph",
                settings=self.proj.get("call_settings"), ghost_ref=g,
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
        """What the editor shows about the ghost."""
        src = self._ghost_src()
        if src:
            g = self.ghost_cfg() if self.proj.get("ghost") else None
            out = {"external": True, "lap": src["lap"], "session": Path(src["session"]).name,
                   "video": Path(src["video"]).name, "offset": src.get("offset"),
                   "error": getattr(self, "ghost_error", None) if self.proj.get("ghost") else None}
            if g is not None:
                out.update(time=fmt_time(g.time), label=g.label, sync=self.ghost_sync,
                           fps=g.fps)
            return out
        from .ghost import choose_ref
        ref = choose_ref(self.tm, self.t0 + float(self.proj["offset"]), self.vinfo["duration"],
                         avoid=self._avoid())
        if ref is None:
            return None
        lap = self.tm.laps[ref]
        return {"external": False, "lap": lap.number, "time": fmt_time(lap.time)}

    def _panel_keys(self) -> list[dict]:
        names = []
        if self.tm.laps:
            names.append(("laps", "Lap timer & sectors"))
        racing = self.race() is not None
        names.append(("map", "Track map (in the race panel)" if racing else "Track map"))
        names.append(("speed", "Speed"))
        if self.proj.get("ghost"):
            names.append(("ghost", "Ghost lap"))
        if racing:
            names.append(("race", "Track map & standings"))
        names.append(("accel", "Throttle & brake"))
        if self.tel.is_bike:
            names.append(("gforce", "Lean angle"))
        elif self.tel.g_lat is not None:
            names.append(("gforce", "G-force"))
        info = self.info()
        for k, lbl in (("event_logo", "Event logo"), ("track_logo", "Track logo"), ("event", "Event name"),
                       ("track", "Track name"), ("date", "Date & time")):
            if str(info.get(k) or "").strip():
                names.append((k, lbl))
        inside = {"map"} if racing and (self.proj.get("layout") or {}).get("race", {}).get("visible") is not False \
            and (self.proj.get("layout") or {}).get("map", {}).get("visible") is not False else set()
        return [{"key": k, "label": lbl, "inside": k in inside} for k, lbl in names]

    def hud_frame(self, vt: float, pw: int) -> dict:
        W, H = self.vinfo["width"], self.vinfo["height"]
        cv = self.canvas()
        pw = int(pw * W / cv["w"])               # the client sends the whole frame's width
        pw = int(max(320, min(pw, W, 1920)))
        ph = int(round(pw * H / W))
        h = self.hud(pw, ph)
        self._frame_hud = h
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
        ghost = {"active": g.active, "vt": g.ref_vt, "gap": g.gap_m,
                 "src": "/ghost-video" if h.ghost is not None and h.ghost.external else "/video"}
        if slot and gp:
            ghost["slot"] = [slot[0], slot[1] - gp.ay, slot[2], slot[3]]   # relative to the panel
        return {"pw": h.canvas_w, "ph": h.canvas_h, "atlas_w": h.atlas_w, "atlas_h": h.atlas_h,
                "video_rect": [h.vid_x0, h.vid_y0, pw, ph],
                "side_colour": "#" + "".join(f"{v:02x}" for v in h.side_colour()),
                "frame_default": "#" + "".join(f"{v:02x}" for v in frame_colour(load_theme(self.proj.get("theme")))),
                "frame": f"/api/frame.png?k={id(h)}",
                "panels": [p for p in h.describe() if p["name"] != "none"],
                "atlas": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
                "readout": {"lap": lap_s, "speed": speed_s, "data_time": t - self.t0,
                            "in_data": inside},
                "ghost": ghost, "results": self._results_view(h, vt)}

    def _results_view(self, h, vt: float):
        """Where the results card goes on the preview, and whether it's showing
        (the first seconds after the render's start)."""
        r = h.results
        if r is None or not r.rows:
            return None
        start = float(((self.proj.get("trim") or {}).get("start")) or 0.0)
        x, y, w, hh, _ = h.results_rect(len(r.rows))
        return {"src": f"/api/results.png?k={id(h)}-{len(r.rows)}-{hash(json.dumps(r.rows, sort_keys=True, default=str)) & 0xffffff}",
                "rect": [x, y, w, hh],
                "show": 0 <= vt - start < resultsmod.RESULTS_SECONDS}

    def update_project(self, changes: dict) -> dict:
        allowed = {"offset", "theme", "units", "scale", "layout", "speed_max",
                   "crew_chief", "voice", "voice_rate", "call_settings", "ghost", "ghost_source", "trim",
                   "race", "race_source", "race_number", "race_class", "race_field", "strips", "strip_size", "info", "frame_color", "strip_align",
                   "qual_source", "heat_source"}
        for k, v in changes.items():
            if k in allowed:
                self.proj[k] = v
        if "offset" in changes:
            self.offset_source = "set by you"
        ghost = self._ghost_ref()          # may fill in the ghost video's sync
        return {"saved": self.save(), "offset": self.proj["offset"], "ghost_ref": ghost, "canvas": self.canvas(),
                "panels": self._panel_keys(), "race": self.race_info(), "info": self.info(),
                "results": self.results_info(),
                "project": {"ghost_source": self.proj.get("ghost_source")}}

    def sync_lap(self, n: int, vt: float) -> dict:
        lap = next((l for l in self.tm.laps if l.number == n), None)
        if lap is None:
            raise ValueError(f"no lap {n}")
        res = self.update_project({"offset": round(lap.t_start - self.t0 - vt, 3)})
        print(f"sync SET BY HAND: lap {n} starts at {vt:.2f} s in the video, so the video starts "
              f"{res['offset']:.2f} s into the RaceBox data. Saved.", flush=True)
        return res

    def has_gyro(self) -> bool:
        """Whether the video carries a usable gyro log, Insta360 or GoPro (cached)."""
        if not hasattr(self, "_has_gyro"):
            self._gyro_kind = sync.camera_gyro(self.video_path)
            self._has_gyro = self._gyro_kind is not None
        return self._has_gyro

    def auto_sync(self) -> dict:
        prior = None
        guess = self._clock_guess()
        if self.vinfo.get("created") is not None and guess:
            prior = self.t0 + guess
        name = Path(self.video_path).name
        how = ({"insta360": "the Insta360's gyro log", "gopro": "the GoPro's gyro log"}[self._gyro_kind]
               if self.has_gyro() else "motion in the picture (no gyro log found; slower)")
        print(f"\nauto-sync: matching {how} in {name} against the RaceBox"
              + (", camera clock as a rough anchor" if prior is not None else ""), flush=True)
        try:
            r = sync.auto_sync(self.video_path, self.tel, prior=prior)
        except sync.SyncError as e:
            print(f"\nauto-sync FAILED: {e}\n  Line up a lap by hand instead: pause on the start/finish line, "
                  "pick the lap, Set sync here.", flush=True)
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            print(f"\nauto-sync FAILED with an unexpected error: {e!r}", flush=True)
            raise
        res = self.update_project({"offset": round(r.offset - self.t0, 3)})
        self.offset_source = f"auto ({r.method})"
        lap1 = ""
        if self.tm.laps:
            lap1 = f", lap {self.tm.laps[0].number} starts at {self.tm.laps[0].t_start - r.offset:.1f} s in the video"
        if r.confident:
            print(f"\nauto-sync SUCCEEDED: video starts {res['offset']:.2f} s into the RaceBox data "
                  f"({r.method}, match {r.score:.2f}){lap1}. Saved.", flush=True)
        else:
            why = "weak match" if r.score < 0.5 else "other laps match almost as well"
            print(f"\nauto-sync FINISHED BUT NOT SURE ({why}): best match {res['offset']:.2f} s "
                  f"(match {r.score:.2f}){lap1}. Saved; check a lap start. Other candidates: "
                  + ", ".join(f"{o - self.t0:.2f} s" for o, _ in r.alternatives), flush=True)
        return {"ok": True, "offset": res["offset"], "score": r.score, "confident": r.confident,
                "method": r.method,
                "alternatives": [{"offset": round(o - self.t0, 3), "score": s} for o, s in r.alternatives]}

    def start_autosync(self) -> dict:
        """Auto-sync in the background (it can take a few minutes, longer than
        a web request may stay open); poll autosync_state for the answer."""
        if self.autosync_state.get("state") == "running":
            return self.autosync_state
        self.autosync_state = {"state": "running"}

        def work():
            try:
                r = self.auto_sync()
            except Exception as e:  # noqa: BLE001 - surfaced to the UI
                r = {"ok": False, "error": str(e)}
            self.autosync_state = dict(r, state="done")
        threading.Thread(target=work, daemon=True).start()
        return self.autosync_state

    # ------------------------------------------------------------ rendering

    def start_render(self, start: float | None, end: float | None) -> dict:
        if self.render_state.get("state") == "running":
            return self.render_state
        out = str(Path(self.video_path).with_name(Path(self.video_path).stem + "_overlay.mp4"))
        self._cancel = threading.Event()
        self.render_state = {"state": "running", "done": 0, "total": 0, "fps": 0, "out": out,
                             "name": Path(out).name}

        def work():
            slot = RENDER_SLOT
            try:
                if slot is not None:          # the web version renders one video at a time
                    while not slot.acquire(timeout=1):
                        self.render_state.update(stage="queued")
                        if self._cancel.is_set():
                            self.render_state.update(state="cancelled", stage=None)
                            slot = None
                            return
                    self.render_state.update(stage=None)
                h = Hud(self.tel, self.tm, self.vinfo["width"], self.vinfo["height"],
                        units=self.proj.get("units") or "mph", scale=float(self.proj.get("scale") or 1),
                        theme=self.proj.get("theme"), layout=self.proj.get("layout") or {},
                        speed_max=self.proj.get("speed_max"), ghost=self.ghost_cfg(),
                        race=self.race(), race_class=bool(self.proj.get("race_class")),
                        race_field=self.proj.get("race_field", True) is not False, strips=self._strips(),
                        strip_size=self.proj.get("strip_size") or {}, info=self.info(),
                        frame_color=self.proj.get("frame_color"),
                        strip_align=self.proj.get("strip_align") or {}, results=self.results())

                last = [-1]

                def prog(i, n, rate):
                    self.render_state.update(done=i, total=n, fps=rate, stage="frames")
                    pct = int(i / n * 20) * 5 if n else 0              # and a line in the Terminal every 5%
                    if pct != last[0]:
                        last[0] = pct
                        eta = (n - i) / rate / 60 if rate else 0
                        print(f"  rendering: {pct:3d}%  frame {i}/{n}  {rate:4.1f} fps  "
                              f"about {eta:.0f} min left", flush=True)

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
                if self._cancel.is_set():
                    self.render_state.update(state="cancelled", stage=None)
                    return
                if self.after_render:                # the web version: keep it for downloading
                    self.render_state.update(stage="saving")
                    self.render_state.update(self.after_render(out) or {})
                self.render_state.update(state="done", stage=None)
            except Exception as e:  # noqa: BLE001 - surfaced to the UI
                self.render_state.update(state="error", error=str(e))
            finally:
                if slot is not None:
                    slot.release()

        threading.Thread(target=work, daemon=True).start()
        return self.render_state

    after_render = None          # set by the web version: called with the finished file

    def render_marker(self) -> str:
        """Changes whenever the render's progress does."""
        st = self.render_state
        return f"{st.get('state')}|{st.get('stage')}|{st.get('done')}"

    def cancel_render(self) -> dict:
        if self._cancel:
            self._cancel.set()
        return self.render_state


# ---------------------------------------------------------------- opening files

RECENT = Path.home() / ".racebox-overlay" / "recent.json"
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".insv"}
SESSION_EXT = {".csv", ".vbo"}


def presets_api(b: dict, studio) -> dict:
    """Saved layouts: list them, save the open video's, get, delete, or pick the
    one new videos start from."""
    act = b.get("action", "list")
    try:
        if act == "save":
            if studio is None:
                raise ValueError("open a video first")
            project.save_preset(str(b.get("name", "")), studio.proj)
            print(f"saved layout {str(b.get('name', '')).strip()!r}", flush=True)
        elif act == "get":
            return {"settings": project.load_preset(str(b.get("name", "")))}
        elif act == "delete":
            project.delete_preset(str(b.get("name", "")))
        elif act == "default":
            project.set_default_preset(b.get("name") or None)
    except (OSError, ValueError) as e:
        return {"error": str(e)}
    return {"presets": project.list_presets(), "default": project.default_preset()}


def recent() -> list[dict]:
    """Videos opened before that still exist, newest first, with their saved session."""
    try:
        items = json.loads(RECENT.read_text())
    except (OSError, ValueError):
        return []
    out = []
    for v in items:
        if Path(v).exists():
            proj = project.load(v)
            sess = proj.get("session")
            out.append({"video": v, "name": Path(v).name, "folder": str(Path(v).parent),
                        "session": sess if sess and Path(sess).exists() else None})
    return out[:8]


def remember(video_path: str) -> None:
    try:
        items = [v for v in json.loads(RECENT.read_text()) if v != video_path] if RECENT.exists() else []
    except (OSError, ValueError):
        items = []
    try:
        RECENT.parent.mkdir(parents=True, exist_ok=True)
        RECENT.write_text(json.dumps([video_path] + items[:19]))
    except OSError:
        pass


def pick_file(kind: str, start_dir: str | None = None) -> dict:
    """Show the computer's own file picker (the editor runs on this computer,
    so the chosen file's full path can be used directly)."""
    prompt = {"video": "Choose your video", "session": "Choose the RaceBox file (.csv or .vbo)",
              "ghost_video": "Choose the ghost lap's video",
              "ghost_session": "Choose the ghost lap's RaceBox file",
              "logo": "Choose a logo image (PNG with a clear background works best)"}.get(kind, "Choose a file")
    folder = start_dir if start_dir and Path(start_dir).is_dir() else None
    if sys.platform == "darwin":
        types = ' of type {"public.movie"}' if kind.endswith("video") else ' of type {"public.image"}' if kind == "logo" else ""
        loc = ' default location (POSIX file "{}")'.format(folder.replace('"', '\\"')) if folder else ""
        script = f'POSIX path of (choose file with prompt "{prompt}"{types}{loc})'
        # "activate" brings the dialog in front of the browser
        r = subprocess.run(["osascript", "-e", "activate", "-e", script], capture_output=True, text=True)
        if r.returncode != 0:
            if "-128" in r.stderr or "cancel" in r.stderr.lower():
                return {"cancelled": True}
            return {"error": r.stderr.strip() or "the file picker didn't open"}
        return {"path": r.stdout.strip()}
    try:                                    # elsewhere: Tk's file dialog, if Python has it
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        ft = [("Video", "*.mp4 *.mov *.m4v *.insv")] if kind.endswith("video") else \
            [("Image", "*.png *.jpg *.jpeg *.webp")] if kind == "logo" else [("RaceBox", "*.csv *.vbo")]
        path = filedialog.askopenfilename(title=prompt, initialdir=folder or os.getcwd(), filetypes=ft + [("All", "*")])
        root.destroy()
        return {"path": path} if path else {"cancelled": True}
    except Exception:  # noqa: BLE001
        return {"error": "no file picker available here; paste the file's path instead"}


def open_files(video_path: str, session_path: str | None) -> Studio:
    v = Path(str(video_path).strip().strip("'\"")).expanduser()
    if not v.exists():
        raise ValueError(f"can't find the video {v}")
    if session_path:
        s_ = Path(str(session_path).strip().strip("'\"")).expanduser()
        if not s_.exists():
            raise ValueError(f"can't find the RaceBox file {s_}")
        session_path = str(s_)
    st = Studio(str(v), session_path)
    st.save()
    remember(st.video_path)
    print(f"\nopened {Path(st.video_path).name} with {Path(st.proj['session']).name}"
          f"\nsettings save to {project.path_for(st.video_path)}", flush=True)
    return st


class Handler(BaseHTTPRequestHandler):
    studio: Studio | None = None
    page_config = "null"          # the web version tells the page about itself here

    def log_message(self, *a):
        pass

    # the web version overrides these: one open video per person, files in their folder
    def set_studio(self, st: Studio) -> None:
        Handler.studio = st

    def recent_list(self) -> list[dict]:
        return recent()

    def open_studio(self, video_path: str, session_path: str | None) -> Studio:
        return open_files(video_path, session_path)

    def peek(self, video_path: str) -> dict:
        v = Path(str(video_path).strip()).expanduser()
        sess = project.load(v).get("session") if v.exists() else None
        return {"session": sess if sess and Path(sess).exists() else None}

    def check_project(self, changes: dict) -> dict:
        return changes

    def check_path(self, path: str) -> str:
        return path

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
                body = (HERE / "gui.html").read_text().replace(
                    "/*RBO_CONFIG*/null", self.page_config).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/video":
                self._send_video()
            elif u.path == "/ghost-video":
                src = self.studio._ghost_src()
                if src:
                    self._send_video(src["video"])
                else:
                    self.send_error(404)
            elif u.path == "/api/ghost-laps":
                self._json(self.studio.ghost_laps(self.check_path(q.get("session", [""])[0])))
            elif u.path == "/api/state":
                st = self.studio.state() if self.studio else {"needs_files": True}
                st["recent"] = self.recent_list()
                self._json(st)
            elif self.studio is None and u.path.startswith(("/api/", "/video", "/ghost-video")):
                self._json({"error": "no video open yet"}, HTTPStatus.CONFLICT)
            elif u.path == "/api/hud":
                self._json(self.studio.hud_frame(float(q["vt"][0]), int(float(q.get("pw", ["960"])[0]))))
            elif u.path == "/api/results.png":     # the results card, for the preview
                h = getattr(self.studio, "_frame_hud", None)
                if h is None or h.results is None:
                    self.send_error(404)
                    return
                buf = io.BytesIO()
                h.results_card(h.results).save(buf, "PNG", compress_level=1)
                body = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")     # never a stale board
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/frame.png":       # the themed frame round the video, for the preview
                h = getattr(self.studio, "_frame_hud", None)
                if h is None:
                    self.send_error(404)
                    return
                buf = io.BytesIO()
                h.frame_image().save(buf, "PNG", compress_level=1)
                body = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "max-age=3600")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/render":
                # ?wait=1&since=N: hold the answer (up to 5 s) until the render moves
                # on, so the page keeps up without timers, which browsers slow
                # right down in a tab that's in the background
                if q.get("wait"):
                    since = q.get("since", [""])[0]
                    stop = time.time() + 5
                    while time.time() < stop and self.studio.render_marker() == since:
                        time.sleep(0.25)
                self._json(dict(self.studio.render_state, marker=self.studio.render_marker()))
            elif u.path == "/api/autosync":     # ?wait=1: hold the answer (up to 20 s) until it's done
                stop = time.time() + (20 if q.get("wait") else 0)
                while time.time() < stop and self.studio.autosync_state.get("state") == "running":
                    time.sleep(0.25)
                self._json(self.studio.autosync_state)
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
            if u.path == "/api/presets":         # saved layouts (they don't need a video open)
                self._json(presets_api(b, s))
                return
            if u.path == "/api/pick":
                self._json(pick_file(b.get("kind", "video"), b.get("dir")))
                return
            if u.path == "/api/open":
                try:
                    if self.studio and self.studio.render_state.get("state") == "running":
                        raise ValueError("wait for the render to finish (or cancel it) before opening other files")
                    self.set_studio(self.open_studio(b["video"], b.get("session")))
                    self._json({"ok": True})
                except Exception as e:  # noqa: BLE001 - shown in the open screen
                    self._json({"ok": False, "error": str(e)})
                return
            if u.path == "/api/peek":           # what's saved for a video, before opening it
                self._json(self.peek(str(b.get("video", ""))))
                return
            if s is None:
                self._json({"error": "no video open yet"}, HTTPStatus.CONFLICT)
                return
            if u.path == "/api/project":
                self._json(s.update_project(self.check_project(b)))
            elif u.path == "/api/sync-lap":
                self._json(s.sync_lap(int(b["lap"]), float(b["vt"])))
            elif u.path == "/api/autosync":
                self._json(s.start_autosync())
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

    def _send_video(self, path: str | None = None, attachment: str | None = None):
        path = path or self.studio.video_path
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
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
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


def serve(video_path: str | None, session_path: str | None, port: int = 8765,
          open_browser: bool = True) -> None:
    studio = None
    if video_path:
        try:
            studio = open_files(video_path, session_path)
        except ValueError as e:
            print(f"{e}; choose the files in the editor instead")
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
    if studio is None:
        print("choose your video and RaceBox file in the editor")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
