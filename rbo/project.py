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
      "layout": {"map": {"x": 0.38, "y": 0.62, "scale": 1.3}, "speed": {"visible": false}},
      "track": null,
      "sectors": 3
    }
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULTS = {"offset": None, "theme": "carbon", "units": "mph", "scale": 1.0,
            "layout": {}, "track": None, "sectors": 3, "session": None}


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
    return data


def save(video: str | Path, data: dict) -> Path:
    p = path_for(video)
    keep = {k: data.get(k, DEFAULTS[k]) for k in DEFAULTS}
    p.write_text(json.dumps(keep, indent=2) + "\n")
    return p
