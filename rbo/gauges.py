"""The lean angle gauge and the throttle & brake gauge, in each theme's gauge
style ("arc", "bar", "tape", "led", "dial").

Mixed into Hud. Both are compact cards: a big number and one simple
indicator, nothing else.
"""
from __future__ import annotations

import math

import numpy as np

# card size (reference px) of the lean gauge in each style
LEAN_SIZE = {"arc": (260, 146), "bar": (260, 112), "tape": (260, 160), "led": (260, 132), "dial": (260, 166)}
ACCEL_SIZE = (200, 196)

THROTTLE_G = 0.06       # above this you're on the gas
BRAKE_G = -0.18         # below this you're braking (between: rolling off / coasting)


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    """Centred moving average over n samples (no lag: the whole ride is known)."""
    if n <= 1:
        return x.astype(float)
    pad = np.pad(x.astype(float), (n // 2, n - 1 - n // 2), mode="edge")
    return np.convolve(pad, np.ones(n) / n, mode="valid")


def accel_signal(tel) -> np.ndarray:
    """How hard you're speeding up (+) or slowing down (-), in G, at each sample.

    The level comes from GPS speed (smooth and honest, but blurred over about a
    second) and the quick changes from the RaceBox's own accelerometer (fast,
    but noisy and offset by the bike pitching): a complementary blend of the
    two. Without an accelerometer column it's GPS alone."""
    t = tel.t
    dt = float(np.median(np.diff(t))) if len(t) > 1 else 0.04
    dt = dt if dt > 0 else 0.04
    n = lambda sec: max(1, int(round(sec / dt)))
    v = _smooth(tel.speed_kmh / 3.6, n(0.3))
    gps = np.gradient(v, t) / 9.81
    gps = np.clip(gps, -3, 3)
    if tel.g_long is not None and np.nanstd(tel.g_long) > 1e-3:
        acc = _smooth(np.nan_to_num(tel.g_long), n(0.25))
        out = _smooth(gps, n(1.0)) + (acc - _smooth(acc, n(1.0)))
    else:
        out = _smooth(gps, n(0.4))
    return np.clip(out, -2.5, 2.5)


class GaugeMixin:
    # ------------------------------------------------------------ lean

    def lean_size(self) -> tuple:
        return LEAN_SIZE.get(self.gauge, LEAN_SIZE["arc"])

    def _lean_now(self, t):
        lean = float(np.interp(t, self.tel.t, self.tel.lean_deg))
        side = "R" if lean > 0.5 else "L" if lean < -0.5 else ""
        return lean, max(-60.0, min(60.0, lean)), side

    def _label(self, d, p, s, y=30, x=18):
        self.text(d, self.S(x), p.iay + self.S(y), s, 14, "dim", track=2)

    def _big_number(self, d, x, y, s, size, side, anchor="m", col="white"):
        """The lean figure with its side letter (L / R) after it, kept centred on x."""
        S = self.S
        w = self.measure(s, size)
        sw = self.measure(side, size * 0.42) + S(4) if side else 0
        x0 = x - (w + sw) / 2 if anchor == "m" else x
        self.text(d, x0, y, s, size, col)
        if side:
            self.text(d, x0 + w + S(4), y, side, size * 0.42, "dim")

    def static_lean(self, img, d, p):
        g, S = self.gauge, self.S
        cx = p.iw / 2
        if g == "bar":
            self._label(d, p, "LEAN")
            x0, x1, y0, y1 = S(18), p.iw - S(18), p.iay + S(88), p.iay + S(98)
            self._lg = (x0, x1, y0, y1)
            d.rectangle([x0, y0, x1, y1], fill=self.tint("white", 0.14))
        elif g == "tape":                       # horizon: fixed wings, a roll scale
            self._label(d, p, "LEAN", y=26)
            cy, R = p.iay + S(108), S(78)
            self._lg = (cx, cy, R)
            col, sc, w = self.C["accent"], self.C["stroke_color"], max(2, S(2.5))
            for pass_c, ex in ((sc, S(2)), (col, 0)):
                d.arc([cx - R, cy - R, cx + R, cy + R], 210, 330, fill=pass_c, width=max(1, S(2)) + ex)
                for a in (-60, -30, 0, 30, 60):
                    ang = math.radians(-90 + a)
                    r2 = R + S(12 if a == 0 else 8)
                    d.line([(cx + R * math.cos(ang), cy + R * math.sin(ang)),
                            (cx + r2 * math.cos(ang), cy + r2 * math.sin(ang))], fill=pass_c, width=w + ex)
                for sx in (-1, 1):
                    d.line([(cx + sx * S(22), cy), (cx + sx * S(58), cy)], fill=pass_c, width=w + ex)
                d.line([(cx - S(12), cy + S(8)), (cx, cy), (cx + S(12), cy + S(8))], fill=pass_c, width=w + ex)
        elif g == "led":
            self._label(d, p, "LEAN", y=28)
            base, n, w, gap = p.iay + S(118), 6, S(12), S(4)
            segs = {}
            for side in (-1, 1):
                row = []
                for i in range(n):
                    f = (i + 1) / n
                    h = S(10) + S(30) * f
                    bx = cx + side * (S(10) + i * (w + gap))
                    rect = (min(bx, bx + side * w), base - h, max(bx, bx + side * w), base)
                    row.append((rect, f))
                    d.rectangle(rect, fill=self.tint(self._seg_colour(f), 0.16))
                segs[side] = row
            self._lg = segs
        elif g == "dial":                       # a small inclinometer
            cy, R = p.iay + S(124), S(110)
            self._lg = (cx, cy, R)
            face = tuple(min(255, v + 12) for v in self.C["bg"][:3]) + (255,)
            d.pieslice([cx - R, cy - R, cx + R, cy + R], 180, 360, fill=self.C["edge"])
            d.pieslice([cx - R + S(4), cy - R + S(4), cx + R - S(4), cy + R - S(4)], 180, 360, fill=self.C["ring"])
            Rf = R - S(8)
            d.pieslice([cx - Rf, cy - Rf, cx + Rf, cy + Rf], 180, 360, fill=face)
            d.rectangle([cx - R, cy, cx + R, cy + S(4)], fill=self.C["edge"])
            for a in range(-60, 61, 15):
                ang = math.radians(-90 + a)
                big = a % 30 == 0
                r1 = Rf - S(8)
                r2 = r1 - S(14 if big else 7)
                d.line([(cx + r1 * math.cos(ang), cy + r1 * math.sin(ang)),
                        (cx + r2 * math.cos(ang), cy + r2 * math.sin(ang))], fill=self.C["text"],
                       width=max(1, S(2.5 if big else 1.5)))
                if big:
                    rl = r2 - S(14)
                    f = self.font(15)
                    self.text(d, cx + rl * math.cos(ang), cy + rl * math.sin(ang) + f.size * 0.36, f"{abs(a)}",
                              15, "text", "m")
            self._label(d, p, "LEAN", y=156)
        else:                                   # "arc"
            self._label(d, p, "LEAN")
            cy, R, wid = p.iay + S(156), S(110), S(12)
            self._lg = (cx, cy, R, wid)
            d.arc([cx - R, cy - R, cx + R, cy + R], 210, 330, fill=self.tint("white", 0.14), width=wid)
            d.line([(cx, cy - R - S(4)), (cx, cy - R - S(11))], fill=self.C["dim"], width=max(1, S(2)))

    def draw_lean(self, img, d, p, t, st):
        g, S = self.gauge, self.S
        lean, a, side = self._lean_now(t)
        num = f"{abs(lean):.0f}°"
        cx = p.iw / 2
        if g == "bar":
            x0, x1, y0, y1 = self._lg
            mid = (x0 + x1) / 2
            xa = mid + (x1 - x0) / 2 * a / 60
            if abs(a) > 0.5:
                d.rectangle([min(mid, xa), y0, max(mid, xa), y1], fill=self.C["accent"])
            d.line([(mid, y0 - S(4)), (mid, y1 + S(4))], fill=self.C["white"], width=max(1, S(2)))
            self._big_number(d, cx, p.iay + S(74), num, 46, side)
        elif g == "tape":
            cx, cy, R = self._lg
            col, sc = self.C["accent"], self.C["stroke_color"]
            ang = math.radians(-a)
            ca, sa = math.cos(ang), math.sin(ang)
            for r0, r1 in ((S(62), S(76)), (-S(76), -S(62))):          # the horizon, tilting
                pts = [(cx + ca * r0, cy + sa * r0), (cx + ca * r1, cy + sa * r1)]
                d.line(pts, fill=sc, width=max(2, S(3)) + S(2))
                d.line(pts, fill=col, width=max(2, S(3)))
            pa = math.radians(-90 + a)
            tip = (cx + (R - S(2)) * math.cos(pa), cy + (R - S(2)) * math.sin(pa))
            sd = (math.cos(pa + math.pi / 2), math.sin(pa + math.pi / 2))
            back = (cx + (R - S(14)) * math.cos(pa), cy + (R - S(14)) * math.sin(pa))
            d.polygon([tip, (back[0] + sd[0] * S(6), back[1] + sd[1] * S(6)),
                       (back[0] - sd[0] * S(6), back[1] - sd[1] * S(6))], fill=col, outline=sc)
            self._big_number(d, cx, cy + S(44), f"{abs(lean):02.0f}°", 28, side, col="accent")
        elif g == "led":
            if side:
                segs = self._lg[1 if lean > 0 else -1]
                lit = math.ceil(abs(a) / 60 * len(segs) - 1e-9)
                for rect, f in segs[:lit]:
                    d.rectangle(rect, fill=self._seg_colour(f))
            self._big_number(d, cx, p.iay + S(64), f"{abs(lean):.0f}", 38, side)
        elif g == "dial":
            cx, cy, R = self._lg
            ang = math.radians(-90 + a)
            L, tail, w = R - S(24), S(14), S(4.5)
            ca, sa = math.cos(ang), math.sin(ang)
            px_, py_ = -sa, ca
            needle = [(cx + ca * L, cy + sa * L), (cx + px_ * w - ca * tail, cy + py_ * w - sa * tail),
                      (cx - px_ * w - ca * tail, cy - py_ * w - sa * tail)]
            d.polygon([(x + S(2), y + S(3)) for x, y in needle], fill=(0, 0, 0, 70))
            d.polygon(needle, fill=self.C["accent"])
            hr = S(11)
            d.ellipse([cx - hr, cy - hr, cx + hr, cy + hr], fill=self.C["ring"], outline=self.C["edge"],
                      width=max(1, S(2)))
            self.readout(d, p.iw - S(52), p.iay + S(150), S(72), S(24), f"{abs(lean):.0f}{side}", 17)
        else:
            cx, cy, R, wid = self._lg
            box = [cx - R, cy - R, cx + R, cy + R]
            if abs(a) > 0.5:
                start, end = (270, 270 + a) if a >= 0 else (270 + a, 270)
                d.arc(box, start, end, fill=self.C["accent"], width=wid)
                ang = math.radians(270 + a)
                ex, ey = cx + (R - wid / 2) * math.cos(ang), cy + (R - wid / 2) * math.sin(ang)
                d.ellipse([ex - wid / 2, ey - wid / 2, ex + wid / 2, ey + wid / 2], fill=self.C["accent"])
            self._big_number(d, cx, cy - S(22), num, 54, side)

    # ------------------------------------------------------------ throttle & brake

    def setup_accel(self) -> None:
        self.accel_g = accel_signal(self.tel)
        top = float(np.percentile(np.abs(self.accel_g), 99.5)) if len(self.accel_g) else 1.0
        self.accel_max = max(0.6, math.ceil(top * 10) / 10)          # full scale, each way

    def _accel_now(self, t):
        g = float(np.interp(t, self.tel.t, self.accel_g))
        state = "THROTTLE" if g > THROTTLE_G else "BRAKE" if g < BRAKE_G else "COAST"
        return g, state

    def static_accel(self, img, d, p):
        gs, S = self.gauge, self.S
        x0, x1, y0, y1 = S(22), S(52), p.iay + S(18), p.iay + p.ih - S(18)
        mid = (y0 + y1) / 2
        self._ag = (x0, x1, y0, y1, mid)
        light = self.light()
        if gs == "led":                         # segments up (gas) and down (brake)
            n, gap = 7, S(3)
            h = (mid - y0 - gap * n) / n
            segs = {1: [], -1: []}
            for side in (1, -1):
                for i in range(n):
                    a = mid - side * (gap / 2 + i * (h + gap))
                    b = a - side * h
                    rect = (x0, min(a, b), x1, max(a, b))
                    segs[side].append(rect)
                    col = self.C["green"] if side > 0 else self.C["red"]
                    d.rectangle(rect, fill=self.tint(col, 0.16))
            self._ag_segs = segs
        elif gs == "tape":                      # HUD: an outlined channel with a ladder
            col, sc = self.C["accent"], self.C["stroke_color"]
            for pass_c, ex in ((sc, S(2)), (col, 0)):
                d.rectangle([x0, y0, x1, y1], outline=pass_c, width=max(1, S(2)) + ex)
                d.line([(x0 - S(6), mid), (x1 + S(6), mid)], fill=pass_c, width=max(1, S(2)) + ex)
        elif gs == "dial":                      # a brass-edged channel on a cream face
            d.rounded_rectangle([x0 - S(4), y0 - S(4), x1 + S(4), y1 + S(4)], radius=S(8), fill=self.C["edge"])
            d.rounded_rectangle([x0 - S(1), y0 - S(1), x1 + S(1), y1 + S(1)], radius=S(6), fill=self.C["ring"])
            d.rounded_rectangle([x0 + S(2), y0 + S(2), x1 - S(2), y1 - S(2)], radius=S(4),
                                fill=tuple(min(255, v + 12) for v in self.C["bg"][:3]) + (255,))
        elif gs == "bar":
            d.rectangle([x0, y0, x1, y1], fill=self.tint("white", 0.12))
        else:
            d.rounded_rectangle([x0, y0, x1, y1], radius=S(6), fill=self.tint("text" if light else "white", 0.12))
        # scale: a tick every 0.5 G each way, the centre longer
        tick = self.C["text"] if gs == "dial" else self.tint("dim", 0.9)
        step = 0.5
        k = 1
        while k * step <= self.accel_max + 1e-6:
            for sgn in (1, -1):
                y = mid - sgn * (mid - y0) * (k * step) / self.accel_max
                d.line([(x1 + S(5), y), (x1 + S(10), y)], fill=tick, width=max(1, S(1.5)))
            k += 1
        if gs != "tape":
            d.line([(x1 + S(5), mid), (x1 + S(14), mid)], fill=tick, width=max(1, S(2)))

    def draw_accel(self, img, d, p, t, st):
        gs, S = self.gauge, self.S
        x0, x1, y0, y1, mid = self._ag
        g, state = self._accel_now(t)
        f = max(-1.0, min(1.0, g / self.accel_max))
        col = self.C["green"] if g > 0 else self.C["red"]
        if state == "COAST":
            col = self.C["dim"]
        ya = mid - (mid - y0) * f
        if gs == "led":
            side = 1 if g > 0 else -1
            segs = self._ag_segs[side]
            lit = math.ceil(abs(f) * len(segs) - 1e-9) if state != "COAST" or abs(f) > 0.04 else 0
            for rect in segs[:lit]:
                d.rectangle(rect, fill=self.C["green"] if side > 0 else self.C["red"])
        elif abs(ya - mid) > 1:
            r = [x0 + S(3), min(mid, ya), x1 - S(3), max(mid, ya)]
            if gs in ("bar", "tape", "dial"):
                d.rectangle(r, fill=col)
            else:
                d.rounded_rectangle(r, radius=S(3), fill=col)
        if gs not in ("led", "tape"):
            d.line([(x0 - S(3), mid), (x1 + S(3), mid)], fill=self.C["white"] if gs != "dial" else self.C["text"],
                   width=max(1, S(2)))
        # the readout: what you're doing, and how hard
        tx = S(70)
        word_col = {"THROTTLE": "green", "BRAKE": "red"}.get(state, "dim")
        self.text(d, tx, p.iay + S(54), state, 14, word_col, track=1.5)
        size = 44 if gs != "led" else 38
        val = f"{abs(g):.2f}"
        w = self.text(d, tx, p.iay + S(118), val, size, "accent" if gs == "tape" else "white")
        self.text(d, tx + w + S(4), p.iay + S(118), "G", 18, "dim")
        self.text(d, tx, p.iay + S(150), "ACCEL / BRAKE" if gs != "led" else "ACC/BRK", 11, "dim", track=1)
