"""The frame around the video: the left, right and bottom strips drawn as one
piece in the theme's style.

Whatever size each strip is, they share one fill, one texture and one outline
that runs round the outside and hugs the picture, so the three read as a single
border. The picture's corners are shaped to match the theme (rounded,
cut, stepped or square), and each theme adds its own trim along the opening.
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# per theme style ("shape" key): the opening's corner, the fill and the trim
FRAME = {
    "round":   {"corner": "round"},
    "slant":   {"corner": "cut"},
    "bracket": {"corner": "square"},
    "pixel":   {"corner": "step"},
    "bezel":   {"corner": "round"},
}


def frame_colour(theme: dict) -> tuple:
    """The frame's base colour: the theme's own `frame_bg`, or its card colour
    made solid and a shade darker."""
    if theme.get("frame_bg"):
        return tuple(int(v) for v in theme["frame_bg"][:3])
    bg = theme["bg"]
    if bg[3] == 0:                                 # card-less themes
        return (12, 14, 18)
    return tuple(int(v * 0.8) for v in bg[:3])


def _corner(kind, r, e):
    """One corner of the opening as offsets from the picture's corner, for the
    lower-left one (frame to the left and below, y down), from its left side
    round to its bottom edge."""
    if kind == "round" and r > 0:
        R, n = r + e, max(8, int((r + e) / 3))
        return [(r + R * math.cos(math.pi - (math.pi / 2) * i / n), -r + R * math.sin(math.pi - (math.pi / 2) * i / n))
                for i in range(n + 1)]
    if kind == "cut" and r > 0:
        t = e * math.tan(math.pi / 8)
        return [(-e, -r + t), (r - t, e)]
    if kind == "step" and r > 0:
        s = r / 2
        return [(-e, e - 2 * s), (-e + s, e - 2 * s), (-e + s, e - s), (-e + 2 * s, e - s), (-e + 2 * s, e)]
    return [(-e, e)]


def _opening(x0, y0, x1, y1, e, kind, r):
    """The picture's outline grown outwards by e, all four corners shaped the
    theme's way, as a closed loop (clockwise from the top-left corner)."""
    base = _corner(kind, r, e)
    pts = []
    for cx, cy, sx, sy, flip in ((x0, y0, -1, -1, False), (x1, y0, 1, -1, True),
                                 (x1, y1, 1, 1, False), (x0, y1, -1, 1, True)):
        c = [(cx - sx * dx, cy + sy * dy) for dx, dy in base]
        pts += c[::-1] if flip else c
    return pts + pts[:2]          # (round past the start so the line closes cleanly)


def _outer(CW, CH, e):
    return [(e, e), (CW - e, e), (CW - e, CH - e), (e, CH - e), (e, e), (CW - e, e)]


def _glow_line(img, pts, col, width, blur):
    layer = Image.new("RGBA", img.size, tuple(col[:3]) + (0,))
    ImageDraw.Draw(layer).line(pts, fill=tuple(col[:3]) + (200,), width=int(width * 2.5), joint="curve")
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur)))


def draw_frame(hud) -> Image.Image:
    """The frame at output size: opaque over the strips, clear over the picture."""
    th, C = hud.theme, hud.C
    shape = th.get("shape", "round")
    style = FRAME.get(shape, FRAME["round"])
    # the fill and texture are made at output size; the lines and the opening
    # are drawn at 2x and scaled down so curves and diagonals come out smooth
    w1, h1 = hud.canvas_w, hud.canvas_h
    ss = 2 if w1 * h1 <= 10_000_000 else 1
    u1 = min(hud.W, hud.H) / 1080
    u = u1 * ss
    U = lambda v: max(1, int(round(v * u)))
    U1 = lambda v: max(1, int(round(v * u1)))
    CW, CH = w1 * ss, h1 * ss
    x0, x1 = hud.vid_x0 * ss, (hud.vid_x0 + hud.W) * ss
    y0, y1 = hud.vid_y0 * ss, (hud.vid_y0 + hud.H) * ss
    base = np.array(frame_colour(th), np.float32)
    light = base.sum() > 382

    # the fill: one gentle top-to-bottom shade over the whole frame, plus texture
    yy, xx = np.ogrid[0:h1, 0:w1]
    yy, xx = yy.astype(np.int32), xx.astype(np.int32)
    g = (yy / max(h1 - 1, 1)).astype(np.float32)
    delta = np.broadcast_to((1 - g) * (6 if light else 10) - g * (6 if light else 8), (h1, w1)).copy()
    mul, grid = None, None
    tex = th.get("texture")
    if shape == "round" and not light and th.get("bg", (0, 0, 0, 0))[3] and th.get("edge", (0, 0, 0, 0))[3] < 100:
        # carbon weave: a fine twill of slightly lighter and darker diagonals
        cell = max(2, U1(7))
        twill = (((xx // cell) + (yy // cell)) % 2) * 2 - 1
        diag = ((xx + yy) // max(1, cell // 2)) % 2
        delta += twill * (2.2 + diag * 1.4)
    elif shape == "slant":                         # broadcast: faint diagonal pinstripes
        delta += (((xx - yy) % U1(18)) < U1(2)) * 5.0
    elif shape == "bracket":                       # HUD: a faint grid
        step = U1(40)
        grid = ((xx - hud.vid_x0) % step < U1(1.4)) | ((yy % step) < U1(1.4))
    if tex == "scanlines":
        mul = np.where((yy % max(2, U1(4))) < max(1, U1(1.6)), 0.62, 1.0).astype(np.float32)
    elif tex == "grain":
        delta += np.random.default_rng(7).normal(0, 9, (h1, w1)).astype(np.float32)
    out = np.empty((h1, w1, 4), np.uint8)
    acc_c = C["accent"]
    for c in range(3):
        ch = base[c] + delta
        if grid is not None:
            ch = np.where(grid, ch * 0.88 + acc_c[c] * 0.12, ch)
        if mul is not None:
            ch = ch * mul
        out[..., c] = np.clip(ch, 0, 255)
    out[..., 3] = 255
    fill = Image.fromarray(out, "RGBA")
    del out, delta, mul, grid
    img = Image.new("RGBA", (CW, CH), tuple(int(v) for v in base) + (0,))   # the lines, at 2x

    corner = style["corner"]
    r = {"round": U(max(th.get("radius", 18), 6) * 1.25), "cut": U(30), "step": U(12), "square": 0}[corner]
    # the picture shows through the opening
    mask = Image.new("L", (CW, CH), 255)
    ImageDraw.Draw(mask).polygon(_opening(x0, y0, x1, y1, 0, corner, r), fill=0)
    if ss > 1:
        mask = mask.reduce(ss)
    fill.putalpha(mask)
    d = ImageDraw.Draw(img)

    acc, edge, ring, text = C["accent"], C["edge"], C["ring"], C["text"]
    blend = lambda col, a: tuple(int(base[i] * (1 - a) + col[i] * a) for i in range(3)) + (255,)
    op = lambda e: _opening(x0, y0, x1, y1, e, corner, r)
    outline = lambda e: (_outer(CW, CH, e), op(e))     # round the outside, and round the picture

    if shape == "slant":                           # TV broadcast
        for pts in outline(U(9)):
            d.line(pts, fill=blend((255, 255, 255), 0.10), width=U(1.5), joint="curve")
        d.line(op(U(4)), fill=blend((255, 255, 255), 0.55), width=U(2), joint="curve")
        y = y1 + U(10)                             # the red band along the bottom of the picture
        d.polygon([(x0 + r, y - U(4)), (x1 - r, y - U(4)), (x1 - r - U(8), y + U(5)), (x0 + r + U(8), y + U(5))],
                  fill=acc)
        d.rectangle([0, CH - U(7), CW, CH], fill=acc)
        d.rectangle([0, 0, CW, U(7)], fill=acc)
    elif shape == "bracket":                       # fighter HUD
        dim = blend(acc, 0.35)
        for pts in outline(U(10)):
            d.line(pts, fill=dim, width=U(1.5))
        d.line(op(U(6)), fill=acc, width=U(2))
        L, w, e = U(64), U(4), U(16)               # corner brackets round the picture
        for (cx, cy, dx, dy) in ((x0 - e, y0 - e, 1, 1), (x1 + e, y0 - e, -1, 1),
                                 (x1 + e, y1 + e, -1, -1), (x0 - e, y1 + e, 1, -1)):
            d.line([(cx, cy + dy * L), (cx, cy), (cx + dx * L, cy)], fill=acc, width=w, joint="curve")
        step = U(30)                               # a ruler under the picture
        for i, x in enumerate(range(int(x0 + U(40)), int(x1 - U(40)), step)):
            d.line([(x, y1 + U(14)), (x, y1 + U(14) + (U(14) if i % 5 == 0 else U(7)))], fill=dim, width=U(1.5))
    elif shape == "pixel":                         # retro arcade
        for pts in outline(U(8)):
            d.line(pts, fill=blend(C["dim"], 0.6), width=U(3))
        d.line(op(U(7)), fill=acc, width=U(6))
        d.line(op(U(16)), fill=blend(C["dim"], 0.8), width=U(2))
    elif shape == "bezel":                         # vintage gauges: gold double rule
        gold = tuple(ring[:3]) + (255,)
        for pts in outline(U(12)):
            d.line(pts, fill=gold, width=U(2), joint="curve")
        for pts in outline(U(17)):
            d.line(pts, fill=blend(gold[:3], 0.45), width=U(1), joint="curve")
        d.line(op(U(6)), fill=gold, width=U(4), joint="curve")
        d.line(op(U(13)), fill=blend((0, 0, 0), 0.5), width=U(1.5), joint="curve")
    elif not th.get("bg", (0, 0, 0, 0))[3] and not edge[3]:     # minimal: just a fine line
        d.line(op(U(6)), fill=blend((255, 255, 255), 0.35), width=U(1.5), joint="curve")
    elif edge[3] >= 100:                           # neon: glowing rims
        _glow_line(img, op(U(6)), acc, U(3), U(8))
        for pts in outline(U(10)):
            _glow_line(img, pts, acc, U(1.5), U(6))
        d = ImageDraw.Draw(img)
        for pts in outline(U(10)):
            d.line(pts, fill=blend(acc, 0.55), width=U(1.5), joint="curve")
        d.line(op(U(6)), fill=acc, width=U(3), joint="curve")
    else:                                          # carbon, daylight
        for pts in outline(U(10)):
            d.line(pts, fill=blend(edge[:3], min(1.0, edge[3] / 255 * 1.6)), width=U(1.5), joint="curve")
        d.line(op(U(5)), fill=tuple(acc[:3]) + (255,), width=U(3), joint="curve")
        d.line(op(U(11)), fill=blend((255, 255, 255) if not light else (0, 0, 0), 0.07), width=U(1), joint="curve")
    if ss > 1:
        img = img.reduce(ss)
    fill.alpha_composite(img)
    return fill
