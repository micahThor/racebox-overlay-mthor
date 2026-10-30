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
from .timing import HudState, Timing, fmt_time

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


def find_font(override: str | None = None) -> str | None:
    for p in ([override] if override else []) + FONT_CANDIDATES:
        if p and Path(p).exists():
            return p
    return None


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

    @property
    def iw(self) -> int:
        return self.w * self.ss

    @property
    def ih(self) -> int:
        return self.h * self.ss

    @property
    def iay(self) -> int:
        return self.ay * self.ss


class Hud:
    def __init__(self, tel: Telemetry, timing: Timing, width: int, height: int,
                 units: str = "mph", scale: float = 1.0, font: str | None = None,
                 hide: set[str] | None = None, theme: str | dict | None = None,
                 layout: dict | None = None, speed_max: float | None = None,
                 supersample: int = 2):
        """layout: {panel: {"x": 0..1, "y": 0..1, "scale": float, "visible": bool}},
        x/y being the panel's top-left corner as a fraction of the frame.
        speed_max: top of the speedometer in display units (70 mph / 120 km/h)."""
        self.spec = dict(tel=tel, timing=timing, width=width, height=height, units=units,
                         scale=scale, font=font, hide=hide, theme=theme, layout=layout,
                         speed_max=speed_max, supersample=supersample)
        self.tel, self.timing = tel, timing
        self.W, self.H = width, height
        self.ss = max(1, int(supersample))
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

        self.panels = self._layout()
        if not self.panels:   # everything hidden: keep a 2x2 transparent stub
            self.panels = [Panel("none", "none", 0, 0, 2, 2, k=self.k, ss=self.ss,
                                 draw=lambda *a: None)]
        self.atlas_w = max(p.w for p in self.panels)
        self.atlas_h = sum(p.h for p in self.panels)
        y = 0
        for p in self.panels:
            p.ay = y
            y += p.h
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
        px = max(6, self.S(size))
        if px not in self._fonts:
            self._fonts[px] = (ImageFont.truetype(self.font_path, px) if self.font_path
                               else ImageFont.load_default(px))
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
        for c, w in zip(s, widths):
            off = (w - sp - L(f, c)) / 2 if c.isdigit() else 0
            d.text((cx + off, y), c, font=f, fill=fill, anchor="ls", **extra)
            cx += w
        return total

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

    def _layout(self) -> list[Panel]:
        W, H = self.W, self.H
        specs = []     # (key, name, ref_w, ref_h, corner, draw)
        if self.timing.laps:
            specs.append(("laps", "laps", 520, 272 if self.timing.sector_gates else 200, "tl",
                          self._draw_laps))
        x, y = self.timing.x, self.timing.y
        aspect = (np.ptp(y) + 1) / (np.ptp(x) + 1)
        mw, mh = 400, int(min(max(400 * aspect, 190), 400))
        if aspect > 1.2:
            mw, mh = int(min(max(400 / aspect, 190), 400)), 400
        specs.append(("map", "map", mw, mh, "tr", self._draw_map))
        specs.append(("speed", "speed", 330, 312, "bl", self._draw_speed))
        if self.has_g:
            specs.append(("gforce", "gforce", 290, 312, "br", self._draw_g))
        elif self.has_lean:
            specs.append(("gforce", "lean", 380, 262, "br", self._draw_lean))

        panels = []
        for key, name, rw, rh, corner, fn in specs:
            if key in self.hide:
                continue
            cfg = self.layout.get(key, {})
            k_out = self.base_k * float(cfg.get("scale", 1.0))
            w, h = max(2, int(round(rw * k_out))), max(2, int(round(rh * k_out)))
            m = int(round(36 * self.base_k))
            if "x" in cfg and "y" in cfg:
                px, py = float(cfg["x"]) * W, float(cfg["y"]) * H
            else:
                px = m if corner[1] == "l" else W - m - w
                py = m if corner[0] == "t" else H - m - h
            px = int(round(min(max(px, 0), max(W - w, 0))))
            py = int(round(min(max(py, 0), max(H - h, 0))))
            panels.append(Panel(name, key, px, py, w, h, k=k_out * self.ss, ss=self.ss, draw=fn))
        return panels

    def describe(self) -> list[dict]:
        """Panel rectangles in output pixels, for the GUI."""
        return [{"key": p.key, "name": p.name, "x": p.x, "y": p.y, "w": p.w, "h": p.h, "ay": p.ay}
                for p in self.panels]

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
            self.card(img, d, p)
            getattr(self, f"_static_{p.name}", lambda *a: None)(img, d, p)
        return img

    def _static_laps(self, img, d, p):
        pad = self.S(24)
        d.line([(pad, p.iay + self.S(148)), (p.iw - pad, p.iay + self.S(148))],
               fill=self.tint("white", 0.12), width=max(1, self.S(1)))

    def _static_map(self, img, d, p):
        pad = self.S(26)
        x, y = self.timing.x, self.timing.y
        s = min((p.iw - 2 * pad) / max(np.ptp(x), 1), (p.ih - 2 * pad) / max(np.ptp(y), 1))
        cx, cy = (x.min() + x.max()) / 2, (y.min() + y.max()) / 2
        oy = p.iay
        self._map_xform = lambda xx, yy: (p.iw / 2 + (xx - cx) * s, oy + p.ih / 2 - (yy - cy) * s)

        step = max(1, len(x) // 4000)
        pts = list(zip(*self._map_xform(x[::step], y[::step])))
        d.line(pts, fill=self.tint("white", 0.16), width=max(1, self.S(3)), joint="curve")

        laps = self.timing.laps
        if laps:
            best = min(laps, key=lambda l: l.time)
            m = (self.tel.t >= best.t_start) & (self.tel.t <= best.t_end)
            line = list(zip(*self._map_xform(x[m], y[m])))
            # soft glow under the racing line
            layer = Image.new("RGBA", img.size, tuple(self.C["line"][:3]) + (0,))
            ImageDraw.Draw(layer).line(line, fill=tuple(self.C["line"][:3]) + (110,),
                                       width=self.S(12), joint="curve")
            box = (0, p.iay, p.iw, p.iay + p.ih)
            region = layer.crop(box).filter(ImageFilter.GaussianBlur(self.S(5)))
            base = img.crop(box)
            base.alpha_composite(region)
            img.paste(base, box[:2])
            d.line(line, fill=self.C["line"], width=max(2, self.S(5)), joint="curve")

        for g in self.timing.sector_gates:
            gx, gy = self._map_xform(g.x, g.y)
            r = self.S(5)
            d.ellipse([gx - r, gy - r, gx + r, gy + r], fill=self.C["bg"] if self._has_card() else (0, 0, 0, 200),
                      outline=self.C["dim"], width=max(1, self.S(2)))
        g = self.timing.start_gate
        if g is not None:
            gx, gy = self._map_xform(g.x, g.y)
            nx, ny = -g.dy, -g.dx          # perpendicular to travel, in screen coords (y flipped)
            tx, ty = g.dx, -g.dy
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
        for i in range(1, n + 1):
            r = R * i / n
            outer = i == n
            d.ellipse([cx - r, cy - r, cx + r, cy + r],
                      outline=self.tint("white", 0.30 if outer else 0.12),
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

    def compose(self, frame: Image.Image, t: float) -> Image.Image:
        """Paste the panels onto a full video frame (used for previews)."""
        atlas = self.render_atlas(t)
        out = frame.convert("RGBA")
        for p in self.panels:
            out.alpha_composite(atlas.crop((0, p.ay, p.w, p.ay + p.h)), (p.x, p.y))
        return out.convert("RGB")

    def _draw_laps(self, img, d, p: Panel, t: float, st: HudState):
        S, oy, pad = self.S, p.iay, self.S(24)
        right = p.iw - pad
        held = st.finished is not None

        # header: lap counter and delta pill
        y = oy + S(44)
        if st.phase == "pre" or (st.phase == "post" and not held):
            self.text(d, pad, y, "OUT LAP" if st.phase == "pre" else "IN LAP", 20, "dim", track=2)
        else:
            num = st.finished.number if held else st.lap_number
            x = pad + self.text(d, pad, y, "LAP", 17, "dim", track=2) + S(9)
            x += self.text(d, x, y, str(num), 28, "white")
            self.text(d, x + S(2), y, f"/{st.lap_total}", 18, "dim")
        if st.delta is not None:
            col = self.C["green"] if st.delta < 0 else self.C["red"]
            label = fmt_time(st.delta, plus=True)
            w = self.font(24).getlength(label) + S(30)
            d.rounded_rectangle([right - w, oy + S(18), right, oy + S(52)], radius=S(17), fill=col)
            self.text(d, right - w / 2, oy + S(44), label, 24, (255, 255, 255, 255), "m")

        # main clock
        if held:
            self.text(d, pad - S(3), oy + S(128), fmt_time(st.finished.time), 92, st.finished_color)
        elif st.elapsed is not None:
            self.text(d, pad - S(3), oy + S(128), fmt_time(st.elapsed, digits=1), 92, "white")
        else:
            self.text(d, pad - S(3), oy + S(128), "-:--.-", 92, self.tint("dim", 0.6))

        # last / best
        y = oy + S(184)
        w = self.text(d, pad, y, "LAST", 15, "dim", track=2)
        self.text(d, pad + w + S(10), y, fmt_time(st.last_lap), 26, "white")
        bx = p.iw / 2 + S(10)
        w = self.text(d, bx, y, "BEST", 15, "dim", track=2)
        self.text(d, bx + w + S(10), y, fmt_time(st.best_lap), 26,
                  "purple" if st.best_lap else "dim")

        # sectors: a segmented bar that fills as each sector is driven
        n = len(self.timing.sector_gates) + 1 if self.timing.sector_gates else 0
        if not n:
            return
        cells = st.sectors or [None] * n
        gap = S(8)
        cw = (p.iw - 2 * pad - gap * (n - 1)) / n
        top, bh = oy + S(222), S(7)
        k = self.timing.lap_index_at(t)
        ref = self.timing._ref_index(k) if k is not None else None
        ref_secs = self.timing.laps[ref].sectors if ref is not None else None
        for i, c in enumerate(cells):
            x0 = pad + i * (cw + gap)
            self.text(d, x0, top - S(8), f"S{i + 1}", 13, "dim", track=1.5)
            d.rounded_rectangle([x0, top, x0 + cw, top + bh], radius=bh / 2, fill=self.tint("white", 0.12))
            if c and c.status == "done":
                d.rounded_rectangle([x0, top, x0 + cw, top + bh], radius=bh / 2, fill=self.col(c.color))
            elif c and c.status == "running" and c.time is not None:
                frac = min(c.time / ref_secs[i], 0.97) if ref_secs else 0.5
                if frac * cw > bh:
                    d.rounded_rectangle([x0, top, x0 + cw * frac, top + bh], radius=bh / 2,
                                        fill=self.tint("white", 0.75))
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

    def _draw_map(self, img, d, p, t, st):
        x = float(np.interp(t, self.tel.t, self.timing.x))
        y = float(np.interp(t, self.tel.t, self.timing.y))
        px, py = self._map_xform(x, y)
        # face the way the bike is moving across the map
        xa = float(np.interp(t - 0.25, self.tel.t, self.timing.x))
        xb = float(np.interp(t + 0.25, self.tel.t, self.timing.x))
        if abs(xb - xa) > 0.3:
            self._facing = xb > xa
        facing = getattr(self, "_facing", True)
        self.stamp(img, self.glow(self.C["accent"], 15), px, py, p)
        self.stamp(img, self._bike_icon(facing), px, py, p)

    def _draw_speed(self, img, d, p, t, st):
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
        # extra channels in the bottom corners
        y = p.iay + p.ih - self.S(16)
        if self.tel.alt is not None:
            a_ = float(np.interp(t, self.tel.t, self.tel.alt))
            val = f"{a_ * 3.28084:.0f} ft" if self.units == "mph" else f"{a_:.0f} m"
            w = self.text(d, self.S(18), y, "ALT", 13, "dim", track=1.5)
            self.text(d, self.S(18) + w + self.S(6), y, val, 18, "white")
        if self.tel.yaw_rate is not None:
            val = f"{float(np.interp(t, self.tel.t, self.tel.yaw_rate)):+.0f}°/s"
            w = self.text(d, p.iw - self.S(18), y, val, 18, "white", "r")
            self.text(d, p.iw - self.S(18) - w - self.S(6), y, "YAW", 13, "dim", "r", track=1.5)

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
