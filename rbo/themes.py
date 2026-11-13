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
      "font": "/path/to/font.ttf"  optional; a list tries each in turn, "file.ttc:Bold"
                                   picks a face, "pixel" is the built-in pixel font
      "font_scale": 1.0,           text size multiplier for wide or narrow fonts

    Style keys (they change shapes, not just colours):
      "shape":   "round" | "slant" | "bracket" | "pixel" | "bezel"   cards, pills, bars
      "gauge":   "arc" | "bar" | "tape" | "led" | "dial"   speedometer, lean, G-force
      "marker":  "bike" | "badge" | "target" | "sprite" | "enamel"   you on the map
      "tower":   "default" | "broadcast" | "hud" | "arcade" | "pitboard"   race standings
      "texture": null | "scanlines" | "grain"
      "pattern": "weave" | "brushed" | "hex" | "plain"   the frame's finish (default: weave on dark round themes)
      "trim": "pinstripe", "trim_color": [r, g, b]   a thin second stripe round the picture
      "map_glow": true, "map_width": 5     racing line on the map
      "radio":   "classic" | "digital" | "military" | "arcade" | "vintage"   crew chief sound
      "frame_bg": [r, g, b]   the frame round the video (default: the card colour, darker)
    }
"""
from __future__ import annotations

import json
from pathlib import Path

# open-licence fonts that come with this (see fonts/OFL-*.txt): used where the
# Mac fonts the themes were designed with aren't there, e.g. on a server
FONTS = Path(__file__).parent / "fonts"

THEMES: dict[str, dict] = {
    "carbon": {
        "label": "Carbon",
        "bg": (10, 12, 16, 168), "edge": (255, 255, 255, 40),
        "text": (245, 247, 250, 255), "dim": (150, 156, 168, 255),
        "accent": (255, 92, 40, 255),
        "purple": (178, 92, 255, 255), "green": (40, 205, 110, 255),
        "yellow": (245, 196, 30, 255), "red": (235, 64, 52, 255),
        "line": (230, 233, 240, 235), "ring": (255, 255, 255, 255),
        "radius": 18, "stroke": 0, "stroke_color": (0, 0, 0, 200),
        "shape": "round", "gauge": "arc", "marker": "bike", "tower": "default", "texture": None,
        "map_glow": True, "map_width": 5, "radio": "classic", "font_scale": 1.0,
    },
    # the Carbon family: Carbon's layout and shapes, each with its own colours and frame finish
    "graphite": {
        "label": "Graphite",
        "bg": (22, 24, 28, 178), "edge": (255, 255, 255, 34),
        "text": (240, 242, 245, 255), "dim": (146, 152, 162, 255),
        "accent": (64, 156, 255, 255), "line": (226, 232, 242, 235),
        "radius": 14, "pattern": "brushed", "frame_bg": (30, 32, 36),
    },
    "rosso": {
        "label": "Rosso",
        "bg": (16, 10, 10, 172), "edge": (255, 255, 255, 36),
        "text": (248, 244, 242, 255), "dim": (172, 152, 150, 255),
        "accent": (226, 28, 44, 255), "line": (242, 236, 234, 235),
        "pattern": "weave", "trim": "pinstripe", "trim_color": (236, 232, 228), "frame_bg": (22, 14, 14),
    },
    "midnight": {
        "label": "Midnight",
        "bg": (8, 14, 32, 182), "edge": (150, 176, 255, 40),
        "text": (240, 244, 255, 255), "dim": (140, 156, 192, 255),
        "accent": (255, 190, 0, 255), "yellow": (255, 226, 96, 255), "line": (232, 238, 255, 235),
        "radius": 16, "pattern": "hex", "frame_bg": (10, 17, 36),
    },
    "lime": {
        "label": "Lime",
        "bg": (8, 10, 8, 172), "edge": (255, 255, 255, 34),
        "text": (244, 248, 240, 255), "dim": (150, 160, 146, 255),
        "accent": (176, 255, 30, 255), "green": (40, 214, 150, 255), "line": (236, 244, 228, 235),
        "pattern": "weave", "frame_bg": (12, 14, 12),
    },
    "stealth": {
        "label": "Stealth",
        "bg": (6, 6, 7, 196), "edge": (255, 255, 255, 22),
        "text": (236, 236, 238, 255), "dim": (124, 124, 130, 255),
        "accent": (212, 216, 224, 255), "line": (220, 222, 228, 230), "ring": (40, 40, 44, 255),
        "radius": 12, "pattern": "plain", "map_glow": False, "frame_bg": (10, 10, 11),
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
    "broadcast": {
        "label": "TV Broadcast",
        "bg": (11, 15, 30, 236), "edge": (0, 0, 0, 0),
        "text": (255, 255, 255, 255), "dim": (160, 172, 196, 255),
        "accent": (226, 18, 22, 255),
        "purple": (176, 84, 255, 255), "green": (24, 214, 104, 255),
        "yellow": (255, 204, 0, 255), "red": (232, 36, 36, 255),
        "line": (255, 255, 255, 240), "ring": (255, 255, 255, 255),
        "radius": 3, "shape": "slant", "gauge": "bar", "marker": "badge", "tower": "broadcast",
        "map_width": 4, "radio": "digital", "font_scale": 1.06,
        "font": ["/System/Library/Fonts/Supplemental/Futura.ttc:Condensed ExtraBold",
                 "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf",
                 str(FONTS / "BarlowCondensed-ExtraBold.ttf")],
    },
    "arcade": {
        "label": "Retro Arcade",
        "bg": (16, 6, 34, 228), "edge": (255, 46, 196, 255),
        "text": (255, 255, 255, 255), "dim": (90, 220, 255, 255),
        "accent": (255, 46, 196, 255),
        "purple": (190, 100, 255, 255), "green": (70, 255, 110, 255),
        "yellow": (255, 236, 50, 255), "red": (255, 64, 64, 255),
        "line": (0, 236, 255, 240), "ring": (255, 255, 255, 255),
        "radius": 0, "shape": "pixel", "gauge": "led", "marker": "sprite", "tower": "arcade",
        "texture": "scanlines", "map_width": 5, "radio": "arcade", "font": "pixel", "font_scale": 1.0,
    },
}

DEFAULT = "carbon"


def _rgba(v):
    v = tuple(int(x) for x in v)
    return v if len(v) == 4 else v + (255,)


# themes that have been taken out: videos saved with them open in Carbon
RETIRED = {"hud", "vintage"}


def load_theme(spec: str | dict | None) -> dict:
    """A built-in name, a path to a JSON file, or a dict of overrides."""
    if spec is None or spec == "" or (isinstance(spec, str) and spec.lower() in RETIRED):
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
        if k != "font" and isinstance(v, (list, tuple)) and len(v) in (3, 4) and all(isinstance(x, (int, float)) for x in v):
            base[k] = _rgba(v)
    return base
