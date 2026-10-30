"""Theme styles that change shapes, not just colours.

Mixed into Hud. A theme's style keys (see themes.py) pick how cards, pills
and bars are shaped, how the speedometer, lean and G gauges are drawn, what
marks you on the map, how the race tower looks, and a texture over the cards.
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw

GAUGE_START = 135.0
GAUGE_SWEEP = 270.0

CLASS_COLOURS = [(255, 204, 0), (0, 180, 255), (40, 214, 110), (255, 120, 40), (190, 110, 255),
                 (255, 80, 150)]
ARCADE_ROWS = [(255, 236, 50), (0, 236, 255), (70, 255, 110), (255, 140, 40), (190, 120, 255),
               (255, 255, 255)]

SPRITE = [".......HH.....",
          "......HHAA....",
          "....AAAAAA....",
          "..AAAAAAAAAA..",
          ".WWWAAAAAAWWW.",
          "WW.WWAAAAWW.WW",
          "W...W....W...W",
          "WW.WW....WW.WW",
          ".WWW......WWW."]


def _ordinal(n: int) -> str:
    return f"{n}{'TH' if 10 <= n % 100 <= 20 else {1: 'ST', 2: 'ND', 3: 'RD'}.get(n % 10, 'TH')}"


class StyleMixin:
    # ------------------------------------------------------------ basics

    @property
    def shape(self) -> str:
        return self.theme.get("shape", "round")

    @property
    def gauge(self) -> str:
        return self.theme.get("gauge", "arc")

    def light(self) -> bool:
        bg = self.C["bg"]
        return bg[3] > 0 and sum(bg[:3]) > 382

    def _poly(self, rect, kind=None):
        x0, y0, x1, y1 = rect
        h = y1 - y0
        kind = kind or self.shape
        if kind == "slant":
            sk = h * 0.35
            return [(x0 + sk, y0), (x1, y0), (x1 - sk, y1), (x0, y1)]
        if kind == "pixel" and h >= self.S(6):
            u = max(1, self.S(3))
            return [(x0 + u, y0), (x1 - u, y0), (x1 - u, y0 + u), (x1, y0 + u), (x1, y1 - u),
                    (x1 - u, y1 - u), (x1 - u, y1), (x0 + u, y1), (x0 + u, y1 - u), (x0, y1 - u),
                    (x0, y0 + u), (x0 + u, y0 + u)]
        return None

    def box(self, d, rect, fill=None, outline=None, width=1, radius=None, solid=True):
        """A pill, bar or highlight in the theme's shape. On HUD themes, boxes
        taller than a thin bar are drawn as outlines unless solid is forced."""
        x0, y0, x1, y1 = rect
        if x1 <= x0 or y1 <= y0:
            return
        h = y1 - y0
        sh = self.shape
        if sh == "bracket" and fill is not None and h > self.S(12) and not solid:
            outline, fill, width = fill, None, max(width, self.S(2))
        poly = self._poly(rect)
        if poly:
            d.polygon(poly, fill=fill, outline=outline, width=width if outline else 0)
        elif sh in ("bracket", "pixel"):
            d.rectangle(rect, fill=fill, outline=outline, width=width if outline else 0)
        else:
            r = radius if radius is not None else min(h / 2, self.S(self.theme.get("radius", 18)))
            d.rounded_rectangle(rect, radius=r, fill=fill, outline=outline, width=width if outline else 0)

    def pill_solid(self) -> bool:
        return self.shape != "bracket"

    def _gradient_fill(self, img, p, mask: Image.Image, top, bot):
        grad = np.linspace(0, 1, p.ih)[:, None, None]
        arr = (np.array(top, float) * (1 - grad) + np.array(bot, float) * grad)
        arr = np.repeat(arr, p.iw, axis=1).astype(np.uint8)
        img.paste(Image.fromarray(arr, "RGBA"), (0, p.iay), mask)

    def brackets(self, d, x0, y0, x1, y1, col, L=None, w=None):
        L, w = L or self.S(24), w or max(2, self.S(3))
        sc = self.C["stroke_color"] if self.theme.get("stroke") else None
        for pass_col, extra in ((sc, max(1, self.S(1.5))), (col, 0)):
            if pass_col is None:
                continue
            ww = w + 2 * extra
            for (ax, ay, dx, dy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
                d.line([(ax, ay + dy * L), (ax, ay), (ax + dx * L, ay)], fill=pass_col, width=ww, joint="curve")

    # ------------------------------------------------------------ cards

    def card_styled(self, img, d, p) -> bool:
        """Draw the card in the theme's shape. False = use the standard card."""
        sh, S, bg = self.shape, self.S, self.C["bg"]
        x0, y0, x1, y1 = 0, p.iay, p.iw - 1, p.iay + p.ih - 1
        if sh == "slant":
            c = S(26)
            pts = [(x0, y0), (x1, y0), (x1, y1 - c), (x1 - c, y1), (x0, y1)]
            mask = Image.new("L", (p.iw, p.ih), 0)
            ImageDraw.Draw(mask).polygon([(x, y - p.iay) for x, y in pts], fill=255)
            top = tuple(min(255, v + 18) for v in bg[:3]) + (bg[3],)
            self._gradient_fill(img, p, mask, top, tuple(max(0, v - 6) for v in bg[:3]) + (bg[3],))
            d.rectangle([x0, y0, x0 + S(6), y1], fill=self.C["accent"])
            d.line([(S(6), y0 + S(1)), (x1, y0 + S(1))], fill=self.tint((255, 255, 255, 255), 0.18),
                   width=max(1, S(1.5)))
            return True
        if sh == "bracket":
            self.brackets(d, x0 + S(3), y0 + S(3), x1 - S(3), y1 - S(3), self.C["accent"])
            return True
        if sh == "pixel":
            pts = self._poly((x0, y0, x1, y1))
            u = S(4)
            d.polygon(pts, fill=bg)
            d.polygon(pts, outline=self.C["accent"], width=u)
            d.rectangle([x0 + S(9), y0 + S(9), x1 - S(9), y1 - S(9)], outline=self.tint("dim", 0.35),
                        width=max(1, S(1.5)))
            return True
        if sh == "bezel":
            r = S(self.theme.get("radius", 22))
            d.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=self.C["edge"])
            d.rounded_rectangle([x0 + S(3), y0 + S(3), x1 - S(3), y1 - S(3)], radius=r - S(3),
                                fill=self.C["ring"])
            mask = Image.new("L", (p.iw, p.ih), 0)
            ImageDraw.Draw(mask).rounded_rectangle([S(6), S(6), p.iw - 1 - S(6), p.ih - 1 - S(6)],
                                                   radius=r - S(6), fill=255)
            top = tuple(min(255, v + 10) for v in bg[:3]) + (255,)
            bot = tuple(max(0, v - 14) for v in bg[:3]) + (255,)
            self._gradient_fill(img, p, mask, top, bot)
            d.rounded_rectangle([S(6), y0 + S(6), p.iw - 1 - S(6), y1 - S(6)], radius=r - S(6),
                                outline=self.tint("edge", 0.6), width=max(1, S(1)))
            return True
        return False

    def apply_texture(self, img, p):
        tex = self.theme.get("texture")
        if not tex or p.name == "ghost":
            return
        box = (0, p.iay, p.iw, p.iay + p.ih)
        arr = np.array(img.crop(box)).astype(np.float32)
        solid = arr[..., 3] > 0
        if tex == "scanlines":
            period, thick = max(2, self.S(4)), max(1, self.S(1.6))
            rows = (np.arange(arr.shape[0]) % period) < thick
            m = solid & rows[:, None]
            arr[..., :3][m] *= 0.62
        elif tex == "grain":
            rng = np.random.default_rng(1)
            n = rng.normal(0, 1, arr.shape[:2]).astype(np.float32) * 7
            for c in range(3):
                arr[..., c] = np.where(solid, arr[..., c] + n, arr[..., c])
        img.paste(Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA"), box[:2])

    # ------------------------------------------------------------ map marker

    def draw_marker(self, img, d, p, px, py, heading, facing=True):
        """You on the map. heading: radians on screen (0 = right, y down);
        facing: whether side-on icons face right."""
        kind = self.theme.get("marker", "bike")
        if kind == "badge":
            self.stamp(img, self.glow(self.C["accent"], 15), px, py, p)
            num = self.race.me.number if getattr(self, "race", None) is not None else None
            if num is None:
                self.stamp(img, self._bike_icon(facing), px, py, p)
                return
            R, ring = self.S(15), self.S(3)
            d.ellipse([px - R - ring, py - R - ring, px + R + ring, py + R + ring], fill=self.C["ring"])
            d.ellipse([px - R, py - R, px + R, py + R], fill=self.C["accent"])
            f = self.font(15 if len(num) < 3 else 12)
            self.text(d, px, py + f.size * 0.36, num, 15 if len(num) < 3 else 12, (255, 255, 255, 255), "m")
            return
        if kind == "target":
            col, sc = self.C["accent"], self.C["stroke_color"]
            r, w = self.S(13), max(2, self.S(3))
            dia = [(px, py - r), (px + r, py), (px, py + r), (px - r, py)]
            if heading is not None:
                L = self.S(30)
                hx, hy = px + L * math.cos(heading), py + L * math.sin(heading)
                d.line([(px, py), (hx, hy)], fill=sc, width=w + self.S(2))
                d.line([(px, py), (hx, hy)], fill=col, width=w)
            d.polygon(dia, outline=sc, width=w + self.S(2))
            d.polygon(dia, outline=col, width=w)
            q = self.S(3)
            d.rectangle([px - q, py - q, px + q, py + q], fill=col)
            return
        if kind == "sprite":
            self.stamp(img, self.glow(self.C["accent"], 16), px, py, p)
            key = ("sprite", facing, round(self.k, 4))
            if key not in self._sprites:
                u = max(1, self.S(2.6))
                wq, hq = len(SPRITE[0]), len(SPRITE)
                im = Image.new("RGBA", (wq * u + 2 * u, hq * u + 2 * u), (0, 0, 0, 0))
                dd = ImageDraw.Draw(im)
                cols = {"A": self.C["accent"], "W": (255, 255, 255, 255), "H": (255, 255, 255, 255)}
                for yy, row in enumerate(SPRITE):          # dark outline first
                    for xx, ch in enumerate(row if facing else row[::-1]):
                        if ch != ".":
                            dd.rectangle([xx * u, yy * u, xx * u + 3 * u - 1, yy * u + 3 * u - 1],
                                         fill=(10, 0, 24, 255))
                for yy, row in enumerate(SPRITE):
                    for xx, ch in enumerate(row if facing else row[::-1]):
                        if ch != ".":
                            dd.rectangle([(xx + 1) * u, (yy + 1) * u, (xx + 2) * u - 1, (yy + 2) * u - 1],
                                         fill=cols[ch])
                self._sprites[key] = im
            self.stamp(img, self._sprites[key], px, py, p)
            return
        # bike / enamel: the bike badge (enamel themes colour its ring with "ring")
        self.stamp(img, self.glow(self.C["accent"], 15), px, py, p)
        self.stamp(img, self._bike_icon(facing), px, py, p)

    # ------------------------------------------------------------ shared bits

    def speed_now(self, t):
        return float(np.interp(t, self.tel.t, self.tel.speed_kmh)) * self.speed_factor

    def lap_top_speed(self, t):
        k = self.timing.lap_index_at(t)
        if k is None:
            return None
        lap = self.timing.laps[k]
        m = (self.tel.t >= lap.t_start) & (self.tel.t <= t)
        return float(self.tel.speed_kmh[m].max()) * self.speed_factor if m.any() else None

    def _red_from(self):
        vmax = self.speed_max
        return vmax * 6 / 7 if vmax >= 35 else vmax * 0.85

    def _seg_colour(self, f):
        return self.C["green"] if f < 0.6 else self.C["yellow"] if f < 0.85 else self.C["red"]

    def readout(self, d, cx, cy, w, h, label, size, col="white", align="m"):
        """A boxed number (HUD readouts, odometer windows)."""
        S = self.S
        x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
        if self.gauge == "dial":
            d.rounded_rectangle([x0, y0, x1, y1], radius=S(4), fill=(24, 20, 16, 255),
                                outline=self.C["edge"], width=max(1, S(2)))
            col = (246, 240, 226, 255)
        else:
            d.rectangle([x0, y0, x1, y1], fill=(0, 22, 8, 170), outline=self.C["accent"], width=max(1, S(2)))
        f = self.font(size)
        tx = cx if align == "m" else x1 - S(8)
        self.text(d, tx, cy + f.size * 0.36, label, size, col, "m" if align == "m" else "r")

    # ------------------------------------------------------------ speed: static

    def static_speed_styled(self, img, d, p) -> bool:
        g, S = self.gauge, self.S
        vmax, red_from = self.speed_max, self._red_from()
        unit = "MPH" if self.units == "mph" else "KM/H"
        major = 10 if vmax <= 100 else 20
        if g == "bar":
            self.text(d, S(24), p.iay + S(40), "SPEED", 15, "dim", track=2)
            x0, x1, y0, y1 = S(24), p.iw - S(24), p.iay + S(212), p.iay + S(232)
            self._bar_geom = (x0, x1, y0, y1)
            d.polygon(self._poly((x0, y0, x1, y1), "slant"), fill=self.tint("white", 0.12))
            xr = x0 + (x1 - x0) * red_from / vmax
            d.polygon(self._poly((xr, y0, x1, y1), "slant"), fill=self.tint("red", 0.4))
            v = 0
            while v <= vmax + 1e-6:
                x = x0 + (x1 - x0) * v / vmax
                d.line([(x, y1 + S(4)), (x, y1 + S(11))], fill=self.C["dim"], width=max(1, S(2)))
                if v % (major * 2) == 0 or v == vmax:
                    self.text(d, x, y1 + S(30), f"{v:.0f}", 15, "red" if v >= red_from else "dim", "m")
                v += major
            return True
        if g == "tape":
            self.text(d, S(24), p.iay + S(38), "SPD", 16, "dim", track=2)
            self.text(d, S(160), p.iay + S(38), unit, 16, "dim", track=2)
            return True
        if g == "led":
            self.text(d, S(24), p.iay + S(40), "SPEED", 14, "dim")
            n, x0, x1, base = 14, S(26), p.iw - S(26), p.iay + S(258)
            gap = S(5)
            w = (x1 - x0 - gap * (n - 1)) / n
            self._led = []
            for i in range(n):
                f = (i + 1) / n
                h = S(18) + (S(96) - S(18)) * f
                bx = x0 + i * (w + gap)
                rect = (bx, base - h, bx + w, base)
                self._led.append((rect, f))
                d.rectangle(rect, fill=self.tint(self._seg_colour(f), 0.16))
            self.text(d, p.iw - S(26), p.iay + S(40), unit, 14, "dim", "r")
            return True
        if g == "dial":
            cx, cy, R = p.iw / 2, p.iay + S(158), S(132)
            self._dial = (cx, cy, R)
            face = tuple(min(255, v + 12) for v in self.C["bg"][:3]) + (255,)
            d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=self.C["edge"])
            d.ellipse([cx - R + S(4), cy - R + S(4), cx + R - S(4), cy + R - S(4)], fill=self.C["ring"])
            Rf = R - S(8)
            d.ellipse([cx - Rf, cy - Rf, cx + Rf, cy + Rf], fill=face)
            box = [cx - Rf + S(10), cy - Rf + S(10), cx + Rf - S(10), cy + Rf - S(10)]
            d.arc(box, GAUGE_START + GAUGE_SWEEP * red_from / vmax, GAUGE_START + GAUGE_SWEEP,
                  fill=self.C["red"], width=S(6))
            minor = major / 5 if vmax <= 100 else major / 4
            v = 0.0
            while v <= vmax + 1e-6:
                a = math.radians(GAUGE_START + GAUGE_SWEEP * v / vmax)
                is_major = abs(v / major - round(v / major)) < 1e-6
                is_mid = abs(v / (major / 2) - round(v / (major / 2))) < 1e-6
                r1 = Rf - S(17)
                r2 = r1 - S(15 if is_major else 9 if is_mid else 5)
                d.line([(cx + r1 * math.cos(a), cy + r1 * math.sin(a)),
                        (cx + r2 * math.cos(a), cy + r2 * math.sin(a))],
                       fill=self.C["text"], width=max(1, S(3 if is_major else 1.5)))
                if is_major:
                    rl = r2 - S(17)
                    f = self.font(19)
                    self.text(d, cx + rl * math.cos(a), cy + rl * math.sin(a) + f.size * 0.36,
                              f"{v:.0f}", 19, "red" if v >= red_from else "white", "m")
                v += minor
            self.text(d, cx, cy - S(36), "M.P.H." if self.units == "mph" else "KM/H", 13, "dim", "m", track=2)
            return True
        return False

    # ------------------------------------------------------------ speed: per frame

    def draw_speed_styled(self, img, d, p, t, st) -> bool:
        g, S = self.gauge, self.S
        if g not in ("bar", "tape", "led", "dial"):
            return False
        v = self.speed_now(t)
        frac = min(max(v / self.speed_max, 0.0), 1.0)
        label = f"{v:.0f}"
        if g == "bar":
            x0, x1, y0, y1 = self._bar_geom
            w = self.text(d, S(22), p.iay + S(176), label, 112, "white")
            self.text(d, S(30) + w, p.iay + S(176), "MPH" if self.units == "mph" else "KM/H", 22, "dim")
            top = self.lap_top_speed(t)
            if top is not None:
                tw = self.text(d, p.iw - S(24), p.iay + S(40), f"{top:.0f}", 22, "white", "r")
                self.text(d, p.iw - S(30) - tw, p.iay + S(40), "TOP", 14, "dim", "r", track=1.5)
            if frac > 0.003:
                xf = x0 + (x1 - x0) * frac
                d.polygon(self._poly((x0, y0, xf, y1), "slant"),
                          fill=self.C["red"] if v >= self._red_from() else self.C["text"])
        elif g == "tape":
            cx0, cx1 = S(24), S(124)
            cy, half = p.iay + S(158), S(104)
            ppu = S(4.2)
            lo, hi = v - half / ppu, v + half / ppu
            sc = self.C["stroke_color"]
            tick = 5
            u = math.floor(lo / tick) * tick
            while u <= hi:
                if u >= 0:
                    y = cy - (u - v) * ppu
                    if abs(y - cy) > S(26) and abs(y - cy) < half:
                        major = u % 10 == 0
                        d.line([(cx1 - S(16 if major else 8), y), (cx1, y)], fill=self.C["accent"],
                               width=max(1, S(2.5 if major else 1.5)))
                        if major:
                            f = self.font(18)
                            self.text(d, cx1 - S(22), y + f.size * 0.36, f"{u:.0f}", 18, "dim", "r")
                u += tick
            d.line([(cx1 + S(2), cy - half), (cx1 + S(2), cy + half)], fill=self.C["accent"], width=max(1, S(2)))
            # readout box with a notch pointing at the scale
            bx0, bx1, by0, by1 = cx0, cx1 + S(14), cy - S(24), cy + S(24)
            pts = [(bx0, by0), (cx1, by0), (bx1, cy), (cx1, by1), (bx0, by1)]
            d.polygon(pts, fill=(0, 22, 8, 180), outline=sc, width=max(1, S(5)))
            d.polygon(pts, outline=self.C["accent"], width=max(1, S(2.5)))
            f = self.font(36)
            self.text(d, cx1 - S(8), cy + f.size * 0.36, label, 36, "accent", "r")
            top = self.lap_top_speed(t)
            self.text(d, S(160), cy - S(6), "MAX", 14, "dim", track=2)
            self.text(d, S(160), cy + S(30), f"{top:.0f}" if top is not None else "---", 34, "white")
            if self.tel.g_long is not None:
                gl = float(np.interp(t, self.tel.t, self.tel.g_long))
                self.text(d, S(160), cy + S(76), f"{'ACC' if gl >= 0 else 'BRK'} {abs(gl):.1f}G", 18, "dim")
        elif g == "led":
            lit = math.ceil(frac * len(self._led) - 1e-9)
            for i, (rect, f) in enumerate(self._led[:lit]):
                d.rectangle(rect, fill=self._seg_colour(f))
            self.text(d, p.iw / 2, p.iay + S(138), label, 64, "white", "m")
        elif g == "dial":
            cx, cy, R = self._dial
            self.readout(d, cx, cy + S(58), S(78), S(34), label, 24)
            a = math.radians(GAUGE_START + GAUGE_SWEEP * frac)
            L, tail, w = R - S(26), S(24), S(5)
            ca, sa = math.cos(a), math.sin(a)
            px_, py_ = -sa, ca
            needle = [(cx + ca * L, cy + sa * L), (cx + px_ * w - ca * tail, cy + py_ * w - sa * tail),
                      (cx - px_ * w - ca * tail, cy - py_ * w - sa * tail)]
            d.polygon([(x + S(2), y + S(3)) for x, y in needle], fill=(0, 0, 0, 70))
            d.polygon(needle, fill=self.C["accent"])
            hr = S(13)
            d.ellipse([cx - hr, cy - hr, cx + hr, cy + hr], fill=self.C["ring"], outline=self.C["edge"],
                      width=max(1, S(2)))
            d.ellipse([cx - S(4), cy - S(4), cx + S(4), cy + S(4)], fill=self.C["edge"])
        self.speed_footer(d, p, t)
        return True

    def speed_footer(self, d, p, t):
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

    # ------------------------------------------------------------ lean

    def static_lean_styled(self, img, d, p) -> bool:
        g, S = self.gauge, self.S
        if g == "bar":
            self.text(d, S(24), p.iay + S(36), "LEAN", 15, "dim", track=2)
            x0, x1, y0, y1 = S(30), p.iw - S(30), p.iay + S(176), p.iay + S(196)
            self._lbar = (x0, x1, y0, y1)
            d.rectangle([x0, y0, x1, y1], fill=self.tint("white", 0.12))
            cx = (x0 + x1) / 2
            for a in range(-60, 61, 15):
                x = cx + (x1 - x0) / 2 * a / 60
                d.line([(x, y1 + S(4)), (x, y1 + S(10 if a % 30 == 0 else 6))], fill=self.C["dim"],
                       width=max(1, S(2)))
                if a % 30 == 0:
                    self.text(d, x, y1 + S(30), f"{abs(a)}", 14, "dim", "m")
            self.text(d, x0, y1 + S(52), "L", 14, "dim")
            self.text(d, x1, y1 + S(52), "R", 14, "dim", "r")
            for m in self.lean_max:
                x = cx + (x1 - x0) / 2 * max(-60, min(60, m)) / 60
                d.polygon([(x, y0 - S(3)), (x - S(6), y0 - S(13)), (x + S(6), y0 - S(13))],
                          fill=self.tint("accent", 0.8))
            return True
        if g == "tape":                 # horizon: fixed bike symbol and a roll scale
            self.text(d, S(22), p.iay + S(36), "LEAN", 15, "dim", track=2)
            cx, cy = p.iw / 2, p.iay + S(150)
            self._hz = (cx, cy)
            col, sc, w = self.C["accent"], self.C["stroke_color"], max(2, S(3))
            R = S(112)
            for pass_c, ex in ((sc, S(2)), (col, 0)):
                for a in range(-60, 61, 15):
                    ang = math.radians(-90 + a)
                    r2 = R + S(14 if a % 30 == 0 else 8)
                    d.line([(cx + R * math.cos(ang), cy + R * math.sin(ang)),
                            (cx + r2 * math.cos(ang), cy + r2 * math.sin(ang))], fill=pass_c, width=w + ex)
                d.arc([cx - R, cy - R, cx + R, cy + R], 210, 330, fill=pass_c, width=max(1, S(2)) + ex)
                for sx in (-1, 1):            # fixed wings
                    d.line([(cx + sx * S(30), cy), (cx + sx * S(78), cy), (cx + sx * S(78), cy + S(10))],
                           fill=pass_c, width=w + ex)
                d.line([(cx - S(16), cy + S(10)), (cx, cy), (cx + S(16), cy + S(10))], fill=pass_c, width=w + ex)
            return True
        if g == "led":
            self.text(d, S(22), p.iay + S(36), "LEAN", 14, "dim")
            cx, base, n = p.iw / 2, p.iay + S(232), 8
            w, gap = S(15), S(4)
            self._lleds = {}
            for side in (-1, 1):
                segs = []
                for i in range(n):
                    f = (i + 1) / n
                    h = S(14) + S(64) * f
                    bx = cx + side * (S(14) + i * (w + gap))
                    rect = (min(bx, bx + side * w), base - h, max(bx, bx + side * w), base)
                    segs.append((rect, f))
                    d.rectangle(rect, fill=self.tint(self._seg_colour(f), 0.16))
                self._lleds[side] = segs
            self.text(d, S(22), base + S(22), "L", 14, "dim")
            self.text(d, p.iw - S(22), base + S(22), "R", 14, "dim", "r")
            return True
        if g == "dial":                 # semicircle inclinometer
            cx, cy, R = p.iw / 2, p.iay + S(222), S(168)
            self._ldial = (cx, cy, R)
            face = tuple(min(255, v + 12) for v in self.C["bg"][:3]) + (255,)
            d.pieslice([cx - R, cy - R, cx + R, cy + R], 180, 360, fill=self.C["edge"])
            d.pieslice([cx - R + S(4), cy - R + S(4), cx + R - S(4), cy + R - S(4)], 180, 360, fill=self.C["ring"])
            Rf = R - S(8)
            d.pieslice([cx - Rf, cy - Rf, cx + Rf, cy + Rf], 180, 360, fill=face)
            d.rectangle([cx - R, cy, cx + R, cy + S(4)], fill=self.C["edge"])
            for a in range(-60, 61, 5):
                ang = math.radians(-90 + a)
                major = a % 15 == 0
                r1 = Rf - S(12)
                r2 = r1 - S(16 if a % 30 == 0 else 10 if major else 5)
                d.line([(cx + r1 * math.cos(ang), cy + r1 * math.sin(ang)),
                        (cx + r2 * math.cos(ang), cy + r2 * math.sin(ang))], fill=self.C["text"],
                       width=max(1, S(3 if a % 30 == 0 else 1.5)))
                if a % 30 == 0:
                    rl = r2 - S(16)
                    f = self.font(17)
                    self.text(d, cx + rl * math.cos(ang), cy + rl * math.sin(ang) + f.size * 0.36, f"{abs(a)}",
                              17, "white", "m")
            for m in self.lean_max:
                ang = math.radians(-90 + max(-60, min(60, m)))
                r = Rf - S(4)
                tip = (cx + (r - S(10)) * math.cos(ang), cy + (r - S(10)) * math.sin(ang))
                sd = (math.cos(ang + math.pi / 2), math.sin(ang + math.pi / 2))
                back = (cx + r * math.cos(ang), cy + r * math.sin(ang))
                d.polygon([tip, (back[0] + sd[0] * S(6), back[1] + sd[1] * S(6)),
                           (back[0] - sd[0] * S(6), back[1] - sd[1] * S(6))], fill=self.C["ring"],
                          outline=self.C["edge"])
            self.text(d, S(24), p.iay + S(36), "LEAN", 14, "dim", track=2)
            return True
        return False

    def draw_lean_styled(self, img, d, p, t, st) -> bool:
        g, S = self.gauge, self.S
        if g not in ("bar", "tape", "led", "dial"):
            return False
        lean = float(np.interp(t, self.tel.t, self.tel.lean_deg))
        a = max(-60, min(60, lean))
        side = "R" if lean > 0.5 else "L" if lean < -0.5 else ""
        if g == "bar":
            x0, x1, y0, y1 = self._lbar
            cx = (x0 + x1) / 2
            xa = cx + (x1 - x0) / 2 * a / 60
            if abs(a) > 0.5:
                d.rectangle([min(cx, xa), y0, max(cx, xa), y1], fill=self.C["accent"])
            d.line([(cx, y0 - S(4)), (cx, y1 + S(4))], fill=self.C["white"], width=max(1, S(2)))
            w = self.text(d, cx, p.iay + S(150), f"{abs(lean):.0f}°", 76, "white", "m")
            if side:
                self.text(d, cx + w / 2 + S(6), p.iay + S(150), side, 26, "dim")
        elif g == "tape":
            cx, cy = self._hz
            col, sc = self.C["accent"], self.C["stroke_color"]
            ang = math.radians(-a)
            ca, sa = math.cos(ang), math.sin(ang)
            for r0, r1 in ((S(92), S(124)), (-S(124), -S(92))):
                pts = [(cx + ca * r0, cy + sa * r0), (cx + ca * r1, cy + sa * r1)]
                d.line(pts, fill=sc, width=max(2, S(3)) + S(2))
                d.line(pts, fill=col, width=max(2, S(3)))
            R = S(112)
            pa = math.radians(-90 + a)
            tip = (cx + (R - S(2)) * math.cos(pa), cy + (R - S(2)) * math.sin(pa))
            sd = (math.cos(pa + math.pi / 2), math.sin(pa + math.pi / 2))
            back = (cx + (R - S(16)) * math.cos(pa), cy + (R - S(16)) * math.sin(pa))
            tri = [tip, (back[0] + sd[0] * S(7), back[1] + sd[1] * S(7)),
                   (back[0] - sd[0] * S(7), back[1] - sd[1] * S(7))]
            d.polygon(tri, fill=col, outline=sc)
            self.readout(d, cx, p.iay + S(222), S(120), S(40), f"{abs(lean):02.0f}° {side or ' '}", 26, "accent")
        elif g == "led":
            if side:
                segs = self._lleds[1 if lean > 0 else -1]
                lit = math.ceil(abs(a) / 60 * len(segs) - 1e-9)
                for rect, f in segs[:lit]:
                    d.rectangle(rect, fill=self._seg_colour(f))
            self.text(d, p.iw / 2, p.iay + S(112), f"{abs(lean):.0f}{side}", 48, "white", "m")
        elif g == "dial":
            cx, cy, R = self._ldial
            self.readout(d, cx - S(118), cy - S(20), S(70), S(28), f"{abs(lean):.0f}{side}", 18)
            ang = math.radians(-90 + a)
            L, tail, w = R - S(30), S(18), S(5)
            ca, sa = math.cos(ang), math.sin(ang)
            px_, py_ = -sa, ca
            needle = [(cx + ca * L, cy + sa * L), (cx + px_ * w - ca * tail, cy + py_ * w - sa * tail),
                      (cx - px_ * w - ca * tail, cy - py_ * w - sa * tail)]
            d.polygon([(x + S(2), y + S(3)) for x, y in needle], fill=(0, 0, 0, 70))
            d.polygon(needle, fill=self.C["accent"])
            hr = S(13)
            d.ellipse([cx - hr, cy - hr, cx + hr, cy + hr], fill=self.C["ring"], outline=self.C["edge"],
                      width=max(1, S(2)))
        if self.tel.g_long is not None:
            gg = float(np.interp(t, self.tel.t, self.tel.g_long))
            w = self.text(d, p.iw - S(22), p.iay + S(36), f"{abs(gg):.2f} G", 20, "white", "r")
            self.text(d, p.iw - S(22) - w - S(6), p.iay + S(36), "ACC" if gg >= 0 else "BRK", 13, "dim", "r",
                      track=1.5)
        return True

    # ------------------------------------------------------------ ghost frame

    def ghost_frame(self, d, p, accent):
        sh, S = self.shape, self.S
        x0, y0, x1, y1 = 0, p.iay, p.iw - 1, p.iay + p.ih - 1
        fill = self.C["bg"] if self._has_card() else (0, 0, 0, 150)
        if sh == "slant":
            d.rectangle([x0, y0, x1, y1], fill=fill)
            d.rectangle([x0, y0, x0 + S(6), y1], fill=accent)
            d.rectangle([x0, y0, x1, y1], outline=accent, width=max(2, S(2)))
        elif sh == "bracket":
            self.brackets(d, x0 + S(2), y0 + S(2), x1 - S(2), y1 - S(2), accent, L=S(34))
        elif sh == "pixel":
            pts = self._poly((x0, y0, x1, y1))
            d.polygon(pts, fill=fill)
            d.polygon(pts, outline=accent, width=S(4))
        elif sh == "bezel":
            r = S(14)
            d.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=self.C["edge"])
            d.rounded_rectangle([x0 + S(3), y0 + S(3), x1 - S(3), y1 - S(3)], radius=r - S(3), fill=self.C["ring"])
            d.rounded_rectangle([x0 + S(5), y0 + S(5), x1 - S(5), y1 - S(5)], radius=r - S(5), fill=fill)
        else:
            d.rounded_rectangle([x0, y0, x1, y1], radius=S(12), fill=fill, outline=accent, width=max(2, S(3)))

    def ghost_radius(self) -> int:
        return max(2, int(8 * self.base_k)) if self.shape in ("round", "bezel") else 0

    # ------------------------------------------------------------ race tower

    def tower_header(self, d, p, b, style):
        S, pad = self.S, self.S(18)
        right, y = p.iw - pad, p.iay + S(40)
        hdr_col = "white"
        if style == "broadcast":
            d.polygon(self._poly((S(6), p.iay + S(10), p.iw - S(4), p.iay + S(52)), "slant"), fill=self.C["accent"])
            hdr_col = (255, 255, 255, 255)
            pad = S(26)
        if not b["started"]:
            txt, col = ("GRID", hdr_col if style == "broadcast" else "dim")
        elif b["finished"]:
            txt, col = ("FINISHED", hdr_col)
        elif b["final_lap"]:
            txt, col = ("FINAL LAP", hdr_col if style == "broadcast" else "yellow")
        else:
            txt, col = None, None
        if style == "pitboard" and txt == "FINAL LAP":
            col = "red"
        if txt:
            self.text(d, pad, y, txt, 19, col, track=2)
        else:
            x = pad + self.text(d, pad, y, "LAP", 15, hdr_col if style == "broadcast" else "dim", track=2) + S(8)
            x += self.text(d, x, y, str(b["lap"]), 24, hdr_col)
            self.text(d, x + S(2), y, f"/{b['laps']}", 16, hdr_col if style == "broadcast" else "dim")
        if b["finished"]:
            fx = right - S(118 if style != "arcade" else 150)
            d.rectangle([fx, p.iay + S(19), fx + S(22), p.iay + S(41)], fill=(255, 255, 255, 255))
            q = S(5.5)
            for i in range(4):
                for j in range(4):
                    if (i + j) % 2:
                        d.rectangle([fx + i * q, p.iay + S(19) + j * q, fx + (i + 1) * q, p.iay + S(19) + (j + 1) * q],
                                    fill=(20, 20, 20, 255))
        if b["pos"]:
            if style == "arcade":
                self.text(d, right, y, _ordinal(b["pos"]), 22, "accent", "r")
            else:
                w = self.text(d, right, y, f"/{b['of']}", 16, hdr_col if style == "broadcast" else "dim", "r")
                self.text(d, right - w - S(2), y, f"P{b['pos']:02d}" if style == "hud" else f"P{b['pos']}", 26,
                          hdr_col if style == "broadcast" else "accent", "r")
        if self.race_cls:
            self.text(d, pad, p.iay + S(57), self.race_cls.upper(), 10, "dim", track=1.5)

    def tower_row(self, d, p, i, r, ry, rh, t, style):
        S, pad = self.S, self.S(18)
        right = p.iw - pad
        base = ry + rh / 2 + S(6)
        me = r["me"]
        x0, x1 = S(8), p.iw - S(8)
        white = (255, 255, 255, 255)
        fg, dim = self.C["text"], self.C["dim"]
        pos_txt, name = str(i + 1), r["name"].upper()[:11]
        num_x, name_x = pad + S(46), pad + S(94)
        if style == "broadcast":
            if me:
                d.polygon(self._poly((x0, ry + S(2), x1, ry + rh - S(2)), "slant"), fill=self.C["accent"])
                fg, dim = white, (255, 255, 255, 215)
            elif i % 2 == 0:
                d.rectangle([x0, ry + S(2), x1, ry + rh - S(2)], fill=self.tint("white", 0.05))
            bx0, bx1 = S(12), S(42)
            d.rectangle([bx0, ry + S(4), bx1, ry + rh - S(4)], fill=white)
            f = self.font(16)
            self.text(d, (bx0 + bx1) / 2, base, pos_txt, 16, self.C["accent"] if me else (14, 18, 34, 255), "m")
            cls = r["cls"] or ""
            cc = CLASS_COLOURS[sum(map(ord, cls)) % len(CLASS_COLOURS)] + (255,)
            d.rectangle([S(48), ry + S(5), S(52), ry + rh - S(5)], fill=cc)
            num_x, name_x = S(58), S(114)
        elif style == "hud":
            pos_txt = f"{i + 1:02d}"
            if me:
                self.brackets(d, x0, ry + S(1), x1, ry + rh - S(1), self.C["accent"], L=S(8), w=max(1, S(2)))
                fg = self.C["accent"]
            self.text(d, pad + S(22), base, pos_txt, 16, fg, "r")
        elif style == "arcade":
            pos_txt = _ordinal(i + 1)
            col = ARCADE_ROWS[i % len(ARCADE_ROWS)] + (255,)
            fg, dim = col, col
            name = name[:8]
            if me and (r["moved"] == 0 or int(t * 4) % 2 == 0):      # blinks after a pass
                d.polygon(self._poly((x0, ry + S(2), x1, ry + rh - S(2)), "pixel"), fill=self.C["accent"])
                fg = dim = white
            self.text(d, pad + S(40), base, pos_txt, 13, fg, "r")
            num_x, name_x = pad + S(50), pad + S(102)
        elif style == "pitboard":
            if me:
                d.rounded_rectangle([x0, ry + S(2), x1, ry + rh - S(2)], radius=S(4), fill=self.tint("yellow", 0.55))
            bx0, bx1 = S(12), S(44)
            d.rectangle([bx0, ry + S(3), bx1, ry + rh - S(3)], fill=(28, 24, 20, 255))
            self.text(d, (bx0 + bx1) / 2, base, pos_txt, 16, (246, 240, 226, 255), "m")
        else:
            if me:
                self.box(d, (x0, ry + S(2), x1, ry + rh - S(2)), fill=self.tint("accent", 0.85), radius=S(7))
                fg, dim = white, (255, 255, 255, 210)
            elif i % 2 == 0:
                self.box(d, (x0, ry + S(2), x1, ry + rh - S(2)), fill=self.tint("white", 0.05), radius=S(7))
        if style == "default":
            self.text(d, pad + S(22), base, pos_txt, 17, fg, "r")
        if r["moved"]:
            up = r["moved"] > 0
            col = self.C["green"] if up else self.C["red"]
            ax = {"broadcast": S(104), "arcade": pad + S(44)}.get(style, pad + S(34))
            if style == "arcade":
                ax = name_x - S(10)
            cx, cy, a = ax, ry + rh / 2, S(5)
            tri = [(cx - a, cy + a * 0.6), (cx + a, cy + a * 0.6), (cx, cy - a * 0.8)] if up else \
                  [(cx - a, cy - a * 0.6), (cx + a, cy - a * 0.6), (cx, cy + a * 0.8)]
            d.polygon(tri, fill=white if (me and style in ("default", "broadcast")) else col)
        if style != "arcade":
            self.text(d, num_x, base, r["number"], 15, dim)
        self.text(d, name_x, base, name, 17 if style != "arcade" else 13, fg, track=0.5)
        gsize = 15 if style != "arcade" else 11
        if r["lapped"]:
            self.text(d, right, base, "+1 LAP" if style != "arcade" else "+1L", gsize - 1, dim, "r")
        elif r["gap"] is not None and i > 0:
            self.text(d, right, base, f"+{r['gap']:.1f}", gsize, dim, "r")
