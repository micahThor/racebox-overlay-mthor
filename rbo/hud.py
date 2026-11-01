"""Draws the overlay panels with Pillow.

Panels: lap timer with sectors, track map, speedometer, and G-force (or lean
angle for bike sessions). Each is a card anchored to a corner of the frame,
or placed wherever the layout says.

Quality: Pillow draws shapes without anti-aliasing, so everything is drawn
at `supersample` x the output size and scaled down each frame, which smooths
every arc, circle and line.

Speed: the panels are packed into one compact "atlas" image per frame;
ffmpeg crops each panel back out and overlays it at its spot, so only the
pixels that matter go through the pipe. Everything static (cards, labels,
gauge faces, the track outline) is drawn once and copied per frame.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .telemetry import Telemetry
from .themes import load_theme
from .ghost import GhostState, ghost_state
from .timing import HudState, Timing, fmt_time
from .pixelfont import PixelFont
from .hudstyles import StyleMixin

PANEL_NAMES = ("laps", "map", "speed", "gforce")   # "gforce" shows lean for bikes

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf",   # macOS
    "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/bahnschrift.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
]

GAUGE_START = 135.0    # speedometer arc: degrees clockwise from 3 o'clock
GAUGE_SWEEP = 270.0


RACE_ROWS = 12          # most rows in the race tower
RACE_RH = 27            # row height, reference pixels
RACE_W = 290            # standings tower width, reference pixels
MAP_PAD = 24            # space between the track and the map card's edge


def sidebar_width(width: int, height: int) -> int:
    """Width of the side panel added beside a video: enough to make an upright
    video 4:5 and a wide one 21:9 (even, and never tiny)."""
    target = height * 4 / 5 if height > width * 1.15 else height * 21 / 9
    side = max(target - width, 0.3 * min(width, height))
    return int(round(side / 2)) * 2


def bottom_height(width: int, height: int) -> int:
    """Height of the top and bottom strips: a slim bar, 12% of the video's longer side
    (230 px under 1080p, 330 under a 1530x2752 upright clip): room for a row of
    small gauges and the lap timer (its size slider makes it bigger)."""
    return int(round(0.12 * max(width, height) / 2)) * 2


def strip_size(width: int, height: int, where: str | None) -> tuple[int, int]:
    """(extra width, extra height) the side or bottom panel adds to the frame."""
    if where in ("left", "right"):
        return sidebar_width(width, height), 0
    if where in ("top", "bottom"):
        return 0, bottom_height(width, height)
    return 0, 0


def find_font(override=None) -> str | None:
    """First font that exists. `override` is a path, a list of paths, "file.ttc:Face"
    to pick a face from a collection, or "pixel" for the built-in pixel font."""
    cands = override if isinstance(override, (list, tuple)) else ([override] if override else [])
    for p in list(cands) + FONT_CANDIDATES:
        if p == "pixel":
            return p
        if p and Path(p.split(":")[0] if p.count(":") and not p[1:3] == ":/" else p).exists():
            return p
    return None


_FACE_INDEX: dict[str, int] = {}


def load_font(spec: str | None, px: int):
    if spec == "pixel":
        return PixelFont(px)
    if not spec:
        return ImageFont.load_default(px)
    path, face = spec, None
    if ":" in spec and not spec[1:3] == ":/":
        path, face = spec.rsplit(":", 1)
    if face is None:
        return ImageFont.truetype(path, px)
    if spec not in _FACE_INDEX:                      # find the named face in a .ttc
        _FACE_INDEX[spec] = 0
        for i in range(32):
            try:
                f = ImageFont.truetype(path, 20, index=i)
            except OSError:
                break
            if f.getname()[1].lower() == face.lower():
                _FACE_INDEX[spec] = i
                break
    return ImageFont.truetype(path, px, index=_FACE_INDEX[spec])


def _mix(a, b, f):
    return tuple(int(round(a[i] + (b[i] - a[i]) * f)) for i in range(3)) + \
        (int(round((a[3] if len(a) > 3 else 255) + ((b[3] if len(b) > 3 else 255) -
                                                    (a[3] if len(a) > 3 else 255)) * f)),)


@dataclass
class Panel:
    name: str
    key: str
    x: int          # output frame position
    y: int
    w: int          # output size
    h: int
    ay: int = 0     # output offset inside the atlas
    k: float = 1.0  # drawing scale (internal pixels per reference pixel)
    ss: int = 1
    draw: Callable | None = None
    # The panel is drawn in a w x h area, but only the part with something in it
    # is shown (and gets the card): vw x vh at (vx, vy) inside that area. x, y are
    # where that visible part goes on screen.
    vx: int = 0
    vy: int = 0
    vw: int = 0
    vh: int = 0

    def __post_init__(self):
        if not self.vw:
            self.vw, self.vh = self.w, self.h

    @property
    def iw(self) -> int:
        return self.w * self.ss

    @property
    def ih(self) -> int:
        return self.h * self.ss

    @property
    def iay(self) -> int:
        return self.ay * self.ss


class Hud(StyleMixin):
    def __init__(self, tel: Telemetry, timing: Timing, width: int, height: int,
                 units: str = "mph", scale: float = 1.0, font: str | None = None,
                 hide: set[str] | None = None, theme: str | dict | None = None,
                 layout: dict | None = None, speed_max: float | None = None,
                 supersample: int = 2, ghost=None, race=None,
                 race_class: bool = False, race_field: bool = True, sidebar: str | None = None,
                 strips=None, strip_size=None, info=None):
        """layout: {panel: {"x": 0..1, "y": 0..1, "scale": float, "visible": bool}},
        x/y being the panel's top-left corner as a fraction of the frame.
        speed_max: top of the speedometer in display units (70 mph / 120 km/h)."""
        self.spec = dict(tel=tel, timing=timing, width=width, height=height, units=units,
                         scale=scale, font=font, hide=hide, theme=theme, layout=layout,
                         speed_max=speed_max, supersample=supersample, ghost=ghost, race=race,
                         race_class=race_class, race_field=race_field, sidebar=sidebar,
                         strips=strips, strip_size=strip_size, info=info)
        # info: the event and track names, the date (text) and the event and track
        # logos (image paths): {"event", "track", "date", "event_logo", "track_logo"}
        self.info = {k: v for k, v in (info or {}).items() if v}
        self.race_field = bool(race_field)
        # race: an rbo.race.Race for the standings tower; race_class: only your class
        self.race = race
        self.race_cls = race.me.cls if race is not None and race_class else None
        # ghost: a ghost.GhostRef (the lap to replay and its video), or None for no ghost panel.
        # The older {"ref": lap index, "offset": s, "duration": s} form still works.
        if isinstance(ghost, dict):
            from .ghost import own_ref
            ghost = own_ref(timing, ghost["ref"], ghost["offset"], ghost["duration"],
                            ghost.get("video", ""), ghost.get("fps", 30.0)) if ghost.get("ref") is not None else None
        self.ghost = ghost
        self.tel, self.timing = tel, timing
        self.W, self.H = width, height
        self.ss = max(1, int(supersample))
        # Strips: the frame around the video. Left, right, top and bottom strips
        # are always there (their sizes can change); a panel goes in one with
        # layout {"where": "left" | "right" | "top" | "bottom"} ("video" or no
        # setting: on the picture). The output frame is canvas_w x canvas_h, with
        # the video at (vid_x0, vid_y0). The top and bottom strips run the full
        # width; the side strips sit between them, beside the picture.
        # (`strips` and `sidebar` are accepted for older callers.)
        self.strips = ["left", "right", "top", "bottom"]
        self.sidebar = "right"
        mult = lambda st: min(max(float((strip_size or {}).get(st, 1.0) or 1.0), 0.4), 2.5)
        even = lambda v: int(round(v / 2)) * 2
        sw = sidebar_width(width, height)
        self.left_w = even(sw * mult("left"))
        self.right_w = even(sw * mult("right"))
        self.top_h = even(bottom_height(width, height) * mult("top"))
        self.bottom_h = even(bottom_height(width, height) * mult("bottom"))
        self.canvas_w = width + self.left_w + self.right_w
        self.canvas_h = self.top_h + height + self.bottom_h
        self.vid_x0, self.vid_y0 = self.left_w, self.top_h
        self.base_k = min(width, height) / 1080 * scale        # output px per reference px
        self.k = self.base_k * self.ss
        self.units = units
        self.speed_factor = 0.621371 if units == "mph" else 1.0
        self.speed_max = float(speed_max or (70 if units == "mph" else 120))
        self.hide = set(hide or set())
        self.layout = layout or {}
        for name, cfg in self.layout.items():
            if cfg.get("visible") is False:
                self.hide.add(name)
        self.theme = load_theme(theme)
        self.C = dict(self.theme)
        self.C["white"] = self.theme["text"]
        self.font_path = find_font(font or self.theme.get("font"))
        self._fonts: dict[int, ImageFont.FreeTypeFont] = {}
        self._widths: dict[tuple, float] = {}

        self.has_g = tel.g_lat is not None and tel.g_long is not None and not tel.is_bike
        self.has_lean = tel.is_bike
        if self.has_g:
            gmag = np.hypot(tel.g_lat, tel.g_long)
            self.gmax = max(1.0, math.ceil(float(np.percentile(gmag, 99.5)) * 2) / 2)
        if self.has_lean:
            self.lean_max = (float(np.percentile(tel.lean_deg, 99.7)),
                             float(np.percentile(tel.lean_deg, 0.3)))

        self._setup_map_rotation()
        self.panels = self._layout()
        if not self.panels:   # everything hidden: keep a 2x2 transparent stub
            self.panels = [Panel("none", "none", 0, 0, 2, 2, k=self.k, ss=self.ss,
                                 draw=lambda *a: None)]
        # a little empty space between panels in the atlas, so a marker or tag drawn
        # right at one panel's edge can never land in the next panel
        gap = max(2, int(round(24 * self.base_k))) // 2 * 2
        self.atlas_w = max(p.w for p in self.panels)
        self.atlas_h = sum(p.h for p in self.panels) + gap * (len(self.panels) - 1)
        y = 0
        for p in self.panels:
            p.ay = y
            y += p.h + gap
        self._sprites: dict = {}
        self.static = self._draw_static()

    # ------------------------------------------------------------ helpers

    def S(self, v: float) -> int:
        return int(round(v * self.k))

    def _use(self, p: Panel) -> None:
        self.k = p.k

    def col(self, c):
        return self.C.get(c, c) if isinstance(c, str) else c

    def tint(self, color, opacity: float):
        """`color` at `opacity` laid over the card, as one solid colour.
        Drawing a translucent colour straight onto the card would replace the
        card's alpha and punch a hole in it, so composite it here instead."""
        bg = self.C["bg"]
        c = self.col(color)
        o = opacity * (c[3] / 255 if len(c) > 3 else 1)
        ab = bg[3] / 255
        a = o + ab * (1 - o)
        if a <= 0:
            return (0, 0, 0, 0)
        rgb = tuple(int(round((o * c[i] + ab * (1 - o) * bg[i]) / a)) for i in range(3))
        return rgb + (int(round(a * 255)),)

    def font(self, size: float) -> ImageFont.FreeTypeFont:
        px = max(6, self.S(size * self.theme.get("font_scale", 1.0)))
        if px not in self._fonts:
            self._fonts[px] = load_font(self.font_path, px)
        return self._fonts[px]

    def text(self, d: ImageDraw.ImageDraw, x: float, y: float, s: str, size: float,
             fill="white", anchor: str = "l", track: float = 0.0) -> float:
        """Draw text with its baseline at y. Digits get a fixed width so
        running numbers don't jitter; `track` adds letter spacing."""
        f = self.font(size)
        fill = self.col(fill)
        L = self._len
        sw = int(round(self.theme.get("stroke", 0) * self.k * size / 30)) if self.theme.get("stroke") else 0
        extra = {"stroke_width": sw, "stroke_fill": self.C["stroke_color"]} if sw else {}
        dw = L(f, "#digit")
        sp = track * self.k
        widths = [(dw if c.isdigit() else L(f, c)) + sp for c in s]
        total = sum(widths) - (sp if s else 0)
        cx = x - total if anchor == "r" else x - total / 2 if anchor == "m" else x
        pixel = isinstance(f, PixelFont)
        for c, w in zip(s, widths):
            off = (w - sp - L(f, c)) / 2 if c.isdigit() else 0
            if pixel:
                f.draw(d, cx + off, y, c, fill, extra.get("stroke_fill"), extra.get("stroke_width", 0))
            else:
                d.text((cx + off, y), c, font=f, fill=fill, anchor="ls", **extra)
            cx += w
        return total

    def measure(self, s: str, size: float) -> float:
        f = self.font(size)
        return sum(self._len(f, "#digit") if c.isdigit() else self._len(f, c) for c in s)

    def _len(self, f, c: str) -> float:
        key = (id(f), c)
        v = self._widths.get(key)
        if v is None:
            v = max(f.getlength(x) for x in "0123456789") if c == "#digit" else f.getlength(c)
            self._widths[key] = v
        return v

    def _has_card(self) -> bool:
        return not (self.C["bg"][3] == 0 and self.C["edge"][3] == 0)

    def card(self, img: Image.Image, d: ImageDraw.ImageDraw, p: Panel):
        if self.shape != "round" and self.card_styled(img, d, p):
            return
        if not self._has_card():
            return
        r = self.S(self.theme.get("radius", 18))
        bg = self.C["bg"]
        light = sum(bg[:3]) > 382
        top = _mix(bg, (255, 255, 255, bg[3]), 0.05 if light else 0.07)
        bot = _mix(bg, (0, 0, 0, bg[3]), 0.04 if light else 0.18)
        grad = np.linspace(0, 1, p.ih)[:, None, None]
        arr = (np.array(top, float) * (1 - grad) + np.array(bot, float) * grad)
        arr = np.repeat(arr, p.iw, axis=1).astype(np.uint8)
        mask = Image.new("L", (p.iw, p.ih), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, p.iw - 1, p.ih - 1], radius=r, fill=255)
        img.paste(Image.fromarray(arr, "RGBA"), (0, p.iay), mask)
        if self.C["edge"][3]:
            d.rounded_rectangle([0, p.iay, p.iw - 1, p.iay + p.ih - 1], radius=r,
                                outline=self.C["edge"], width=max(1, self.S(1.2)))
        # glassy highlight along the top edge
        hl = self.tint((255, 255, 255, 255), 0.06 if light else 0.10)
        d.line([(r, p.iay + self.S(1.5)), (p.iw - r, p.iay + self.S(1.5))], fill=hl,
               width=max(1, self.S(1.2)))

    def side_colour(self) -> tuple:
        """The frame's base colour (see frame.py)."""
        from .frame import frame_colour
        return frame_colour(self.theme)

    def frame_image(self) -> Image.Image:
        """The themed frame round the video, at output size (clear over the picture)."""
        if getattr(self, "_frame", None) is None:
            from .frame import draw_frame
            self._frame = draw_frame(self)
        return self._frame

    def _card_visible(self, img, d, p: Panel, fringe):
        """The card, drawn only around the visible part of the panel."""
        if not p.vx and not p.vy and p.vw == p.w and p.vh == p.h:
            self.card(img, d, p)
            return
        tmp = Image.new("RGBA", (p.vw * self.ss, p.vh * self.ss), tuple(fringe[:3]) + (0,))
        q = Panel(p.name, p.key, 0, 0, p.vw, p.vh, ay=0, k=p.k, ss=p.ss)
        self.card(tmp, ImageDraw.Draw(tmp), q)
        img.alpha_composite(tmp, (p.vx * self.ss, (p.ay + p.vy) * self.ss))

    # Empty margins to leave off each panel (reference px: left, top, right,
    # bottom), measured from what each style actually draws, so the card fits
    # its contents snugly.
    TRIM = {
        ("speed", "arc"): (19, 14, 24, 38), ("speed", "bar"): (7, 10, 2, 37),
        ("speed", "tape"): (12, 0, 52, 31), ("speed", "led"): (8, 13, 9, 37),
        ("speed", "dial"): (21, 14, 20, 9),
        ("lean", "arc"): (11, 10, 11, 25), ("lean", "bar"): (9, 8, 9, 2),
        ("lean", "tape"): (0, 10, 10, 0), ("lean", "led"): (6, 4, 8, 0),
        ("lean", "dial"): (8, 5, 7, 2),
        ("laps", "*"): (10, 6, 11, 0),
        ("race", "default"): (0, 7, 0, 0), ("race", "arcade"): (0, 3, 0, 0), ("race", "pitboard"): (0, 3, 0, 0),
    }

    def _trim(self, name: str) -> tuple:
        if name in ("speed", "lean"):
            return self.TRIM.get((name, self.gauge), (0, 0, 0, 0))
        if name == "race":
            return self.TRIM.get((name, self.theme.get("tower", "default")), (0, 0, 0, 0))
        return self.TRIM.get((name, "*"), (0, 0, 0, 0))

    def glow(self, color, radius: float):
        """A soft round glow sprite (cached)."""
        key = (tuple(color), round(radius * self.k))
        if key not in self._sprites:
            r = self.S(radius)
            size = r * 4 + 2
            im = Image.new("RGBA", (size, size), tuple(color[:3]) + (0,))
            ImageDraw.Draw(im).ellipse([size / 2 - r, size / 2 - r, size / 2 + r, size / 2 + r],
                                       fill=tuple(color[:3]) + (170,))
            self._sprites[key] = im.filter(ImageFilter.GaussianBlur(r * 0.75))
        return self._sprites[key]

    def stamp(self, img: Image.Image, sprite: Image.Image, cx: float, cy: float, p: Panel):
        """alpha_composite a sprite centred at (cx, cy), clipped to the panel."""
        x0, y0 = int(round(cx - sprite.width / 2)), int(round(cy - sprite.height / 2))
        bx0, by0 = max(x0, 0), max(y0, p.iay)
        bx1, by1 = min(x0 + sprite.width, p.iw), min(y0 + sprite.height, p.iay + p.ih)
        if bx1 <= bx0 or by1 <= by0:
            return
        crop = sprite.crop((bx0 - x0, by0 - y0, bx1 - x0, by1 - y0))
        img.alpha_composite(crop, (bx0, by0))

    def dot(self, img, d, x, y, r, color, p: Panel, glow=True):
        if glow:
            self.stamp(img, self.glow(self.col(color), r * 1.6), x, y, p)
        R, ring = self.S(r), self.S(r * 0.3)
        d.ellipse([x - R - ring, y - R - ring, x + R + ring, y + R + ring], fill=self.C["ring"])
        d.ellipse([x - R, y - R, x + R, y + R], fill=self.col(color))

    # ------------------------------------------------------------ layout

    # ------------------------------------------------------------ map rotation

    def _setup_map_rotation(self) -> None:
        """How the track map is turned: layout["map"]["rotate"] is 0, 90, 180 or
        270 degrees clockwise from the map as recorded (other values snap to
        the nearest quarter turn)."""
        cfg = self.layout.get("map", {}) or {}
        quarter = int(round(float(cfg.get("rotate", 0) or 0) / 90.0)) % 4
        self.map_quarter = quarter
        x, y = self.timing.x, self.timing.y
        self._map_c = (float((x.min() + x.max()) / 2), float((y.min() + y.max()) / 2))
        self._map_ang = -quarter * math.pi / 2                  # clockwise on screen

    def _map_track(self):
        """World x/y of the track the map shows: the fastest lap (so the grid-out
        or pit-out path isn't drawn), or the whole ride if there are no laps."""
        laps = self.timing.laps
        if not laps:
            return self.timing.x, self.timing.y
        best = min(laps, key=lambda l: l.time)
        m = (self.tel.t >= best.t_start) & (self.tel.t <= best.t_end)
        return self.timing.x[m], self.timing.y[m]

    def map_rot(self, xx, yy):
        """World x/y (metres) turned the way the map is shown."""
        if not self.map_quarter:
            return xx, yy
        c, s_ = [(1, 0), (0, -1), (-1, 0), (0, 1)][self.map_quarter]     # exact quarter turns
        cx, cy = self._map_c
        dx, dy = np.asarray(xx) - cx, np.asarray(yy) - cy
        return cx + dx * c - dy * s_, cy + dx * s_ + dy * c

    RESHAPE = {"map": "wh", "race": "wh", "ghost": "wh", "laps": "w"}

    @staticmethod
    def _shape_factor(cfg: dict, k: str) -> float:
        try:
            return min(max(float(cfg.get(k, 1.0) or 1.0), 0.4), 3.0)
        except (TypeError, ValueError):
            return 1.0

    LAP_ITEMS = ("time", "sectors", "last", "best")

    def _laps_geometry(self) -> tuple[int, int]:
        """What the lap timer shows (layout["laps"]["items"], any of time, sectors,
        last, best; default all) and where each part goes, in reference px.
        The card shrinks to fit: just the lap time is a small card, everything
        is the full-size one."""
        cfg = self.layout.get("laps", {}) or {}
        items = cfg.get("items")
        items = set(self.LAP_ITEMS if items is None else items) & set(self.LAP_ITEMS)
        if not self.timing.sector_gates:
            items.discard("sectors")
        self.lap_items = items
        g = {"header": 44}
        y = 52                                   # under the header (lap counter and delta)
        if "time" in items:
            g["clock"] = 128
            y = 148
        below = ("last" in items or "best" in items or "sectors" in items)
        if below and "time" in items:
            g["divider"] = 148
        if "last" in items or "best" in items:
            g["row"] = y + 36
            y += 52
        if "sectors" in items:
            g["sectors"] = y + 22                # top of the bars
            y += 72
        else:
            y += 8 if "time" not in items else 0
        g["h"] = max(y, 72)
        wide = "sectors" in items or ("last" in items and "best" in items)
        need = self.measure("8:88.88", 92) / self.k + 56 if "time" in items else 0   # a held lap time
        g["w"] = int(max(520 if wide else 360, need))
        g["min_w"] = max(need, 430 if wide else 0)      # narrowest it can be made
        self._lap_geom = g
        return g["w"], g["h"]

    # ------------------------------------------------------------ event info panels

    INFO_TEXT = {"event": "EVENT", "track": "TRACK", "date": "DATE"}
    LOGO_H = 140            # a logo's reference height

    def _info_specs(self) -> list:
        """The event and track logos and the event name, track name and date panels
        (each only when it has something to show)."""
        out = []
        self._logos = {}
        for key in ("event_logo", "track_logo"):
            path = self.info.get(key)
            if not path:
                continue
            try:
                im = Image.open(Path(str(path)).expanduser())
                im.load()
                im = im.convert("RGBA")
            except (OSError, ValueError):
                continue
            bb = im.getchannel("A").getbbox()            # leave off empty transparent margins
            if bb:
                im = im.crop(bb)
            self._logos[key] = im
            a = im.width / max(im.height, 1)
            rh = self.LOGO_H
            rw = min(max(rh * a, 40), 600)
            if rh * a > 600:                               # a very wide logo: keep its shape
                rh = 600 / a
            out.append((key, "logo", rw, rh, "bl" if key == "event_logo" else "br", lambda *a: None))
        texts = [k for k in ("event", "track", "date") if str(self.info.get(k) or "").strip()]
        # all the same width (the widest one's), so they line up and share a text size
        need = max([self.measure(str(self.info[k]).strip(), 30) / self.k + 52 for k in texts] or [0])
        rw = min(max(need, 220), 640)
        for key in texts:
            out.append((key, "text", rw, 92, "tl", lambda *a: None))
        return out

    def _static_logo(self, img, d, p):
        im = self._logos.get(p.key)
        if im is None:
            return
        im = im.resize((p.iw, p.ih), Image.LANCZOS)
        img.alpha_composite(im, (0, p.iay))

    def _static_text(self, img, d, p):
        S, pad = self.S, self.S(24)
        txt = str(self.info.get(p.key) or "").strip()
        self.text(d, pad, p.iay + S(34), self.INFO_TEXT.get(p.key, ""), 15, "dim", track=2)
        size = 30
        room = p.iw - 2 * pad
        while size > 14 and self.measure(txt, size) > room:   # long text: smaller, to fit
            size -= 1
        self.text(d, pad, p.iay + S(74), txt, size, "white")

    def _layout(self) -> list[Panel]:
        W, H = self.W, self.H
        specs = []     # (key, name, ref_w, ref_h, corner, draw)
        if self.timing.laps:
            lw, lh = self._laps_geometry()
            specs.append(("laps", "laps", lw, lh, "tl",
                          self._draw_laps))
        # the map's card fits the track itself (a timed lap, not the ride out of the
        # pits or off the grid), turned the way it's shown
        x, y = self.map_rot(*self._map_track())
        aspect = (np.ptp(y) + 1) / (np.ptp(x) + 1)
        pad = 2 * MAP_PAD
        if aspect <= 1:
            mw, mh = 400, int(max((400 - pad) * aspect + pad, 110))
        else:
            mw, mh = int(max((400 - pad) / aspect + pad, 110)), 400
        specs.append(("map", "map", mw, mh, "tr", self._draw_map))
        specs.append(("speed", "speed", 330, 312, "bl", self._draw_speed))
        if self.has_g:
            specs.append(("gforce", "gforce", 290, 312, "br", self._draw_g))
        elif self.has_lean:
            specs.append(("gforce", "lean", 380, 262, "br", self._draw_lean))
        portrait = H > W * 1.15
        self.portrait = portrait
        if self.ghost:
            # the window keeps the ghost video's own shape (portrait or landscape)
            a = float(getattr(self.ghost, "aspect", 9 / 16) or 9 / 16)
            if portrait:
                sw = 300 if a > 1 else 440
            else:
                sw = 464 if a <= 1 else 340 / a
            sh = sw * a
            label_h = 33 if sw + 16 >= 420 else 56          # narrow window: label on two lines
            gw, gh = sw + 16, sh + 8 + label_h
            cfg = self.layout.get("ghost", {}) or {}
            gw, gh = gw * self._shape_factor(cfg, "w"), gh * self._shape_factor(cfg, "h")
            label_h = 33 if gw >= 420 else 56
            avail_h = gh - 8 - label_h
            fit = min(gw - 16, avail_h / a)                  # video fitted inside, centred
            self.ghost_slot_ref = ((gw - fit) / 2, 8 + (avail_h - fit * a) / 2, fit, fit * a)
            specs.append(("ghost", "ghost", gw, gh, "tc" if not portrait else "tr", self._draw_ghost))
        if self.race is not None:
            n = len([r for r in self.race.riders if not self.race_cls or r.cls == self.race_cls])
            self.race_rows = min(n, RACE_ROWS)
            self.race_key_h = 14 if self.multi_class() else 0       # the class colour key under the header
            self.combined = "map" not in self.hide and "race" not in self.hide
            if self.combined:
                # race mode: the track map and the standings are one panel, map on
                # top, the class key under it, then the standings
                self.race_key_h = 0
                cfg = self.layout.get("race", {}) or {}
                rw_ = RACE_W * self._shape_factor(cfg, "w")
                x, y = self.map_rot(*self._map_track())
                aspect = (np.ptp(y) + 1) / (np.ptp(x) + 1)
                pad = 2 * MAP_PAD
                map_h = min(max((rw_ - pad) * aspect + pad, 110), 420)
                key_h = 24 if self.multi_class() else 0
                tower_h = (70 + self.race_rows * RACE_RH) * self._shape_factor(cfg, "h")
                self._racemap = (map_h, key_h)
                specs = [sp for sp in specs if sp[0] != "map"]
                specs.append(("race", "racemap", rw_, map_h + key_h + tower_h, "tr", self._draw_racemap))
            else:
                specs.append(("race", "race", RACE_W, 70 + self.race_key_h + self.race_rows * RACE_RH, "ml",
                              self._draw_race))

        specs += self._info_specs()

        # Portrait (a helmet camera held upright, or a phone-shaped export): keep the
        # middle of the frame, the track ahead, clear. Timing and map go in the sky
        # at the top, the standings and ghost down the sides just below them, and
        # speed and lean sit together at the bottom like a dash, a little larger.
        portrait_scale = {"speed": 1.15, "gforce": 1.15} if portrait else {}
        side_k = self._side_scales(specs)
        sized = []
        for key, name, rw, rh, corner, fn in specs:
            if key in self.hide:
                continue
            cfg = self.layout.get(key, {})
            k_out = self.base_k * float(cfg.get("scale", 1.0)) * portrait_scale.get(key, 1.0)
            trim = self._trim(name)
            if key in side_k:                    # in the side panel: sized to fill its width
                k_out = side_k[key]
            # reshaping: these panels can be made wider/narrower and taller/shorter on
            # their own; their contents re-flow (the round gauges only scale evenly)
            if key in side_k:
                pass
            elif key in ("map", "race") and name != "racemap":     # (the combined panel did its own)
                rw, rh = rw * self._shape_factor(cfg, "w"), rh * self._shape_factor(cfg, "h")
            elif key == "laps":
                rw = max(rw * self._shape_factor(cfg, "w"), self._lap_geom["min_w"])
            w, h = max(2, int(round(rw * k_out))), max(2, int(round(rh * k_out)))
            L, T, R, B = (int(round(v * k_out)) for v in trim)
            vw, vh = max(2, w - L - R), max(2, h - T - B)
            sized.append((key, name, w, h, corner, fn, cfg, k_out, (L, T, vw, vh)))
        m = int(round(36 * self.base_k))
        gap = int(round(16 * self.base_k))
        size = {key: (vis[2], vis[3]) for key, _, _w, _h, _c, _f, _cfg, _k, vis in sized}
        panels = []
        # the side panel: its panels stacked top to bottom, centred in the strip
        side_pos = {}
        for st in self.strips:
            sx0, sy0, sw, sh = self.strip_rect(st)
            items = sorted([it for it in sized if it[0] in side_k and self.in_side(it[0]) == st],
                           key=lambda it: self._side_order(it[0]))
            if st in ("top", "bottom"):         # a row, centred over/under the video
                row = sum(it[8][2] for it in items) + gap * max(len(items) - 1, 0)
                x = self.vid_x0 + (self.W - row) / 2
                x = min(max(x, sx0), sx0 + max(sw - row, 0))   # too wide: slide along, stay in frame
                for it in items:
                    side_pos[it[0]] = (x, sy0 + (sh - it[8][3]) / 2)
                    x += it[8][2] + gap
            else:                               # a column, centred in the side strip
                col = sum(it[8][3] for it in items) + gap * max(len(items) - 1, 0)
                y = sy0 + (sh - col) / 2
                for it in items:
                    side_pos[it[0]] = (sx0 + (sw - it[8][2]) / 2, y)
                    y += it[8][3] + gap
        for key, name, dw, dh, corner, fn, cfg, k_out, vis in sized:
            L, T, w, h = vis
            if key in side_pos:
                px, py = side_pos[key]
                panels.append(Panel(name, key, int(round(px)), int(round(py)), dw, dh, k=k_out * self.ss,
                                    ss=self.ss, draw=fn, vx=L, vy=T, vw=w, vh=h))
                continue
            if "x" in cfg and "y" in cfg:
                px, py = float(cfg["x"]) * W, float(cfg["y"]) * H
            elif portrait:
                px, py = self._portrait_pos(key, w, h, size, m, gap)
            else:
                px = m if corner[1] == "l" else W - m - w if corner[1] == "r" else (W - w) / 2
                py = m if corner[0] == "t" else H - m - h if corner[0] == "b" else \
                    (H - h) / 2 if corner[1] == "l" else H * 0.40
            px = int(round(min(max(px, 0), max(W - w, 0)))) + self.vid_x0
            py = int(round(min(max(py, 0), max(H - h, 0)))) + self.vid_y0
            panels.append(Panel(name, key, px, py, dw, dh, k=k_out * self.ss, ss=self.ss, draw=fn,
                                vx=L, vy=T, vw=w, vh=h))
        return panels

    SIDE_ORDER = ("event_logo", "event", "track", "date", "laps", "race", "map", "ghost", "speed", "gforce",
                  "track_logo")
    # where a panel goes when the layout doesn't say: the race panel in the right
    # strip, the logos at either end of the bottom strip, the names and date in
    # a row in the top strip; the rest on the video
    DEFAULT_WHERE = {"race": "right", "event_logo": "bottom", "track_logo": "bottom",
                     "event": "top", "track": "top", "date": "top"}

    def strip_rect(self, st: str) -> tuple:
        """(x, y, w, h) of a strip in the output frame."""
        if st == "left":
            return 0, self.vid_y0, self.left_w, self.H
        if st == "right":
            return self.left_w + self.W, self.vid_y0, self.right_w, self.H
        if st == "top":
            return 0, 0, self.canvas_w, self.top_h
        return 0, self.vid_y0 + self.H, self.canvas_w, self.bottom_h

    def in_side(self, key: str) -> str:
        """The strip a panel sits in, or "" if it's on the video."""
        w = (self.layout.get(key, {}) or {}).get("where", self.DEFAULT_WHERE.get(key))
        if w == "side":                          # older setting: the first side strip
            w = "right"
        return w if w in self.strips else ""

    def _side_order(self, key: str) -> float:
        cfg = self.layout.get(key, {}) or {}
        if cfg.get("side_order") is not None:
            return float(cfg["side_order"])
        return self.SIDE_ORDER.index(key) / 10 if key in self.SIDE_ORDER else 0.99

    def _side_scales(self, specs) -> dict:
        """Drawing scale for each panel in a strip. In a side strip each panel fills
        its width (a column); in the bottom strip each fills its height (a row);
        all of a strip's panels shrink together if they don't fit."""
        out = {}
        m, gap = 36 * self.base_k, 16 * self.base_k
        for st in self.strips:
            _, _, sw, sh = self.strip_rect(st)
            items = []
            for key, name, rw, rh, corner, fn in specs:
                if key in self.hide or self.in_side(key) != st:
                    continue
                L, T, R, B = self._trim(name)
                # the size slider: a share of the strip (100% fills it)
                share = min(max(float((self.layout.get(key, {}) or {}).get("scale", 1.0) or 1.0), 0.2), 1.0)
                if st in ("top", "bottom"):
                    k = (sh - 2 * m) / max(rh - T - B, 1) * share
                    if name == "text":           # names and the date: no bigger than a strip's width would make them
                        k = min(k, (self.left_w - 2 * m) / max(rw, 1) * share)
                    items.append((key, k, (rw - L - R) * k))
                else:
                    k = (sw - 2 * m) / max(rw - L - R, 1) * share
                    items.append((key, k, (rh - T - B) * k))
            if not items:
                continue
            total = sum(h for *_, h in items) + gap * (len(items) - 1)
            room = (sw if st in ("top", "bottom") else sh) - 2 * m
            f = min(1.0, room / max(total, 1))
            out.update({key: k * f for key, k, _ in items})
        return out

    def _portrait_pos(self, key, w, h, size, m, gap):
        W, H = self.W, self.H
        if key == "laps":
            return m, m
        if key == "map":
            return W - m - w, m
        if key == "race":
            return m, m + (size["laps"][1] + gap if "laps" in size else 0)
        if key == "ghost":
            return W - m - w, m + (size["map"][1] + gap if "map" in size else 0)
        if key in ("speed", "gforce"):                       # side by side, centred, at the bottom
            sw_ = size.get("speed", (0, 0))[0]
            gw_ = size.get("gforce", (0, 0))[0]
            total = sw_ + gw_ + (gap * 2 if sw_ and gw_ else 0)
            x0 = (W - total) / 2
            return (x0 if key == "speed" else x0 + total - w), H - m - h
        if key in ("event_logo", "event", "track", "date"):
            return m, H * 0.45 + {"event_logo": 0, "event": 1, "track": 2, "date": 3}[key] * (h + gap)
        if key == "track_logo":
            return W - m - w, H * 0.45
        return (W - w) / 2, H * 0.4

    def describe(self) -> list[dict]:
        """Panel rectangles in output pixels, for the GUI."""
        return [{"key": p.key, "name": p.name, "x": p.x, "y": p.y, "w": p.vw, "h": p.vh,
                 "ay": p.ay + p.vy, "ax": p.vx,
                 "reshape": "" if self.in_side(p.key) else self.RESHAPE.get(p.key, ""),
                 "side": self.in_side(p.key)} for p in self.panels]

    def frame_info(self) -> dict:
        """The output frame and its strips, for the editor."""
        return {"w": self.canvas_w, "h": self.canvas_h, "vx": self.vid_x0, "vy": self.vid_y0,
                "vw": self.W, "vh": self.H,
                "strips": {st: list(self.strip_rect(st)) for st in self.strips}}

    # ------------------------------------------------------------ static layer

    def _draw_static(self) -> Image.Image:
        # Transparent pixels get the card's colour (or the text colour on card-less
        # themes) so that averaging during the downscale doesn't darken edges.
        fringe = self.C["bg"] if self._has_card() else self.C["text"]
        img = Image.new("RGBA", (self.atlas_w * self.ss, self.atlas_h * self.ss),
                        tuple(fringe[:3]) + (0,))
        d = ImageDraw.Draw(img)
        for p in self.panels:
            if p.name == "none":
                continue
            self._use(p)
            if p.name not in ("ghost", "logo"):   # the ghost's card only shows while it's active; logos have none
                self._card_visible(img, d, p, fringe)
                self.apply_texture(img, p)
            getattr(self, f"_static_{p.name}", lambda *a: None)(img, d, p)
        return img

    def _static_laps(self, img, d, p):
        pad = self.S(24)
        if "divider" in self._lap_geom:
            yv = p.iay + self.S(self._lap_geom["divider"])
            d.line([(pad, yv), (p.iw - pad, yv)],
                   fill=self.tint("text" if self.light() else "white", 0.14), width=max(1, self.S(1)))

    def _static_map(self, img, d, p):
        pad = self.S(MAP_PAD)
        x, y = self.timing.x, self.timing.y
        xr, yr = self.map_rot(*self._map_track())
        s = min((p.iw - 2 * pad) / max(np.ptp(xr), 1), (p.ih - 2 * pad) / max(np.ptp(yr), 1))
        cx, cy = (xr.min() + xr.max()) / 2, (yr.min() + yr.max()) / 2
        oy = p.iay

        def xform(xx, yy):
            rx, ry = self.map_rot(xx, yy)
            return p.iw / 2 + (rx - cx) * s, oy + p.ih / 2 - (ry - cy) * s
        self._map_xform = xform

        if not self.timing.laps:      # no laps: the whole ride, faintly
            step = max(1, len(x) // 4000)
            pts = list(zip(*self._map_xform(x[::step], y[::step])))
            d.line(pts, fill=self.tint("text" if self.light() else "white", 0.5), width=max(1, self.S(3)),
                   joint="curve")

        laps = self.timing.laps
        if laps:
            best = min(laps, key=lambda l: l.time)
            m = (self.tel.t >= best.t_start) & (self.tel.t <= best.t_end)
            line = list(zip(*self._map_xform(x[m], y[m])))
            dd = self.timing.dist[m] - self.timing.dist[m][0]           # for placing the field
            self._lap_path = (dd / max(dd[-1], 1e-6), x[m], y[m])
            if self.theme.get("map_glow", True):        # soft glow under the racing line
                layer = Image.new("RGBA", img.size, tuple(self.C["line"][:3]) + (0,))
                ImageDraw.Draw(layer).line(line, fill=tuple(self.C["line"][:3]) + (110,),
                                           width=self.S(12), joint="curve")
                box = (0, p.iay, p.iw, p.iay + p.ih)
                region = layer.crop(box).filter(ImageFilter.GaussianBlur(self.S(5)))
                base = img.crop(box)
                base.alpha_composite(region)
                img.paste(base, box[:2])
            lw = max(2, self.S(self.theme.get("map_width", 5)))
            if self.theme.get("stroke"):
                d.line(line, fill=self.C["stroke_color"], width=lw + self.S(3), joint="curve")
            d.line(line, fill=self.C["line"], width=lw, joint="curve")

        for g in self.timing.sector_gates:
            gx, gy = self._map_xform(g.x, g.y)
            r = self.S(5)
            d.ellipse([gx - r, gy - r, gx + r, gy + r], fill=self.C["bg"] if self._has_card() else (0, 0, 0, 200),
                      outline=self.C["dim"], width=max(1, self.S(2)))
        g = self.timing.start_gate
        if g is not None:
            gx, gy = self._map_xform(g.x, g.y)
            ca, sa = [(1, 0), (0, -1), (-1, 0), (0, 1)][self.map_quarter]
            gdx, gdy = g.dx * ca - g.dy * sa, g.dx * sa + g.dy * ca      # travel direction, turned
            nx, ny = -gdy, -gdx            # perpendicular to travel, in screen coords (y flipped)
            tx, ty = gdx, -gdy
            L, Wd = self.S(15), self.S(4.5)
            n = 4
            for i in range(n):          # chequered bar across the track
                for j in range(2):
                    a0, a1 = -L + 2 * L * i / n, -L + 2 * L * (i + 1) / n
                    b0, b1 = -Wd + Wd * j, Wd * j
                    quad = [(gx + nx * a + tx * b, gy + ny * a + ty * b)
                            for a, b in ((a0, b0), (a1, b0), (a1, b1), (a0, b1))]
                    d.polygon(quad, fill=(255, 255, 255, 255) if (i + j) % 2 == 0 else (20, 20, 20, 255))

    def _gauge_geom(self, p):
        cx, cy = p.iw / 2, p.iay + self.S(160)
        return cx, cy, self.S(128), self.S(14)

    def _ang(self, frac):
        return math.radians(GAUGE_START + GAUGE_SWEEP * frac)

    def _static_speed(self, img, d, p):
        if self.gauge != "arc" and self.static_speed_styled(img, d, p):
            return
        cx, cy, R, wid = self._gauge_geom(p)
        box = [cx - R, cy - R, cx + R, cy + R]
        vmax = self.speed_max
        red_from = vmax * 6 / 7 if vmax >= 35 else vmax * 0.85
        d.arc(box, GAUGE_START, GAUGE_START + GAUGE_SWEEP, fill=self.tint("white", 0.13), width=wid)
        d.arc(box, GAUGE_START + GAUGE_SWEEP * red_from / vmax, GAUGE_START + GAUGE_SWEEP,
              fill=self.tint("red", 0.45), width=wid)
        major = 10 if vmax <= 100 else 20
        minor = major / 2
        v = 0.0
        while v <= vmax + 1e-6:
            a = self._ang(v / vmax)
            is_major = abs(v / major - round(v / major)) < 1e-6
            r1 = R - wid - self.S(5)
            r2 = r1 - self.S(13 if is_major else 7)
            col = self.C["red"] if v >= red_from - 1e-6 and is_major else self.C["dim"]
            d.line([(cx + r1 * math.cos(a), cy + r1 * math.sin(a)),
                    (cx + r2 * math.cos(a), cy + r2 * math.sin(a))],
                   fill=col if is_major else self.tint("dim", 0.7), width=max(1, self.S(2.5 if is_major else 1.5)))
            if is_major:
                rl = r2 - self.S(17)
                f = self.font(17)
                self.text(d, cx + rl * math.cos(a), cy + rl * math.sin(a) + f.size * 0.36,
                          f"{v:.0f}", 17, col, "m")
            v += minor
        unit = "MPH" if self.units == "mph" else "KM/H"
        self.text(d, cx, cy + self.S(58), unit, 15, "dim", "m", track=2)
        # gradient arc used for the live speed fill, masked per frame
        self._speed_arc = self._ring_image(R, wid, red_from / vmax)

    def _ring_image(self, R: int, wid: int, red_frac: float) -> Image.Image:
        size = 2 * R + wid + 4
        c = size / 2
        yy, xx = np.mgrid[0:size, 0:size].astype(float) + 0.5
        ang = (np.degrees(np.arctan2(yy - c, xx - c)) - GAUGE_START) % 360
        frac = np.clip(ang / GAUGE_SWEEP, 0, 1)[..., None]
        acc = np.array(self.C["accent"][:3], float)
        lo = acc * 0.55 + np.array(self.C["bg"][:3], float) * 0.45
        rgb = lo * (1 - frac / max(red_frac, 1e-3)) + acc * (frac / max(red_frac, 1e-3))
        red = np.array(self.C["red"][:3], float)
        rgb = np.where(frac >= red_frac, red, np.clip(rgb, 0, 255))
        arr = np.dstack([rgb, np.full((size, size), 255.0)]).astype(np.uint8)
        return Image.fromarray(arr, "RGBA")

    def _static_gforce(self, img, d, p):
        cx, cy, R = p.iw / 2, p.iay + self.S(176), self.S(108)
        self._g_geom = (cx, cy, R)
        n = int(round(self.gmax / 0.5))
        ring_col = "text" if self.light() else "white"
        if self.gauge == "dial":
            rr = R + self.S(10)
            d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=self.C["edge"])
            d.ellipse([cx - rr + self.S(4), cy - rr + self.S(4), cx + rr - self.S(4), cy + rr - self.S(4)],
                      fill=tuple(min(255, v + 12) for v in self.C["bg"][:3]) + (255,))
        for i in range(1, n + 1):
            r = R * i / n * (0.86 if self.gauge in ("led", "tape") else 1)
            outer = i == n
            shape = d.rectangle if self.gauge in ("led", "tape") else d.ellipse
            shape([cx - r, cy - r, cx + r, cy + r],
                  outline=self.tint(ring_col, 0.30 if outer else 0.12) if not self.theme.get("stroke")
                  else self.tint("accent", 0.8 if outer else 0.4),
                  width=max(1, self.S(2 if outer else 1.2)))
            if i % 2 == 0 or outer:
                self.text(d, cx + r * 0.71 + self.S(4), cy - r * 0.71 - self.S(2),
                          f"{i * 0.5:g}", 13, self.tint("dim", 0.8), "l")
        for a in (0, 90):
            ar = math.radians(a)
            d.line([(cx - R * math.cos(ar), cy - R * math.sin(ar)),
                    (cx + R * math.cos(ar), cy + R * math.sin(ar))],
                   fill=self.tint("white", 0.10), width=max(1, self.S(1)))
        self.text(d, self.S(20), p.iay + self.S(36), "G-FORCE", 15, "dim", track=2)

    def _static_lean(self, img, d, p):
        if self.gauge != "arc" and self.static_lean_styled(img, d, p):
            return
        self.text(d, self.S(22), p.iay + self.S(36), "LEAN", 15, "dim", track=2)
        cx, cy, R = p.iw / 2, p.iay + self.S(228), self.S(150)
        wid = self.S(14)
        self._lean_geom = (cx, cy, R, wid)
        box = [cx - R, cy - R, cx + R, cy + R]
        d.arc(box, 210, 330, fill=self.tint("white", 0.13), width=wid)
        for a in (-60, -45, -30, -15, 0, 15, 30, 45, 60):
            ang = math.radians(-90 + a)
            major = a % 30 == 0
            r1, r2 = R + wid / 2 + self.S(4), R + wid / 2 + self.S(14 if major else 9)
            d.line([(cx + r1 * math.cos(ang), cy + r1 * math.sin(ang)),
                    (cx + r2 * math.cos(ang), cy + r2 * math.sin(ang))],
                   fill=self.C["dim"] if major else self.tint("dim", 0.6),
                   width=max(1, self.S(2.5 if major else 1.5)))
        for m in self.lean_max:              # session max lean, each side
            a = math.radians(-90 + max(-60, min(60, m)))
            r = R - wid / 2 - self.S(8)
            tip = (cx + r * math.cos(a), cy + r * math.sin(a))
            side = (math.cos(a + math.pi / 2), math.sin(a + math.pi / 2))
            back = (cx + (r - self.S(12)) * math.cos(a), cy + (r - self.S(12)) * math.sin(a))
            d.polygon([tip, (back[0] + side[0] * self.S(6), back[1] + side[1] * self.S(6)),
                       (back[0] - side[0] * self.S(6), back[1] - side[1] * self.S(6))],
                      fill=self.tint("accent", 0.7))

    # ------------------------------------------------------------ dynamic layer

    def render_atlas(self, t: float) -> Image.Image:
        img = self.static.copy()
        d = ImageDraw.Draw(img)
        st = self.timing.state(t)
        for p in self.panels:
            self._use(p)
            p.draw(img, d, p, t, st)
        if self.ss > 1:
            img = img.reduce(self.ss)
        return img

    def compose(self, frame: Image.Image, t: float, ghost_frame=None) -> Image.Image:
        """Paste the panels onto a full video frame (used for previews).
        ghost_frame(video_time, w, h) -> RGB array, used when the ghost is showing."""
        atlas = self.render_atlas(t)
        g = self.ghost_info(t)
        if g.active and ghost_frame is not None:
            _, _, w, h = self.ghost_slot()
            arr = np.array(atlas)
            self.paste_ghost(arr, ghost_frame(g.ref_vt, w, h))
            atlas = Image.fromarray(arr, "RGBA")
        out = Image.new("RGBA", (self.canvas_w, self.canvas_h), self.side_colour() + (255,))
        out.paste(frame.convert("RGB"), (self.vid_x0, self.vid_y0))
        out.alpha_composite(self.frame_image())
        for p in self.panels:
            out.alpha_composite(atlas.crop((p.vx, p.ay + p.vy, p.vx + p.vw, p.ay + p.vy + p.vh)), (p.x, p.y))
        return out.convert("RGB")

    def _draw_laps(self, img, d, p: Panel, t: float, st: HudState):
        S, oy, pad = self.S, p.iay, self.S(24)
        right = p.iw - pad
        held = st.finished is not None

        # header: lap counter and delta pill
        y = oy + S(44)
        rb = self.race.board(t) if self.race is not None else None
        if rb and rb["started"] and not rb["finished"]:      # race laps, not RaceBox laps
            x = pad + self.text(d, pad, y, "LAP", 17, "dim", track=2) + S(9)
            x += self.text(d, x, y, str(rb["lap"]), 28, "white")
            self.text(d, x + S(2), y, f"/{rb['laps']}", 18, "dim")
        elif st.phase == "pre" or (st.phase == "post" and not held):
            self.text(d, pad, y, "OUT LAP" if st.phase == "pre" else "IN LAP", 20, "dim", track=2)
        else:
            num = st.finished.number if held else st.lap_number
            x = pad + self.text(d, pad, y, "LAP", 17, "dim", track=2) + S(9)
            x += self.text(d, x, y, str(num), 28, "white")
            self.text(d, x + S(2), y, f"/{st.lap_total}", 18, "dim")
        if st.delta is not None:
            col = self.C["green"] if st.delta < 0 else self.C["red"]
            label = fmt_time(st.delta, plus=True)
            w = self.measure(label, 24) + S(30)
            self.box(d, (right - w, oy + S(18), right, oy + S(52)), fill=col, radius=S(17), solid=False)
            self.text(d, right - w / 2, oy + S(44), label, 24,
                      (255, 255, 255, 255) if self.pill_solid() else col, "m")

        g, items = self._lap_geom, self.lap_items
        # main clock
        if "clock" in g:
            yc = oy + S(g["clock"])
            if held:
                self.text(d, pad - S(3), yc, fmt_time(st.finished.time), 92, st.finished_color)
            elif st.elapsed is not None:
                self.text(d, pad - S(3), yc, fmt_time(st.elapsed, digits=1), 92, "white")
            else:
                self.text(d, pad - S(3), yc, "-:--.-", 92, self.tint("dim", 0.6))

        # last / best (side by side, or whichever one is on)
        if "row" in g:
            y = oy + S(g["row"])
            x = pad
            if "last" in items:
                w = self.text(d, x, y, "LAST", 15, "dim", track=2)
                self.text(d, x + w + S(10), y, fmt_time(st.last_lap), 26, "white")
                x = p.iw / 2 + S(10)
            if "best" in items:
                w = self.text(d, x, y, "BEST", 15, "dim", track=2)
                self.text(d, x + w + S(10), y, fmt_time(st.best_lap), 26,
                          "purple" if st.best_lap else "dim")

        # sectors: a segmented bar that fills as each sector is driven
        n = len(self.timing.sector_gates) + 1 if self.timing.sector_gates else 0
        if not n or "sectors" not in g:
            return
        cells = st.sectors or [None] * n
        gap = S(8)
        cw = (p.iw - 2 * pad - gap * (n - 1)) / n
        top, bh = oy + S(g["sectors"]), S(7)
        k = self.timing.lap_index_at(t)
        ref = self.timing._ref_index(k) if k is not None else None
        ref_secs = self.timing.laps[ref].sectors if ref is not None else None
        for i, c in enumerate(cells):
            x0 = pad + i * (cw + gap)
            self.text(d, x0, top - S(8), f"S{i + 1}", 13, "dim", track=1.5)
            track_col = self.tint("text" if self.light() else "white", 0.12)
            self.box(d, (x0, top, x0 + cw, top + bh), fill=track_col, radius=bh / 2)
            if c and c.status == "done":
                self.box(d, (x0, top, x0 + cw, top + bh), fill=self.col(c.color), radius=bh / 2)
            elif c and c.status == "running" and c.time is not None:
                frac = min(c.time / ref_secs[i], 0.97) if ref_secs else 0.5
                if frac * cw > bh:
                    self.box(d, (x0, top, x0 + cw * frac, top + bh), radius=bh / 2,
                             fill=self.tint("text" if self.light() else "white", 0.75))
            if c and c.time is not None:
                done = c.status == "done"
                self.text(d, x0, top + bh + S(30), fmt_time(c.time, digits=2 if done else 1), 23,
                          c.color if done else self.tint("white", 0.85))

    def _bike_icon(self, facing_right: bool) -> Image.Image:
        """Motorcycle-and-rider silhouette on an accent badge (cached)."""
        key = ("bike", facing_right, round(self.k, 4))
        if key in self._sprites:
            return self._sprites[key]
        R = self.S(15)                        # badge radius
        ring = max(1, self.S(2.5))
        size = 2 * (R + ring) + 2
        im = Image.new("RGBA", (size, size), tuple(self.C["accent"][:3]) + (0,))
        d = ImageDraw.Draw(im)
        c = size / 2
        d.ellipse([c - R - ring, c - R - ring, c + R + ring, c + R + ring], fill=self.C["ring"])
        d.ellipse([c - R, c - R, c + R, c + R], fill=self.C["accent"])
        u = R * 0.66                          # icon units -> pixels
        sx = 1 if facing_right else -1
        P = lambda x, y: (c + sx * x * u, c + y * u + R * 0.12)
        fg = (255, 255, 255, 255)
        bg = tuple(self.C["accent"])
        for wx in (-0.80, 0.82):              # chunky tyres around a hub
            ox, oy = P(wx, 0.42)
            r = 0.44 * u
            d.ellipse([ox - r, oy - r, ox + r, oy + r], fill=fg)
            r2 = 0.20 * u
            d.ellipse([ox - r2, oy - r2, ox + r2, oy + r2], fill=bg)
        # sport-bike body: tail, seat, tank, fairing nose, belly pan
        d.polygon([P(-0.80, 0.30), P(-1.05, -0.20), P(-0.55, -0.16), P(-0.10, -0.34),
                   P(0.45, -0.46), P(0.80, -0.20), P(0.86, 0.04), P(0.62, 0.36),
                   P(0.20, 0.44), P(-0.30, 0.40)], fill=fg)
        # cut-outs between body and wheels so the wheels read as wheels
        for wx in (-0.80, 0.82):
            ox, oy = P(wx, 0.42)
            r = 0.44 * u
            d.arc([ox - r - 0.09 * u, oy - r - 0.09 * u, ox + r + 0.09 * u, oy + r + 0.09 * u],
                  200 if sx > 0 else 200, 340, fill=bg, width=max(1, int(0.09 * u)))
        # rider tucked behind the screen
        d.polygon([P(-0.62, -0.18), P(-0.25, -0.70), P(0.20, -0.82), P(0.48, -0.62),
                   P(0.30, -0.44), P(-0.05, -0.36)], fill=fg)
        hx, hy = P(0.36, -0.90)
        hr = 0.24 * u
        d.ellipse([hx - hr, hy - hr, hx + hr, hy + hr], fill=fg)
        self._sprites[key] = im
        return im

    # ------------------------------------------------------------ ghost lap

    GHOST_SLOT = (8, 8, 464, 261)            # video area inside the panel, reference px (16:9 default)

    def ghost_info(self, t: float) -> GhostState:
        g = self.ghost
        if not g:
            return GhostState(False)
        return ghost_state(self.timing, g, t)

    def ghost_slot(self) -> tuple[int, int, int, int] | None:
        """(x, y, w, h) of the ghost video inside the atlas, output pixels."""
        p = next((p for p in self.panels if p.name == "ghost"), None)
        if p is None:
            return None
        k = p.k / self.ss
        x, y, w, h = getattr(self, "ghost_slot_ref", self.GHOST_SLOT)
        return (int(round(x * k)), p.ay + int(round(y * k)), int(round(w * k)) // 2 * 2,
                int(round(h * k)) // 2 * 2)

    def ghost_mask(self) -> np.ndarray:
        """Rounded-corner mask for the ghost video (cached), 0..1."""
        slot = self.ghost_slot()
        key = ("gmask", slot)
        if key not in self._sprites:
            _, _, w, h = slot
            m = Image.new("L", (w, h), 0)
            ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=self.ghost_radius(), fill=255)
            self._sprites[key] = np.asarray(m, np.float32)[..., None] / 255
        return self._sprites[key]

    def paste_ghost(self, atlas: np.ndarray, frame: np.ndarray) -> None:
        """Put a ghost video frame (h x w x 3) into an RGBA atlas array in place."""
        x, y, w, h = self.ghost_slot()
        m = self.ghost_mask()
        region = atlas[y:y + h, x:x + w]
        region[..., :3] = (frame[:h, :w] * m + region[..., :3] * (1 - m)).astype(np.uint8)
        region[..., 3] = np.maximum(region[..., 3], (m[..., 0] * 255).astype(np.uint8))

    def _draw_ghost(self, img, d, p, t, st):
        g = self.ghost_info(t)
        if not g.active:
            return
        S = self.S
        self.ghost_frame(d, p, self.C["accent"])
        gap = g.gap_m
        if abs(gap) < 0.5:
            label, col = "LEVEL", "white"
        else:
            label = f"{abs(gap):.0f} M {'AHEAD' if gap > 0 else 'BEHIND'}"
            col = "red" if gap > 0 else "green"
        narrow = p.iw < S(420)
        y = p.iay + p.ih - S(11)
        y1 = y - S(23) if narrow else y                     # narrow window: two lines
        w = self.text(d, S(14), y1, "GHOST", 14, "accent", track=2)
        self.text(d, S(14) + w + S(10), y1, f"{g.label}  {fmt_time(g.ref_time)}", 15, "dim", track=1)
        self.text(d, (S(14) if narrow else p.iw - S(14)), y, label, 16, col, "l" if narrow else "r", track=1)

    # ------------------------------------------------------------ race tower

    # ------------------------------------------------------------ map + standings in one

    def _racemap_parts(self, p):
        """The map and standings areas inside the combined race panel: the map on
        top, the standings under it, and the class key along the bottom."""
        kref = p.k / p.ss
        map_h, key_h = self._racemap
        mh = int(round(map_h * kref))
        kh = int(round(key_h * kref))
        mp = Panel("map", "map", p.x, p.y, p.w, mh, ay=p.ay, k=p.k, ss=p.ss)
        tw = Panel("race", "race", p.x, p.y, p.w, p.h - mh - kh, ay=p.ay + mh, k=p.k, ss=p.ss)
        return mp, tw, mh

    def _static_racemap(self, img, d, p):
        mp, tw, top = self._racemap_parts(p)
        self._static_map(img, d, mp)
        S = self.S
        if self._racemap[1]:                     # the class key, along the bottom under the standings
            self.class_legend(d, p, S(14), p.iay + p.ih - S(12))
        d.line([(S(14), tw.iay), (p.iw - S(14), tw.iay)],
               fill=self.tint("text" if self.light() else "white", 0.14), width=max(1, S(1)))

    def _draw_racemap(self, img, d, p, t, st):
        mp, tw, _ = self._racemap_parts(p)
        self._draw_map(img, d, mp, t, st)
        self._draw_race(img, d, tw, t, st)

    def _draw_race(self, img, d, p, t, st):
        S, oy = self.S, p.iay
        b = self.race.board(t, self.race_cls)
        style = self.theme.get("tower", "default")
        style = style if style in ("broadcast", "hud", "arcade", "pitboard") else "default"
        self.tower_header(d, p, b, style)
        top = oy + S(62 + self.race_key_h)
        rh = max(S(RACE_RH) * 0.6, (p.ih - S(70 + self.race_key_h)) / max(self.race_rows, 1))   # taller: rows spread
        rows = b["rows"]
        n = len(rows)
        me = next((i for i, r in enumerate(rows) if r["me"]), 0)
        if n > self.race_rows:                      # top three plus the riders around you
            k = self.race_rows - 3
            lo = min(max(me - k // 2, 3), n - k)
            shown = list(range(3)) + list(range(lo, lo + k))
        else:
            shown = list(range(n))
        slot = {i: j for j, i in enumerate(shown)}
        for i in sorted(shown, key=lambda i: rows[i]["me"]):     # draw my row last, on top
            r = rows[i]
            src = r["row"]
            if n > self.race_rows:                  # animate only between shown places
                a, bb = int(math.floor(src)), int(math.ceil(src))
                src = slot.get(a, slot[i]) + (slot.get(bb, slot[i]) - slot.get(a, slot[i])) * (src - a)
            ry = top + src * rh
            self.tower_row(d, p, i, r, ry, rh, t, style)

    def _draw_map(self, img, d, p, t, st):
        x = float(np.interp(t, self.tel.t, self.timing.x))
        y = float(np.interp(t, self.tel.t, self.timing.y))
        px, py = self._map_xform(x, y)
        # face the way the bike is moving across the map (on screen)
        xa = self._map_xform(float(np.interp(t - 0.25, self.tel.t, self.timing.x)),
                             float(np.interp(t - 0.25, self.tel.t, self.timing.y)))[0]
        xb = self._map_xform(float(np.interp(t + 0.25, self.tel.t, self.timing.x)),
                             float(np.interp(t + 0.25, self.tel.t, self.timing.y)))[0]
        if abs(xb - xa) > self.S(0.3):
            self._facing = xb > xa
        x0_, y0_ = self._map_xform(float(np.interp(t - 0.4, self.tel.t, self.timing.x)),
                                   float(np.interp(t - 0.4, self.tel.t, self.timing.y)))
        heading = math.atan2(py - y0_, px - x0_) if math.hypot(px - x0_, py - y0_) > self.S(1) else None
        if heading is None:
            heading = getattr(self, "_heading", None)
        else:
            self._heading = heading
        if self.race is not None and self.race_field and getattr(self, "_lap_path", None) is not None:
            self._map_panel = p
            fr, bx, by = self._lap_path
            for num, f in self.race.field(t, self.race_cls):
                fx, fy = self._map_xform(float(np.interp(f, fr, bx)), float(np.interp(f, fr, by)))
                self.field_dot(d, fx, fy, num, self.race.rider(num).cls)
            self.you_ring(d, px, py)
        self.draw_marker(img, d, p, px, py, heading, getattr(self, "_facing", True))

    def _draw_speed(self, img, d, p, t, st):
        if self.gauge != "arc" and self.draw_speed_styled(img, d, p, t, st):
            return
        cx, cy, R, wid = self._gauge_geom(p)
        v = float(np.interp(t, self.tel.t, self.tel.speed_kmh)) * self.speed_factor
        frac = min(max(v / self.speed_max, 0.0), 1.0)
        arc = self._speed_arc
        x0, y0 = int(round(cx - arc.width / 2)), int(round(cy - arc.height / 2))
        if frac > 0.002:
            ring = Image.new("L", arc.size, 0)
            rd = ImageDraw.Draw(ring)
            c = arc.width / 2
            rd.arc([c - R, c - R, c + R, c + R], GAUGE_START, GAUGE_START + GAUGE_SWEEP * frac,
                   fill=255, width=wid)
            for f in (0.0, frac):            # round caps
                a = self._ang(f)
                ex, ey = c + (R - wid / 2) * math.cos(a), c + (R - wid / 2) * math.sin(a)
                rd.ellipse([ex - wid / 2, ey - wid / 2, ex + wid / 2, ey + wid / 2], fill=255)
            img.paste(arc, (x0, y0), ring)
        # pointer across the ring
        a = self._ang(frac)
        r1, r2 = R - wid / 2 - self.S(10), R + wid / 2 - self.S(1)
        d.line([(cx + (r1 - wid / 2) * math.cos(a), cy + (r1 - wid / 2) * math.sin(a)),
                (cx + r2 * math.cos(a), cy + r2 * math.sin(a))],
               fill=self.C["white"], width=self.S(4))
        label = f"{v:.0f}"
        self.text(d, cx, cy + self.S(26 if len(label) < 3 else 22), label,
                  72 if len(label) < 3 else 56, "white", "m")

    def _draw_g(self, img, d, p, t, st):
        cx, cy, R = self._g_geom
        tt = self.tel.t
        trail = np.linspace(t - 1.0, t, 14)
        gl = np.clip(np.interp(trail, tt, self.tel.g_lat) / self.gmax, -1.08, 1.08)
        gn = np.clip(np.interp(trail, tt, self.tel.g_long) / self.gmax, -1.08, 1.08)
        for i in range(len(gl) - 1):
            f = i / len(gl)
            r = self.S(2 + 3 * f)
            px, py = cx + gl[i] * R, cy - gn[i] * R
            d.ellipse([px - r, py - r, px + r, py + r], fill=self.tint("accent", 0.15 + 0.55 * f))
        px, py = cx + gl[-1] * R, cy - gn[-1] * R
        self.dot(img, d, px, py, 9, "accent", p)
        a = float(np.interp(t, tt, self.tel.g_lat))
        b = float(np.interp(t, tt, self.tel.g_long))
        w = self.text(d, p.iw - self.S(20), p.iay + self.S(38), "G", 16, "dim", "r")
        self.text(d, p.iw - self.S(20) - w - self.S(4), p.iay + self.S(38), f"{math.hypot(a, b):.2f}",
                  26, "white", "r")
        y = p.iay + p.ih - self.S(16)
        w = self.text(d, self.S(20), y, "LAT", 13, "dim", track=1.5)
        self.text(d, self.S(20) + w + self.S(6), y, f"{abs(a):.2f}", 18, "white")
        val = f"{abs(b):.2f}"
        w = self.text(d, p.iw - self.S(20), y, val, 18, "white", "r")
        self.text(d, p.iw - self.S(20) - w - self.S(6), y, "ACC" if b >= 0 else "BRK", 13,
                  "dim", "r", track=1.5)

    def _draw_lean(self, img, d, p, t, st):
        if self.gauge != "arc" and self.draw_lean_styled(img, d, p, t, st):
            return
        cx, cy, R, wid = self._lean_geom
        lean = float(np.interp(t, self.tel.t, self.tel.lean_deg))
        a = max(-60, min(60, lean))
        box = [cx - R, cy - R, cx + R, cy + R]
        if abs(a) > 0.5:
            start, end = (270, 270 + a) if a >= 0 else (270 + a, 270)
            d.arc(box, start, end, fill=self.C["accent"], width=wid)
            ang = math.radians(270 + a)
            ex, ey = cx + (R - wid / 2) * math.cos(ang), cy + (R - wid / 2) * math.sin(ang)
            d.ellipse([ex - wid / 2, ey - wid / 2, ex + wid / 2, ey + wid / 2], fill=self.C["accent"])
        ang = math.radians(-90 + a)
        r1, r2 = R - wid - self.S(6), R + self.S(1)
        d.line([(cx + r1 * math.cos(ang), cy + r1 * math.sin(ang)),
                (cx + r2 * math.cos(ang), cy + r2 * math.sin(ang))], fill=self.C["white"], width=self.S(4))
        side = "R" if lean > 0.5 else "L" if lean < -0.5 else ""
        w = self.text(d, cx, cy - self.S(4), f"{abs(lean):.0f}°", 64, "white", "m")
        if side:
            self.text(d, cx + w / 2 + self.S(4), cy - self.S(4), side, 24, "dim")
        if self.tel.g_long is not None:
            g = float(np.interp(t, self.tel.t, self.tel.g_long))
            w = self.text(d, p.iw - self.S(22), p.iay + self.S(36), f"{abs(g):.2f} G", 20, "white", "r")
            self.text(d, p.iw - self.S(22) - w - self.S(6), p.iay + self.S(36), "ACC" if g >= 0 else "BRK",
                      13, "dim", "r", track=1.5)
