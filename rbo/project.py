"""Per-video settings, saved next to the video as <video>.overlay.json.

The GUI writes it; `render` and `preview` read it, so a sync point, theme and
layout set once are reused without retyping options. Command-line options
still win over anything in the file.

    {
      "session": "/path/to/session.csv",
      "offset": 12.54,              video starts this many seconds into the data
      "theme": "carbon",            built-in name or path to a theme JSON
      "units": "mph",
      "scale": 1.0,
      "speed_max": 70,              top of the speedometer (null = 70 mph / 120 km/h)
      "layout": {"map": {"x": 0.38, "y": 0.62, "scale": 1.3}, "speed": {"visible": false}},
      "track": null,
      "sectors": 3,
      "trim": {"start": 85.0, "end": 460.0},   part of the video to render (seconds; null = all)
      "ghost": true,                picture-in-picture of your best lap, matched by position
      "ghost_source": {             optional: the ghost lap comes from another session
        "session": "/path/other.csv", "video": "/path/other.mp4", "lap": 4,
        "offset": 31.2              that video's sync (seconds into its data)
      },
      "crew_chief": true,           add spoken crew chief calls to the render
      "voice": "Daniel",            a macOS voice (null = Daniel if installed)
      "voice_rate": 190,            words per minute
      "call_settings": {            crew chief sensitivity (see rbo/crewchief.py DEFAULTS)
        "wide_m": 3.5, "early_brake_m": 15, "slow_corner_kmh": 8,
        "min_loss": 0.15, "max_corner_calls": 2, "consistency_s": 0.35,
        "off": ["topspeed"]
      },
      "race": true,                 race mode: standings tower and race calls
      "race_source": "https://speedhive.mylaps.com/sessions/12935135",
      "race_number": "96",          your start number in that race
      "race_class": false,          tower shows only your class
      "race_field": true,           the other riders as dots on the map
      "strip_size": {"left": 1.0, "right": 1.4, "bottom": 0.8},   the frame round the video
                                    (left, right and bottom strips, always on); a panel goes
                                    in one with "where": "left"/"right"/"bottom" ("video" = on it)
      "info": {"event": "SFL MiniGP Round 6", "track": "...", "date": "...",
               "event_logo": "/path/logo.png", "track_logo": "/path/track.png"}
                                    track and date start from the RaceBox file; "" = off
    }
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULTS = {"offset": None, "theme": "carbon", "units": "mph", "scale": 1.0,
            "speed_max": None, "layout": {}, "track": None, "sectors": 3, "session": None,
            "crew_chief": False, "voice": None, "voice_rate": 190, "call_settings": {},
            "ghost": False, "race": False, "race_source": None, "race_number": None,
            "race_class": False, "race_field": True, "ghost_source": None,
            "trim": None, "race_side": None, "side": None, "strips": [],
            "strip_size": {}, "info": None}


def path_for(video: str | Path) -> Path:
    v = Path(video)
    return v.with_name(v.name + ".overlay.json")


def load(video: str | Path) -> dict:
    p = path_for(video)
    data = dict(DEFAULTS)
    data["layout"] = {}
    if p.exists():
        try:
            data.update(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            pass
    # older settings: one race-only side panel, or a single "side"
    old = data.get("race_side") if data.get("race_side") in ("left", "right") else None
    if old and not data.get("strips"):
        layout = dict(data.get("layout") or {})
        layout["race"] = dict(layout.get("race", {}), where=old)
        data["layout"] = layout
        data["strips"] = [old]
    if data.get("side") in ("left", "right", "bottom") and not data.get("strips"):
        data["strips"] = [data["side"]]
    data["race_side"] = data["side"] = None
    data["strips"] = [st for st in ("left", "right", "bottom") if st in (data.get("strips") or [])]
    return data


def save(video: str | Path, data: dict) -> Path:
    p = path_for(video)
    keep = {k: data.get(k, DEFAULTS[k]) for k in DEFAULTS}
    p.write_text(json.dumps(keep, indent=2) + "\n")
    return p


INFO_KEYS = ("event", "track", "date", "event_logo", "track_logo")


def session_info(tel, saved: dict | None) -> dict:
    """The event name, track name, date and logos to show. The track name and the
    date start from the RaceBox file (its track, and when the session started in
    this computer's time zone) until they're set; an empty one is off."""
    from datetime import datetime
    saved = saved or {}
    when = datetime.fromtimestamp(float(tel.t[0]))
    defaults = {"track": str(tel.meta.get("Track", "")).strip(),
                "date": f"{when:%b} {when.day}, {when.year} · {when.hour % 12 or 12}:{when:%M %p}"}
    return {k: (defaults.get(k, "") if saved.get(k) is None else saved[k]) for k in INFO_KEYS}
