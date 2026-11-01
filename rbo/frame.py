"""The frame around the video: the left, right and bottom strips drawn as one
piece in the theme's style.

Whatever size each strip is, they share one fill, one texture and one outline
that runs round the outside and hugs the picture, so the three read as a single
border. The picture's lower corners are shaped to match the theme (rounded,
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


def _opening(x0, x1, H, e, corner, r, top=-4.0):
    """The picture's outline grown outwards by e: down the left side, round the
    lower corners, along the bottom and up the right side (open at the top)."""
    pts = [(x0 - e, top)]
    if corner == "round" and r > 0:
        R = r + e
        n = max(8, int(R / 3))
        for i in range(n + 1):                     # lower left: 180° -> 90°
            a = math.pi - (math.pi / 2) * i / n
            pts.append((x0 + r + R * math.cos(a), H - r + R * math.sin(a)))
        for i in range(n + 1):                     # lower right: 90° -> 0°
            a = math.pi / 2 - (math.pi / 2) * i / n
            pts.append((x1 - r + R * math.cos(a), H - r + R * math.sin(a)))
    elif corner == "cut" and r > 0:
        t = e * math.tan(math.pi / 8)
        pts += [(x0 - e, H - r + t), (x0 + r - t, H + e), (x1 - r + t, H + e), (x1 + e, H - r + t)]
    elif corner == "step" and r > 0:
        s = r / 2
        pts += [(x0 - e, H + e - 2 * s), (x0 - e + s, H + e - 2 * s), (x0 - e + s, H + e - s),
                (x0 - e + 2 * s, H + e - s), (x0 - e + 2 * s, H + e),
                (x1 + e - 2 * s, H + e), (x1 + e - 2 * s, H + e - s), (x1 + e - s, H + e - s),
                (x1 + e - s, H + e - 2 * s), (x1 + e, H + e - 2 * s)]
    else:
        pts += [(x0 - e, H + e), (x1 + e, H + e)]
    pts.append((x1 + e, top))
    return pts


def _outline(x0, x1, H, CW, CH, e, corner, r):
    """One closed line e in from every edge of the frame: round the outside and
    back along the picture."""
    inner = _opening(x0, x1, H, e, corner, r, top=e)
    return [(x0 - e, e), (e, e), (e, CH - e), (CW - e, CH - e), (CW - e, e)] + inner[::-1] + [(x0 - e, e)]


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
    x0, x1, H = hud.vid_x0 * ss, (hud.vid_x0 + hud.W) * ss, hud.H * ss
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
    ImageDraw.Draw(mask).polygon(_opening(x0, x1, H, 0, corner, r), fill=0)
    if ss > 1:
        mask = mask.reduce(ss)
    fill.putalpha(mask)
    d = ImageDraw.Draw(img)

    acc, edge, ring, text = C["accent"], C["edge"], C["ring"], C["text"]
    blend = lambda col, a: tuple(int(base[i] * (1 - a) + col[i] * a) for i in range(3)) + (255,)
    op = lambda e: _opening(x0, x1, H, e, corner, r)
    outline = lambda e: _outline(x0, x1, H, CW, CH, e, corner, r)

    if shape == "slant":                           # TV broadcast
        d.line(outline(U(9)), fill=blend((255, 255, 255), 0.10), width=U(1.5), joint="curve")
        d.line(op(U(4)), fill=blend((255, 255, 255), 0.55), width=U(2), joint="curve")
        y = H + U(10)                              # the red band along the bottom of the picture
        d.polygon([(x0 + r, y - U(4)), (x1 - r, y - U(4)), (x1 - r - U(8), y + U(5)), (x0 + r + U(8), y + U(5))],
                  fill=acc)
        d.rectangle([0, CH - U(7), CW, CH], fill=acc)
    elif shape == "bracket":                       # fighter HUD
        dim = blend(acc, 0.35)
        d.line(outline(U(10)), fill=dim, width=U(1.5))
        d.line(op(U(6)), fill=acc, width=U(2))
        L, w, e = U(64), U(4), U(16)               # corner brackets round the picture
        for (cx, cy, dx, dy) in ((x0 - e, H + e, 1, -1), (x1 + e, H + e, -1, -1)):
            d.line([(cx, cy + dy * L), (cx, cy), (cx + dx * L, cy)], fill=acc, width=w, joint="curve")
        for (cx, dx) in ((x0 - e, 1), (x1 + e, -1)):
            d.line([(cx, e + L), (cx, e), (cx + dx * L, e)], fill=acc, width=w, joint="curve")
        step = U(30)                               # a ruler under the picture
        for i, x in enumerate(range(int(x0 + U(40)), int(x1 - U(40)), step)):
            d.line([(x, H + U(14)), (x, H + U(14) + (U(14) if i % 5 == 0 else U(7)))], fill=dim, width=U(1.5))
    elif shape == "pixel":                         # retro arcade
        d.line(outline(U(8)), fill=blend(C["dim"], 0.6), width=U(3))
        d.line(op(U(7)), fill=acc, width=U(6))
        d.line(op(U(16)), fill=blend(C["dim"], 0.8), width=U(2))
    elif shape == "bezel":                         # vintage gauges: gold double rule
        gold = tuple(ring[:3]) + (255,)
        d.line(outline(U(12)), fill=gold, width=U(2), joint="curve")
        d.line(outline(U(17)), fill=blend(gold[:3], 0.45), width=U(1), joint="curve")
        d.line(op(U(6)), fill=gold, width=U(4), joint="curve")
        d.line(op(U(13)), fill=blend((0, 0, 0), 0.5), width=U(1.5), joint="curve")
    elif not th.get("bg", (0, 0, 0, 0))[3] and not edge[3]:     # minimal: just a fine line
        d.line(op(U(6)), fill=blend((255, 255, 255), 0.35), width=U(1.5), joint="curve")
    elif edge[3] >= 100:                           # neon: glowing rims
        _glow_line(img, op(U(6)), acc, U(3), U(8))
        _glow_line(img, outline(U(10)), acc, U(1.5), U(6))
        d = ImageDraw.Draw(img)
        d.line(outline(U(10)), fill=blend(acc, 0.55), width=U(1.5), joint="curve")
        d.line(op(U(6)), fill=acc, width=U(3), joint="curve")
    else:                                          # carbon, daylight
        d.line(outline(U(10)), fill=blend(edge[:3], min(1.0, edge[3] / 255 * 1.6)), width=U(1.5), joint="curve")
        d.line(op(U(5)), fill=tuple(acc[:3]) + (255,), width=U(3), joint="curve")
        d.line(op(U(11)), fill=blend((255, 255, 255) if not light else (0, 0, 0), 0.07), width=U(1), joint="curve")
    if ss > 1:
        img = img.reduce(ss)
    fill.alpha_composite(img)
    return fill
