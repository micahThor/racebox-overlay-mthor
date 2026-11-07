"""Results from an earlier session of the race weekend, shown at the start of
the video.

A race weekend runs qualifying, then heat races, then the main. When the
video has the Speedhive link of an earlier session:

* qualifying results given  -> this video is the heat: show qualifying;
* heat race results given   -> this video is the main: show the heat
  (the heat wins if both are given).

The results card is big (most of the picture), see-through, in the current
theme, and shows for the first RESULTS_SECONDS of the rendered video.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

RESULTS_SECONDS = 10.0
FADE_IN, FADE_OUT = 0.5, 0.7

TITLES = {"qualifying": "QUALIFYING", "heat": "HEAT RACE"}
TIME_HEAD = {"qualifying": "BEST LAP", "heat": "TIME"}


class ResultsError(Exception):
    pass


@dataclass
class Results:
    kind: str                       # "qualifying" or "heat"
    rows: list = field(default_factory=list)   # {pos, number, name, cls, time}
    me: str = ""                    # your start number, highlighted

    @property
    def title(self) -> str:
        return TITLES.get(self.kind, "RESULTS")


def _secs(s) -> float | None:
    s = str(s or "").strip()
    if not s:
        return None
    try:
        v = 0.0
        for part in s.split(":"):
            v = v * 60 + float(part)
        return v
    except ValueError:
        return None


def _fmt(v: float | None) -> str:
    if v is None:
        return "—"
    m, s = divmod(v, 60)
    return f"{int(m)}:{s:06.3f}" if m else f"{s:.3f}"


def load_classification(source: str, cache: Path | None = None) -> dict:
    """The Speedhive classification for a session link/number, or a saved JSON
    file (a whole race-mode download or just the classification)."""
    from .race import session_id
    p = Path(str(source)).expanduser()
    if p.exists():
        data = json.loads(p.read_text())
        return data.get("classification", data)
    sid = session_id(source)
    if not sid:
        raise ResultsError(f"not a Speedhive session link, number or file: {source}")
    if cache and cache.exists():
        try:
            data = json.loads(cache.read_text())
            if str(data.get("session")) == sid:
                return data["classification"]
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    from .race import API
    req = urllib.request.Request(API.format(sid=sid, ep="classification"),
                                 headers={"User-Agent": "racebox-overlay", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            cls = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        raise ResultsError(f"couldn't download the Speedhive results for session {sid}: {e}") from e
    if cache:
        cache.write_text(json.dumps({"session": int(sid), "classification": cls}))
    return cls


def build(kind: str, classification: dict, me: str = "") -> Results:
    rows_in = sorted(classification.get("rows") or [],
                     key=lambda r: (r.get("position") is None, r.get("position") or 0))
    if not rows_in:
        raise ResultsError("that Speedhive session has no results")
    lead_laps = max((r.get("numberOfLaps") or 0) for r in rows_in)
    rows = []
    for r in rows_in:
        if kind == "qualifying":
            t = _fmt(_secs(r.get("bestTime")))
        else:                                   # a race: finish time, or laps down
            down = lead_laps - (r.get("numberOfLaps") or 0)
            if down > 0 and r.get("numberOfLaps"):
                t = f"+{down} LAP" + ("S" if down > 1 else "")
            else:
                t = _fmt(_secs(r.get("totalTime")))
        rows.append({"pos": r.get("position"), "number": str(r.get("startNumber", "")).strip(),
                     "name": " ".join(str(r.get("name", "")).split()),
                     "cls": str(r.get("resultClass") or "").strip(), "time": t})
    return Results(kind, rows, str(me or "").strip())


def from_project(proj: dict, video_path: str | None = None) -> Results | None:
    """The results to show for this video: the heat's if given (this is the
    main), else qualifying's (this is the heat), else None."""
    for kind, key in (("heat", "heat_source"), ("qualifying", "qual_source")):
        src = (proj.get(key) or "").strip() if isinstance(proj.get(key), str) else proj.get(key)
        if src:
            cache = Path(video_path).with_name(Path(video_path).name + f".{kind}.json") if video_path else None
            return build(kind, load_classification(src, cache), proj.get("race_number") or "")
    return None


class ResultsMixin:
    """Mixed into Hud: draws the results card in the theme."""

    RW = 820            # reference width of the card

    def results_rect(self, n_rows: int) -> tuple:
        """(x, y, w, h, k) of the card in the output frame: most of the picture,
        centred on it."""
        RH = self._res_rh()
        ref_h = 128 + n_rows * RH + 26
        k = min(self.W * 0.9 / self.RW, self.H * 0.88 / ref_h)
        w, h = int(round(self.RW * k)), int(round(ref_h * k))
        return self.vid_x0 + (self.W - w) // 2, self.vid_y0 + (self.H - h) // 2, w, h, k

    def _res_rh(self) -> int:
        return 44

    def results_card(self, res: Results) -> Image.Image:
        """The card at output size, see-through, in the theme."""
        from .hud import Panel
        n = len(res.rows)
        x, y, w, h, k = self.results_rect(n)
        ss = 2
        p = Panel("results", "results", x, y, w, h, ay=0, k=k * ss, ss=ss)
        old_k, old_bg = self.k, self.C["bg"]
        bg = old_bg
        light = sum(bg[:3]) > 382
        if bg[3] == 0:                          # card-less themes: a dark glass behind the table
            bg = (8, 10, 14, 0)
        self.C["bg"] = tuple(bg[:3]) + ((225 if light else 150),)
        try:
            self._use(p)
            img = Image.new("RGBA", (p.iw, p.ih), tuple(self.C["bg"][:3]) + (0,))
            d = ImageDraw.Draw(img)
            if not self._has_card() or old_bg[3] == 0:
                d.rounded_rectangle([0, 0, p.iw - 1, p.ih - 1], radius=self.S(16), fill=self.C["bg"])
            self.card(img, d, p)
            self._results_body(img, d, p, res)
        finally:
            self.C["bg"], self.k = old_bg, old_k
        return img.resize((w, h), Image.LANCZOS)

    def _results_body(self, img, d, p, res: Results):
        S = self.S
        RW, RH = self.RW, self._res_rh()
        pad = 30
        light = self.light()
        # header: the session, and the event below it
        self.text(d, S(pad), S(62), res.title, 40, "white", track=1.5)
        tw = self.measure(res.title, 40) + S(1.5) * len(res.title)
        self.text(d, S(pad) + tw + S(14), S(62), "RESULTS", 40, "accent", track=1.5)
        info = getattr(self, "info", None) or {}
        sub = " · ".join(str(info.get(k)) for k in ("event", "track") if info.get(k))
        if sub:
            self.text(d, S(pad), S(90), sub, 17, "dim")
        d.line([(S(pad), S(100)), (S(RW - pad), S(100))], fill=self.C["accent"], width=max(1, S(2.5)))
        # columns
        has_cls = any(r["cls"] for r in res.rows)
        c_pos, c_num, c_name, c_cls, c_time = pad + 40, pad + 128, pad + 148, 548, RW - pad
        hy = 122
        for x_, s_, a_ in ((c_pos, "POS", "r"), (c_num, "NO.", "r"), (c_name, "RIDER", "l"),
                           (c_time, TIME_HEAD.get(res.kind, "TIME"), "r")):
            self.text(d, S(x_), S(hy), s_, 13, "dim", a_, track=1.5)
        if has_cls:
            self.text(d, S(c_cls), S(hy), "CLASS", 13, "dim", track=1.5)
        top = 132
        time_w = max(self.measure(r["time"], 24) for r in res.rows)
        stroke = self.theme.get("stroke")
        for i, r in enumerate(res.rows):
            ry = top + i * RH
            base = S(ry + RH / 2 + 8)
            me = res.me and r["number"] == res.me
            if me:
                d.rounded_rectangle([S(12), S(ry + 3), S(RW - 12), S(ry + RH - 3)], radius=S(8),
                                    fill=self.C["accent"])
            elif i % 2 == 0:
                d.rectangle([S(12), S(ry + 3), S(RW - 12), S(ry + RH - 3)],
                            fill=self.tint("text" if light else "white", 0.06))
            if me and stroke:                   # outlined lettering would blur on the solid highlight
                self.theme["stroke"] = 0
            ink = self._ink_on(self.C["accent"]) if me else self.col("white")
            dim = ink if me else self.col("dim")
            self.text(d, S(c_pos), base, str(r["pos"] or "—"), 22, dim, "r")
            self.text(d, S(c_num), base, r["number"], 26, ink, "r")
            name, size = r["name"].upper(), 22
            room = S((c_cls - 16 if has_cls else c_time - 150) - c_name)
            while size > 13 and self.measure(name, size) > room:
                size -= 1
            self.text(d, S(c_name), base, name, size, ink)
            if has_cls and r["cls"]:
                cc = self.class_colour(r["cls"])
                dr = S(6)
                cy = base - S(7)
                d.ellipse([S(c_cls), cy - dr, S(c_cls) + 2 * dr, cy + dr], fill=cc,
                          outline=ink if me else None, width=max(1, S(1.5)))
                cname, csz = r["cls"].upper(), 17
                croom = S(c_time - c_cls - 20 - 14) - time_w
                while csz > 11 and self.measure(cname, csz) > croom:
                    csz -= 1
                self.text(d, S(c_cls + 20), base, cname, csz, dim)
            self.text(d, S(c_time), base, r["time"], 24, ink, "r")
            if stroke:
                self.theme["stroke"] = stroke
