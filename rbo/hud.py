"""Draws the overlay panels with Pillow.

Each panel (lap timing, track map, speed, G-force / lean) is a translucent card
anchored to a corner of the frame. To keep the ffmpeg pipe small, the panels
are packed into one compact "atlas" image per frame; ffmpeg crops each panel
back out and overlays it at its spot. Everything static (card backgrounds,
labels, the track outline, gauge rings) is drawn once and copied per frame.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .telemetry import Telemetry
from .timing import Timing, HudState, fmt_time

COLORS = {
    "bg": (10, 12, 16, 200),
    "edge": (255, 255, 255, 40),
    "white": (245, 247, 250, 255),
    "dim": (150, 156, 168, 255),
    "faint": (255, 255, 255, 60),
    "accent": (255, 92, 40, 255),
    "purple": (178, 92, 255, 255),
    "green": (40, 205, 110, 255),
    "yellow": (245, 196, 30, 255),
    "red": (235, 64, 52, 255),
}

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


def tint(color, opacity: float):
    """A colour as it looks laid over the card. Drawing a translucent colour
    straight onto the card would replace the card's alpha and punch a hole in
    it, so blend it into the card colour instead and keep the card's alpha."""
    bg = COLORS["bg"]
    c = COLORS[color] if isinstance(color, str) else color
    o = opacity * (c[3] / 255 if len(c) > 3 else 1)
    return tuple(int(round(o * c[i] + (1 - o) * bg[i])) for i in range(3)) + (max(bg[3], int(o * 255)),)


def find_font(override: str | None = None) -> str | None:
    for p in ([override] if override else []) + FONT_CANDIDATES:
        if p and Path(p).exists():
            return p
    return None


@dataclass
class Panel:
    name: str
    x: int          # position in the output frame
    y: int
    w: int
    h: int
    ay: int = 0     # vertical offset inside the atlas
    draw: Callable | None = None


class Hud:
    def __init__(self, tel: Telemetry, timing: Timing, width: int, height: int,
                 units: str = "mph", scale: float = 1.0, font: str | None = None,
                 hide: set[str] | None = None):
        self.tel, self.timing = tel, timing
        self.W, self.H = width, height
        self.k = min(width, height) / 1080 * scale
        self.units = units
        self.hide = hide or set()
        self.font_path = find_font(font)
        self._fonts: dict[int, ImageFont.FreeTypeFont] = {}

        self.speed_factor = 0.621371 if units == "mph" else 1.0
        self.vmax = float(np.percentile(tel.speed_kmh, 99.5)) * self.speed_factor
        self.has_g = tel.g_lat is not None and tel.g_long is not None and not tel.is_bike
        self.has_lean = tel.is_bike
        if self.has_g:
            gmag = np.hypot(tel.g_lat, tel.g_long)
            self.gmax = max(1.0, math.ceil(float(np.percentile(gmag, 99.5)) * 2) / 2)
        self.lean_max = (float(np.percentile(np.abs(tel.lean_deg), 99.5)) if self.has_lean else 0)

        self.panels = self._layout()
        self.atlas_w = max(p.w for p in self.panels)
        self.atlas_h = sum(p.h for p in self.panels)
        y = 0
        for p in self.panels:
            p.ay = y
            y += p.h
        self.static = self._draw_static()

    # ------------------------------------------------------------ helpers

    def S(self, v: float) -> int:
        return int(round(v * self.k))

    def font(self, size: float) -> ImageFont.FreeTypeFont:
        px = max(8, self.S(size))
        if px not in self._fonts:
            self._fonts[px] = (ImageFont.truetype(self.font_path, px) if self.font_path
                               else ImageFont.load_default(px))
        return self._fonts[px]

    def text(self, d: ImageDraw.ImageDraw, x: float, y: float, s: str, size: float,
             fill="white", anchor: str = "l"):
        """Draw text at baseline y. Digits get a fixed width so running times
        don't jitter left and right as they count."""
        f = self.font(size)
        fill = COLORS.get(fill, fill) if isinstance(fill, str) else fill
        dw = max(f.getlength(c) for c in "0123456789")
        widths = [dw if c.isdigit() else f.getlength(c) for c in s]
        total = sum(widths)
        cx = x - total if anchor == "r" else x - total / 2 if anchor == "m" else x
        for c, w in zip(s, widths):
            off = (w - f.getlength(c)) / 2 if c.isdigit() else 0
            d.text((cx + off, y), c, font=f, fill=fill, anchor="ls")
            cx += w
        return total

    def card(self, d, p: Panel):
        oy = p.ay
        d.rounded_rectangle([0, oy, p.w - 1, oy + p.h - 1], radius=self.S(18),
                            fill=COLORS["bg"], outline=COLORS["edge"], width=max(1, self.S(1.5)))

    # ------------------------------------------------------------ layout

    def _layout(self) -> list[Panel]:
        S, W, H = self.S, self.W, self.H
        m = S(36)
        panels = []
        if self.timing.laps and "laps" not in self.hide:
            h = 340 if self.timing.sector_gates else 250
            panels.append(Panel("laps", m, m, S(560), S(h), draw=self._draw_laps))
        if "map" not in self.hide:
            x, y = self.timing.x, self.timing.y
            aspect = (np.ptp(y) + 1) / (np.ptp(x) + 1)
            w = 420
            h = int(min(max(w * aspect, 200), 420))
            if aspect > 1.2:
                h, w = 420, int(min(max(420 / aspect, 200), 420))
            panels.append(Panel("map", W - m - S(w), m, S(w), S(h), draw=self._draw_map))
        if "speed" not in self.hide:
            panels.append(Panel("speed", m, H - m - S(230), S(400), S(230), draw=self._draw_speed))
        if self.has_g and "gforce" not in self.hide:
            panels.append(Panel("gforce", W - m - S(320), H - m - S(330), S(320), S(330),
                                draw=self._draw_g))
        elif self.has_lean and "gforce" not in self.hide:
            panels.append(Panel("lean", W - m - S(400), H - m - S(280), S(400), S(280),
                                draw=self._draw_lean))
        return panels

    # ------------------------------------------------------------ static layer

    def _draw_static(self) -> Image.Image:
        img = Image.new("RGBA", (self.atlas_w, self.atlas_h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        for p in self.panels:
            self.card(d, p)
            getattr(self, f"_static_{p.name}", lambda d, p: None)(d, p)
        return img

    def _static_map(self, d, p):
        pad = self.S(26)
        x, y = self.timing.x, self.timing.y
        sx = (p.w - 2 * pad) / max(np.ptp(x), 1)
        sy = (p.h - 2 * pad) / max(np.ptp(y), 1)
        s = min(sx, sy)
        cx = (x.min() + x.max()) / 2
        cy = (y.min() + y.max()) / 2
        self._map_xform = lambda xx, yy: (p.w / 2 + (xx - cx) * s, p.ay + p.h / 2 - (yy - cy) * s)

        step = max(1, len(x) // 3000)
        pts = list(zip(*self._map_xform(x[::step], y[::step])))
        d.line(pts, fill=tint("white", 0.22), width=max(1, self.S(3)), joint="curve")
        laps = self.timing.laps
        if laps:
            best = min(laps, key=lambda l: l.time)
            m = (self.tel.t >= best.t_start) & (self.tel.t <= best.t_end)
            bx, by = self._map_xform(x[m], y[m])
            d.line(list(zip(bx, by)), fill=(230, 233, 240, 235), width=max(2, self.S(5)), joint="curve")
        gates = ([self.timing.start_gate] if self.timing.start_gate else []) + self.timing.sector_gates
        for i, g in enumerate(gates):
            gx, gy = self._map_xform(g.x, g.y)
            nx, ny = -g.dy, g.dx            # perpendicular, map y is flipped
            L = self.S(16 if i == 0 else 10)
            col = COLORS["white"] if i == 0 else COLORS["dim"]
            d.line([(gx - nx * L, gy + ny * L), (gx + nx * L, gy - ny * L)], fill=col,
                   width=max(2, self.S(4 if i == 0 else 3)))

    def _static_speed(self, d, p):
        pass

    def _static_gforce(self, d, p):
        cx, cy, R = p.w / 2, p.ay + self.S(180), self.S(118)
        self._g_geom = (cx, cy, R)
        n = int(self.gmax / 0.5)
        for i in range(1, n + 1):
            r = R * i / n
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=tint("white", 0.22),
                      width=max(1, self.S(2 if i == n else 1.2)))
        d.line([(cx - R, cy), (cx + R, cy)], fill=tint("white", 0.22), width=max(1, self.S(1)))
        d.line([(cx, cy - R), (cx, cy + R)], fill=tint("white", 0.22), width=max(1, self.S(1)))
        self.text(d, self.S(22), p.ay + self.S(40), "G-FORCE", 22, "dim")
        self.text(d, cx + R + self.S(4), cy - R + self.S(14), f"{self.gmax:g}G", 16, "dim", "l")

    def _static_lean(self, d, p):
        self.text(d, self.S(22), p.ay + self.S(40), "LEAN", 22, "dim")
        cx, cy, R = p.w / 2, p.ay + self.S(250), self.S(150)
        self._lean_geom = (cx, cy, R)
        box = [cx - R, cy - R, cx + R, cy + R]
        d.arc(box, 180 + 30, 360 - 30, fill=tint("white", 0.18), width=self.S(14))
        for a in (-60, -45, -30, -15, 0, 15, 30, 45, 60):
            ang = math.radians(-90 + a)
            r1, r2 = R + self.S(10), R + self.S(18 if a % 30 == 0 else 14)
            d.line([(cx + r1 * math.cos(ang), cy + r1 * math.sin(ang)),
                    (cx + r2 * math.cos(ang), cy + r2 * math.sin(ang))],
                   fill=COLORS["dim"], width=max(1, self.S(2)))

    # ------------------------------------------------------------ dynamic layer

    def render_atlas(self, t: float) -> Image.Image:
        img = self.static.copy()
        d = ImageDraw.Draw(img)
        st = self.timing.state(t)
        for p in self.panels:
            p.draw(d, p, t, st)
        return img

    def compose(self, frame: Image.Image, t: float) -> Image.Image:
        """Paste the panels onto a full video frame (used for previews)."""
        atlas = self.render_atlas(t)
        out = frame.convert("RGBA")
        for p in self.panels:
            out.alpha_composite(atlas.crop((0, p.ay, p.w, p.ay + p.h)), (p.x, p.y))
        return out.convert("RGB")

    def _draw_laps(self, d, p: Panel, t: float, st: HudState):
        S, oy, pad = self.S, p.ay, self.S(26)
        right = p.w - pad
        held = st.finished is not None

        # header: lap number and delta
        if st.phase == "pre":
            head = "OUT LAP"
        elif st.phase == "post" and not held:
            head = "IN LAP"
        else:
            num = st.finished.number if held else st.lap_number
            head = f"LAP {num}/{st.lap_total}"
        self.text(d, pad, oy + S(50), head, 30, "dim")

        if st.delta is not None:
            col = COLORS["green"] if st.delta < 0 else COLORS["red"]
            label = fmt_time(st.delta, plus=True)
            w = self.font(30).getlength(label) + S(28)
            d.rounded_rectangle([right - w, oy + S(18), right, oy + S(60)], radius=S(10), fill=col)
            self.text(d, right - w / 2, oy + S(50), label, 30, "white", "m")

        # main clock
        if held:
            self.text(d, pad, oy + S(150), fmt_time(st.finished.time), 104, st.finished_color)
        elif st.elapsed is not None:
            self.text(d, pad, oy + S(150), fmt_time(st.elapsed, digits=1), 104, "white")
        else:
            self.text(d, pad, oy + S(150), "--:--.-", 104, "dim")

        # last / best
        y = oy + S(205)
        self.text(d, pad, y, "LAST", 20, "dim")
        self.text(d, pad + S(62), y, fmt_time(st.last_lap), 30, "white")
        self.text(d, pad + S(270), y, "BEST", 20, "dim")
        self.text(d, pad + S(334), y, fmt_time(st.best_lap), 30, "purple" if st.best_lap else "dim")

        # sectors
        cells = st.sectors or [None] * (len(self.timing.sector_gates) + 1 if self.timing.sector_gates else 0)
        if cells:
            gap = S(8)
            n = len(cells)
            cw = (p.w - 2 * pad - gap * (n - 1)) / n
            top, bot = oy + S(234), oy + p.h - S(22)
            for i, c in enumerate(cells):
                x0 = pad + i * (cw + gap)
                d.rounded_rectangle([x0, top, x0 + cw, bot], radius=S(8), fill=tint("white", 0.08))
                color = COLORS.get(c.color if c else "dim")
                if c and c.status == "done":
                    d.rounded_rectangle([x0, top, x0 + cw, top + S(6)], radius=S(3), fill=color)
                self.text(d, x0 + S(12), top + S(34), f"S{i + 1}", 18, "dim")
                if c and c.time is not None:
                    val = fmt_time(c.time, digits=2 if c.status == "done" else 1)
                    self.text(d, x0 + cw - S(12), bot - S(16), val, 30,
                              c.color if c.status == "done" else "white", "r")

    def _draw_map(self, d, p, t, st):
        x = float(np.interp(t, self.tel.t, self.timing.x))
        y = float(np.interp(t, self.tel.t, self.timing.y))
        px, py = self._map_xform(x, y)
        r = self.S(10)
        d.ellipse([px - r - self.S(3), py - r - self.S(3), px + r + self.S(3), py + r + self.S(3)],
                  fill=(255, 255, 255, 255))
        d.ellipse([px - r, py - r, px + r, py + r], fill=COLORS["accent"])

    def _draw_speed(self, d, p, t, st):
        S, oy, pad = self.S, p.ay, self.S(26)
        v = float(np.interp(t, self.tel.t, self.tel.speed_kmh)) * self.speed_factor
        self.text(d, S(270), oy + S(140), f"{v:.0f}", 140, "white", "r")
        self.text(d, S(284), oy + S(140), "MPH" if self.units == "mph" else "KM/H", 30, "dim")
        # speed bar
        bx0, bx1, by = pad, p.w - pad, oy + S(162)
        d.rounded_rectangle([bx0, by, bx1, by + S(10)], radius=S(5), fill=tint("white", 0.14))
        frac = min(max(v / max(self.vmax, 1), 0), 1)
        if frac > 0.01:
            d.rounded_rectangle([bx0, by, bx0 + (bx1 - bx0) * frac, by + S(10)], radius=S(5),
                                fill=COLORS["accent"])
        # extra channels
        y = oy + S(208)
        items = []
        if self.tel.alt is not None:
            a = float(np.interp(t, self.tel.t, self.tel.alt))
            items.append(("ALT", f"{a * 3.28084:.0f} ft" if self.units == "mph" else f"{a:.0f} m"))
        if self.tel.yaw_rate is not None:
            items.append(("YAW", f"{float(np.interp(t, self.tel.t, self.tel.yaw_rate)):+.0f}°/s"))
        if self.tel.g_long is not None and not self.has_g and not self.has_lean:
            items.append(("LON G", f"{float(np.interp(t, self.tel.t, self.tel.g_long)):+.2f}"))
        x = pad
        for label, val in items:
            need = self.font(18).getlength(label) + S(8) + self.font(24).getlength(val)
            if x + need > p.w - pad:
                break
            x += self.text(d, x, y, label, 18, "dim") + S(8)
            x += self.text(d, x, y, val, 24, "white") + S(24)

    def _draw_g(self, d, p, t, st):
        cx, cy, R = self._g_geom
        tt = self.tel.t
        # trail: last 1.2 s
        trail = np.linspace(t - 1.2, t, 16)
        gl = np.interp(trail, tt, self.tel.g_lat)
        gn = np.interp(trail, tt, self.tel.g_long)
        for i, (a, b) in enumerate(zip(gl, gn)):
            r = self.S(3 + 3 * i / len(gl))
            px, py = cx + a / self.gmax * R, cy - b / self.gmax * R
            d.ellipse([px - r, py - r, px + r, py + r], fill=tint("accent", 0.15 + 0.6 * i / len(gl)))
        a, b = gl[-1], gn[-1]
        px, py = cx + max(-1.1, min(1.1, a / self.gmax)) * R, cy - max(-1.1, min(1.1, b / self.gmax)) * R
        r = self.S(11)
        d.ellipse([px - r - self.S(3), py - r - self.S(3), px + r + self.S(3), py + r + self.S(3)],
                  fill=(255, 255, 255, 255))
        d.ellipse([px - r, py - r, px + r, py + r], fill=COLORS["accent"])
        self.text(d, p.w - self.S(22), p.ay + self.S(42), f"{math.hypot(a, b):.2f} G", 30, "white", "r")
        y = p.ay + p.h - self.S(18)
        self.text(d, self.S(22), y, f"LAT {abs(a):.2f}", 20, "dim")
        self.text(d, p.w - self.S(22), y, ("ACC " if b >= 0 else "BRK ") + f"{abs(b):.2f}", 20, "dim", "r")

    def _draw_lean(self, d, p, t, st):
        cx, cy, R = self._lean_geom
        lean = float(np.interp(t, self.tel.t, self.tel.lean_deg))
        a = max(-60, min(60, lean))
        box = [cx - R, cy - R, cx + R, cy + R]
        start, end = (270, 270 + a) if a >= 0 else (270 + a, 270)
        if abs(a) > 0.5:
            d.arc(box, start, end, fill=COLORS["accent"], width=self.S(14))
        # marker across the arc at the current angle
        ang = math.radians(-90 + a)
        r1, r2 = R - self.S(22), R + self.S(8)
        d.line([(cx + r1 * math.cos(ang), cy + r1 * math.sin(ang)),
                (cx + r2 * math.cos(ang), cy + r2 * math.sin(ang))],
               fill=COLORS["white"], width=self.S(6))
        side = "R" if lean > 0.5 else "L" if lean < -0.5 else ""
        self.text(d, cx, cy - self.S(40), f"{abs(lean):.0f}°{side}", 72, "white", "m")
        if self.tel.g_long is not None:
            g = float(np.interp(t, self.tel.t, self.tel.g_long))
            self.text(d, p.w - self.S(22), p.ay + self.S(40),
                      ("ACC " if g >= 0 else "BRK ") + f"{abs(g):.2f} G", 24, "dim", "r")
