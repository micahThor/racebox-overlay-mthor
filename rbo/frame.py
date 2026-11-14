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


def _honeycomb(a: int) -> np.ndarray:
    """One repeat of a honeycomb (hexagons of size a): bright where cells meet."""
    tx, ty = max(2, round(a * 1.732)), max(2, round(a * 3))     # lattice spacing; the repeat
    yy, xx = np.mgrid[0:ty, 0:tx].astype(np.float32) + 0.5
    d = []
    for ox, oy in ((0, 0), (tx / 2, ty / 2)):                  # two offset lattices = hexagon centres
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                d.append(np.hypot(xx - (ox + i * tx), yy - (oy + j * ty)))
    d = np.sort(np.stack(d), axis=0)
    edge = (d[1] - d[0]) < max(1.0, a * 0.16)
    return np.where(edge, 4.5, -0.7).astype(np.float32)


def _grass(h: int, w: int, u: float) -> np.ndarray:
    """Thick trackside grass, h x w x 3: tufts of blades fanning out, dark
    underneath and lighter on top, in a tile, with patches of lighter and darker
    green over it so the tile doesn't show."""
    rng = np.random.default_rng(5)
    t = max(64, int(256 * u))                       # the tile
    tile = Image.new("RGB", (t, t), (22, 52, 20))
    d = ImageDraw.Draw(tile)
    layers = [[(20, 48, 18), (26, 60, 22), (30, 66, 26)],           # underneath
              [(36, 80, 30), (44, 94, 34), (50, 102, 40)],
              [(58, 116, 44), (70, 130, 50), (84, 146, 58)]]        # tips catching the light
    tufts = int(t * t / max(30.0, 150 * u * u))
    for layer, greens in enumerate(layers):
        for _ in range(tufts):
            bx, by = rng.uniform(0, t), rng.uniform(0, t)
            for _ in range(rng.integers(8, 18)):
                ln = rng.uniform(14, 30) * u * (0.8 + 0.15 * layer)
                a = math.radians(90 + rng.normal(0, 16))
                bend = rng.normal(0, 0.18)
                col = greens[rng.integers(len(greens))]
                x0b = bx + rng.normal(0, 3 * u)
                mid = (x0b + ln * 0.55 * math.cos(a + bend * 0.5), by - ln * 0.55 * math.sin(a + bend * 0.5))
                tip = (x0b + ln * math.cos(a + bend), by - ln * math.sin(a + bend))
                wb = max(1, round(u * rng.uniform(0.9, 1.5)))      # thin blades, thinner at the tip
                for ox in (-t, 0, t):               # wrap so the tile repeats seamlessly
                    for oy in (-t, 0, t):
                        if -ln <= bx + ox <= t + ln and -ln <= by + oy <= t + ln:
                            d.line([(x0b + ox, by + oy), (mid[0] + ox, mid[1] + oy)], fill=col, width=wb)
                            d.line([(mid[0] + ox, mid[1] + oy), (tip[0] + ox, tip[1] + oy)], fill=col,
                                   width=max(1, wb - 1))
    tile = tile.filter(ImageFilter.GaussianBlur(max(0.3, 0.4 * u)))
    g = np.tile(np.array(tile, np.float32), (h // t + 1, w // t + 1, 1))[:h, :w]
    cell = max(8, int(70 * u))
    lo = rng.uniform(0.82, 1.14, (h // cell + 2, w // cell + 2))
    patch = np.array(Image.fromarray((lo * 100).astype(np.uint8), "L").resize((w, h), Image.BICUBIC),
                     np.float32) / 100
    return np.clip(g * patch[..., None], 0, 255)


def _band(pts, width):
    """A polygon `width` wide along a path (offsets either side of it)."""
    half, left, right = width / 2, [], []
    for k, p in enumerate(pts):
        a = pts[max(0, k - 1)]
        b = pts[min(len(pts) - 1, k + 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / n * half, dx / n * half
        left.append((p[0] + nx, p[1] + ny))
        right.append((p[0] - nx, p[1] - ny))
    return left + right[::-1]


def _kerb(d, pts, width, seg, red, white):
    """A red and white kerb (rumble strip) along an open path, red at both ends."""
    if len(pts) < 2:
        return
    lens = [math.dist(a, b) for a, b in zip(pts, pts[1:])]
    total = sum(lens)
    n = max(1, int(round(total / seg)) | 1)         # an odd count: red at both ends
    seg = total / n
    pos, i, run, red_on = 0.0, 0, [pts[0]], True
    blocks = []
    for a, b, ln in zip(pts, pts[1:], lens):
        while i < n and ln > 0 and pos + ln >= seg * (i + 1) - 1e-9:
            cut = seg * (i + 1) - pos
            p = (a[0] + (b[0] - a[0]) * cut / ln, a[1] + (b[1] - a[1]) * cut / ln)
            run.append(p)
            blocks.append((run, red_on))
            red_on, run, i = not red_on, [p], i + 1
        pos += ln
        run.append(b)
    if len(run) > 1:
        blocks.append((run, red_on))
    for run, is_red in blocks:
        d.polygon(_band(run, width), fill=red if is_red else white)


def _corner_runs(pts, x0, y0, x1, y1, span):
    """The parts of a loop round the picture that are within `span` of a corner
    (along both sides), one run per corner."""
    dense = []
    for a, b in zip(pts, pts[1:]):                  # fill in the long straight sides
        n = max(1, int(math.dist(a, b) / 4))
        dense += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n)]
    near = lambda p: (p[0] < x0 + span or p[0] > x1 - span) and (p[1] < y0 + span or p[1] > y1 - span)
    runs, cur = [], []
    for p in dense:
        if near(p):
            cur.append(p)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        if runs and near(dense[0]):                 # the loop starts mid-corner: join the two halves
            runs[0] = cur + runs[0]
        else:
            runs.append(cur)
    out = []
    for run in runs:                                # drop repeated points (they upset the band's edges)
        clean = [run[0]]
        for p in run[1:]:
            if math.dist(p, clean[-1]) > 0.5:
                clean.append(p)
        out.append(clean)
    return out


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
    pattern = th.get("pattern")
    if pattern is None and shape == "round" and not light and th.get("bg", (0, 0, 0, 0))[3] \
            and th.get("edge", (0, 0, 0, 0))[3] < 100:
        pattern = "weave"
    if pattern == "weave":
        # carbon weave: a fine twill of slightly lighter and darker diagonals
        cell = max(2, U1(7))
        twill = (((xx // cell) + (yy // cell)) % 2) * 2 - 1
        diag = ((xx + yy) // max(1, cell // 2)) % 2
        delta += twill * (2.2 + diag * 1.4)
    elif pattern == "brushed":                     # brushed metal: fine streaks along the frame
        rng = np.random.default_rng(11)
        rows = rng.normal(0, 1, h1).astype(np.float32)
        k = np.ones(max(1, U1(3))) / max(1, U1(3))
        rows = np.convolve(rows, k, mode="same")
        streak = rng.normal(0, 1, (h1, max(2, w1 // max(1, U1(90))) + 1)).astype(np.float32)
        streak = np.repeat(streak, max(1, U1(90)), axis=1)[:, :w1]
        delta += rows[:, None] * 2.6 + streak * 0.9
    elif pattern == "grass":
        pass                                       # coloured, not a shade: added below
    elif pattern == "hex":                         # a faint honeycomb, like a radiator grille
        delta += np.tile(_honeycomb(max(4, U1(8))), (h1 // max(4, U1(8)) + 3, w1 // max(4, U1(8)) + 3))[:h1, :w1]
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
    grass = _grass(h1, w1, u1) * (base / np.array((34, 74, 30), np.float32)) if pattern == "grass" else None
    for c in range(3):
        ch = (grass[..., c] + delta * 0.6) if grass is not None else base[c] + delta
        if grid is not None:
            ch = np.where(grid, ch * 0.88 + acc_c[c] * 0.12, ch)
        if mul is not None:
            ch = ch * mul
        out[..., c] = np.clip(ch, 0, 255)
    out[..., 3] = 255
    fill = Image.fromarray(out, "RGBA")
    del out, delta, mul, grid, grass
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
    elif th.get("trim") == "kerb":                 # trackside: a white edge line, kerbs at the corners
        kw = U(20)
        d.line(op(U(2)), fill=(0, 0, 0, 150), width=U(2), joint="curve")
        d.line(op(U(6)), fill=(236, 236, 230, 255), width=U(4), joint="curve")
        span = min(x1 - x0, y1 - y0) * 0.2
        for run in _corner_runs(op(U(2) + kw / 2), x0, y0, x1, y1, span):
            _kerb(d, run, kw, U(40), (206, 28, 34, 255), (238, 238, 232, 255))
        for run in _corner_runs(op(U(3) + kw), x0, y0, x1, y1, span):
            d.line(run, fill=(16, 30, 14, 140), width=U(2), joint="curve")       # its shadow on the grass
        d.line(_outer(CW, CH, U(10)), fill=blend(edge[:3], min(1.0, edge[3] / 255 * 1.6)), width=U(1.5),
               joint="curve")                      # (only round the outside: nothing across the kerbs)
    else:                                          # carbon and its family, daylight
        for pts in outline(U(10)):
            d.line(pts, fill=blend(edge[:3], min(1.0, edge[3] / 255 * 1.6)), width=U(1.5), joint="curve")
        if th.get("trim") == "pinstripe":          # a thin second stripe outside the accent line
            pin = tuple(th.get("trim_color", (255, 255, 255))[:3])
            d.line(op(U(9.5)), fill=blend(pin, 0.85), width=U(1.5), joint="curve")
        d.line(op(U(5)), fill=tuple(acc[:3]) + (255,), width=U(3), joint="curve")
        d.line(op(U(11)), fill=blend((255, 255, 255) if not light else (0, 0, 0), 0.07), width=U(1), joint="curve")
    if ss > 1:
        img = img.reduce(ss)
    fill.alpha_composite(img)
    return fill
