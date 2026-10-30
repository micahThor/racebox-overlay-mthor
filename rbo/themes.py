"""Overlay colour themes.

Use a built-in by name (--theme neon) or a JSON file with any of the keys
below; missing keys come from "carbon". Colours are [r, g, b] or [r, g, b, a].

    {
      "base": "carbon",            optional: which built-in to start from
      "bg": [10, 12, 16, 200],     card fill (alpha 0 = no cards)
      "edge": [255, 255, 255, 40], card outline (alpha 0 = none)
      "text": [245, 247, 250],     main text
      "dim": [150, 156, 168],      labels
      "accent": [255, 92, 40],     position dot, speed bar, G dot
      "purple": ..., "green": ..., "yellow": ..., "red": ...   timing colours
      "line": [230, 233, 240, 235],   best-lap line on the map
      "ring": [255, 255, 255],        outline around the position dot
      "radius": 18,                card corner radius (at 1080p)
      "stroke": 0,                 text outline width (at 1080p), for card-less themes
      "stroke_color": [0, 0, 0, 200],
      "font": "/path/to/font.ttf"  optional
    }
"""
from __future__ import annotations

import json
from pathlib import Path

THEMES: dict[str, dict] = {
    "carbon": {
        "label": "Carbon",
        "bg": (10, 12, 16, 200), "edge": (255, 255, 255, 40),
        "text": (245, 247, 250, 255), "dim": (150, 156, 168, 255),
        "accent": (255, 92, 40, 255),
        "purple": (178, 92, 255, 255), "green": (40, 205, 110, 255),
        "yellow": (245, 196, 30, 255), "red": (235, 64, 52, 255),
        "line": (230, 233, 240, 235), "ring": (255, 255, 255, 255),
        "radius": 18, "stroke": 0, "stroke_color": (0, 0, 0, 200),
    },
    "daylight": {
        "label": "Daylight",
        "bg": (246, 247, 249, 225), "edge": (0, 0, 0, 30),
        "text": (18, 20, 26, 255), "dim": (98, 104, 116, 255),
        "accent": (226, 60, 20, 255),
        "purple": (126, 46, 214, 255), "green": (12, 150, 70, 255),
        "yellow": (196, 136, 0, 255), "red": (214, 40, 30, 255),
        "line": (30, 34, 44, 230), "ring": (18, 20, 26, 255),
        "radius": 14,
    },
    "neon": {
        "label": "Neon",
        "bg": (2, 6, 14, 185), "edge": (0, 229, 255, 150),
        "text": (232, 252, 255, 255), "dim": (110, 190, 210, 255),
        "accent": (0, 229, 255, 255),
        "purple": (214, 92, 255, 255), "green": (60, 255, 140, 255),
        "yellow": (255, 230, 60, 255), "red": (255, 60, 110, 255),
        "line": (0, 229, 255, 230), "ring": (232, 252, 255, 255),
        "radius": 4,
    },
    "minimal": {
        "label": "Minimal",
        "bg": (0, 0, 0, 0), "edge": (0, 0, 0, 0),
        "text": (255, 255, 255, 255), "dim": (225, 228, 235, 255),
        "accent": (255, 214, 0, 255),
        "line": (255, 255, 255, 220), "ring": (0, 0, 0, 200),
        "stroke": 3, "stroke_color": (0, 0, 0, 210),
    },
}

DEFAULT = "carbon"


def _rgba(v):
    v = tuple(int(x) for x in v)
    return v if len(v) == 4 else v + (255,)


def load_theme(spec: str | dict | None) -> dict:
    """A built-in name, a path to a JSON file, or a dict of overrides."""
    if spec is None or spec == "":
        spec = DEFAULT
    if isinstance(spec, str):
        if spec.lower() in THEMES:
            over = {"base": spec.lower()}
        elif Path(spec).expanduser().exists():
            over = json.loads(Path(spec).expanduser().read_text())
        else:
            raise ValueError(f"unknown theme {spec!r}; built-ins: {', '.join(THEMES)}")
    else:
        over = dict(spec)
    base = dict(THEMES[DEFAULT])
    base.update(THEMES.get(str(over.get("base", DEFAULT)).lower(), {}))
    base.update({k: v for k, v in over.items() if k != "base"})
    for k, v in base.items():
        if isinstance(v, (list, tuple)) and len(v) in (3, 4):
            base[k] = _rgba(v)
    return base
