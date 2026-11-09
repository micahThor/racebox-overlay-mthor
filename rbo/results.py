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
# the columns after the class, right-aligned: (key, heading, right edge in reference px)
COLUMNS = {
    "qualifying": [("laps", "LAPS", 830), ("time", "BEST LAP", 990), ("gap", "GAP", 1130)],
    "heat": [("laps", "LAPS", 750), ("best", "BEST LAP", 880), ("time", "TIME", 1010), ("gap", "GAP", 1130)],
}
GRID_X = 690            # right edge of the heat's GRID column (with places gained / lost)
CLASS_X = {"qualifying": 560, "heat": 470}


class ResultsError(Exception):
    pass


@dataclass
class Results:
    kind: str                       # "qualifying" or "heat"
    rows: list = field(default_factory=list)   # {pos, number, name, cls, time}
    me: str = ""                    # your start number, highlighted
    has_grid: bool = False          # a heat whose starting grid Speedhive knows

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


def _get(sid: str, ep: str):
    from .race import API
    req = urllib.request.Request(API.format(sid=sid, ep=ep),
                                 headers={"User-Agent": "racebox-overlay", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _starts(lapchart) -> list:
    """Start numbers in grid order, from a Speedhive lap chart."""
    return [str(x.get("startNumber", "")).strip() for x in ((lapchart or {}).get("startPositions") or [])]


def load_session(source: str, cache: Path | None = None) -> tuple[dict, list]:
    """(classification, grid) for a Speedhive session link/number, or a saved
    JSON file (a whole race-mode download, or just the classification). The
    grid is the start numbers in starting order (empty if Speedhive has none)."""
    from .race import session_id
    p = Path(str(source)).expanduser()
    if p.exists():
        data = json.loads(p.read_text())
        return data.get("classification", data), _starts(data.get("lapchart"))
    sid = session_id(source)
    if not sid:
        raise ResultsError(f"not a Speedhive session link, number or file: {source}")
    if cache and cache.exists():
        try:
            data = json.loads(cache.read_text())
            if str(data.get("session")) == str(sid) and "lapchart" in data:
                return data["classification"], _starts(data["lapchart"])
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    try:
        cls = _get(sid, "classification")
    except Exception as e:  # noqa: BLE001
        raise ResultsError(f"couldn't download the Speedhive results for session {sid}: {e}") from e
    try:
        lap = _get(sid, "lapchart")
        lap = {"startPositions": lap.get("startPositions") or []}
    except Exception:  # noqa: BLE001 - the grid is a nice-to-have
        lap = {"startPositions": []}
    if cache:
        try:
            cache.write_text(json.dumps({"session": int(sid), "classification": cls, "lapchart": lap}))
        except OSError:
            pass
    return cls, _starts(lap)


def load_classification(source: str, cache: Path | None = None) -> dict:
    return load_session(source, cache)[0]


def _gap(v: float) -> str:
    m, sec = divmod(v, 60)
    return f"+{int(m)}:{sec:06.3f}" if m else f"+{sec:.3f}"


STATUS_WORDS = ("DNF", "DNS", "DSQ", "DQ", "NC")


def _status(r: dict) -> str:
    """DNF / DNS / DSQ when Speedhive marks a rider so (field names vary)."""
    for k in ("status", "resultStatus", "state"):
        v = str(r.get(k) or "").strip().upper()
        if v and v not in ("OK", "FINISHED", "CLASSIFIED", "NORMAL", "NONE"):
            for w in STATUS_WORDS:
                if w in v:
                    return w
            return v[:4]
    return ""


def build(kind: str, classification: dict, me: str = "", grid: list | None = None) -> Results:
    """Every rider in the session, in finishing order (riders without a
    position at the end), with all their results: laps, best lap, total time
    and the gap to the leader (to pole, for qualifying)."""
    rows_in = sorted(classification.get("rows") or [],
                     key=lambda r: (r.get("position") is None, r.get("position") or 0))
    if not rows_in:
        raise ResultsError("that Speedhive session has no results")
    lead_laps = max((r.get("numberOfLaps") or 0) for r in rows_in)
    lead = rows_in[0]
    pole = min((v for v in (_secs(r.get("bestTime")) for r in rows_in) if v), default=None)
    lead_total = _secs(lead.get("totalTime"))
    slot = {n: i + 1 for i, n in enumerate(grid or []) if n}
    rows = []
    for r in rows_in:
        laps = r.get("numberOfLaps")
        best, total = _secs(r.get("bestTime")), _secs(r.get("totalTime"))
        st = _status(r)
        if kind == "qualifying":
            t = _fmt(best) if best else (st or "—")
            gap = "" if not best or not pole else ("POLE" if abs(best - pole) < 1e-6 else _gap(best - pole))
        else:                                   # a race: finish time, or laps down
            down = lead_laps - (laps or 0)
            t = st or (_fmt(total) if total else "—")
            if st:
                gap = ""
            elif down > 0 and laps:
                gap = f"+{down} LAP" + ("S" if down > 1 else "")
            elif r is lead:
                gap = "WINNER" if lead_total else ""
            else:
                gap = _gap(total - lead_total) if total and lead_total else ""
        rows.append({"pos": r.get("position"), "number": str(r.get("startNumber", "")).strip(),
                     "name": " ".join(str(r.get("name", "")).split()),
                     "cls": str(r.get("resultClass") or "").strip(),
                     "cpos": r.get("positionInClass"),
                     "laps": "" if laps is None else str(laps),
                     "best": _fmt(best) if best else "—",
                     "time": t, "gap": gap,
                     "grid": slot.get(str(r.get("startNumber", "")).strip())})
    out = Results(kind, rows, str(me or "").strip())
    out.has_grid = kind == "heat" and any(r["grid"] for r in rows)
    return out


def _src(proj: dict, key: str):
    v = proj.get(key)
    return v.strip() if isinstance(v, str) else v


def _order(cls: dict) -> list[str]:
    """Start numbers in finishing order (riders with no position at the end)."""
    rows = sorted(cls.get("rows") or [], key=lambda r: (r.get("position") is None, r.get("position") or 0))
    return [str(r.get("startNumber", "")).strip() for r in rows]


def _load(proj: dict, video_path: str | None, kind: str, key: str):
    cache = Path(video_path).with_name(Path(video_path).name + f".{kind}.json") if video_path else None
    return load_session(_src(proj, key), cache)


def from_project(proj: dict, video_path: str | None = None) -> Results | None:
    """The results to show for this video: the heat's if given (this is the
    main), else qualifying's (this is the heat), else None. A heat's grid is
    the qualifying order when qualifying is given too (Speedhive's own "start
    positions" are the order the field first crossed the line, not the grid)."""
    if _src(proj, "heat_source"):
        cls, grid = _load(proj, video_path, "heat", "heat_source")
        if _src(proj, "qual_source"):
            try:
                grid = _order(_load(proj, video_path, "qualifying", "qual_source")[0])
            except ResultsError:
                pass
        return build("heat", cls, proj.get("race_number") or "", grid)
    if _src(proj, "qual_source"):
        cls, grid = _load(proj, video_path, "qualifying", "qual_source")
        return build("qualifying", cls, proj.get("race_number") or "", grid)
    return None


def grid_for_video(proj: dict, video_path: str | None = None) -> list[str] | None:
    """The starting grid of the race in this video, as start numbers: the heat's
    finishing order (this video is the main), else qualifying's (it's the
    heat), else None (Speedhive's lap chart is used)."""
    for kind, key in (("heat", "heat_source"), ("qualifying", "qual_source")):
        if _src(proj, key):
            return _order(_load(proj, video_path, kind, key)[0])
    return None


class ResultsMixin:
    """Mixed into Hud: draws the results card in the theme."""

    RW = 1160           # reference width of the card

    def results_rect(self, n_rows: int) -> tuple:
        """(x, y, w, h, k) of the card in the output frame: most of the picture,
        centred on it."""
        RH = self._res_rh(n_rows)
        ref_h = 128 + n_rows * RH + 26
        k = min(self.W * 0.9 / self.RW, self.H * 0.88 / ref_h)
        w, h = int(round(self.RW * k)), int(round(ref_h * k))
        return self.vid_x0 + (self.W - w) // 2, self.vid_y0 + (self.H - h) // 2, w, h, k

    def _res_rh(self, n: int) -> int:
        """Row height: tighter for a big field, so everyone fits."""
        return 44 if n <= 16 else 38 if n <= 24 else 32

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
        RW, RH = self.RW, self._res_rh(len(res.rows))
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
        # columns: position, number, rider, class, then the times
        has_cls = any(r["cls"] for r in res.rows)
        cols = COLUMNS.get(res.kind, COLUMNS["heat"])
        c_pos, c_num, c_name = pad + 40, pad + 128, pad + 148
        c_cls = CLASS_X.get(res.kind, 470)
        first_col = min(x - self.measure_ref(res, k) for k, _, x in cols)
        if res.has_grid:
            first_col = min(first_col, GRID_X - 78)
            self.text(d, S(GRID_X), S(122), "GRID", 13, "dim", "r", track=1.5)
        hy = 122
        for x_, s_, a_ in ((c_pos, "POS", "r"), (c_num, "NO.", "r"), (c_name, "RIDER", "l")):
            self.text(d, S(x_), S(hy), s_, 13, "dim", a_, track=1.5)
        for k_, s_, x_ in cols:
            self.text(d, S(x_), S(hy), s_, 13, "dim", "r", track=1.5)
        if has_cls:
            self.text(d, S(c_cls), S(hy), "CLASS", 13, "dim", track=1.5)
        top = 132
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
            room = S((c_cls - 16 if has_cls else first_col - 20) - c_name)
            while size > 12 and self.measure(name, size) > room:
                size -= 1
            self.text(d, S(c_name), base, name, size, ink)
            if has_cls and r["cls"]:
                cc = self.class_colour(r["cls"])
                dr = S(6)
                cy = base - S(7)
                d.ellipse([S(c_cls), cy - dr, S(c_cls) + 2 * dr, cy + dr], fill=cc,
                          outline=ink if me else None, width=max(1, S(1.5)))
                cname = r["cls"].upper() + (f" P{r['cpos']}" if r.get("cpos") and self._multi(res) else "")
                csz = 17
                croom = S(first_col - c_cls - 20 - 16)
                while csz > 10 and self.measure(cname, csz) > croom:
                    csz -= 1
                self.text(d, S(c_cls + 20), base, cname, csz, dim)
            if res.has_grid and r.get("grid"):
                self._grid_cell(d, base, r, ink if me else self.col("white"), me)
            for k_, _, x_ in cols:
                v = r.get(k_, "")
                if not v:
                    continue
                main = k_ == "time"
                sz = 22 if main else 19
                if v in ("POLE", "WINNER") or v.endswith(("LAP", "LAPS")):
                    sz = 15
                self.text(d, S(x_), base, v, sz, ink if main or me else dim, "r")
            if stroke:
                self.theme["stroke"] = stroke

    def _grid_cell(self, d, base, r, ink, me):
        """Where they started, and the places gained (green, up) or lost (red, down)."""
        S = self.S
        self.text(d, S(GRID_X - 38), base, str(r["grid"]), 20, ink, "r")
        if not r.get("pos") or r.get("time") in STATUS_WORDS:
            return
        moved = r["grid"] - r["pos"]
        if not moved:
            return
        up = moved > 0
        col = ink if me else self.C["green"] if up else self.C["red"]
        x0, cy, w = S(GRID_X - 32), base - S(6), S(9)
        tri = ([(x0, cy + w / 2), (x0 + w, cy + w / 2), (x0 + w / 2, cy - w / 2)] if up else
               [(x0, cy - w / 2), (x0 + w, cy - w / 2), (x0 + w / 2, cy + w / 2)])
        d.polygon(tri, fill=col)
        self.text(d, S(GRID_X), base, str(abs(moved)), 14, col, "r")

    def _multi(self, res: Results) -> bool:
        return len({r["cls"] for r in res.rows if r["cls"]}) > 1

    def measure_ref(self, res: Results, key: str) -> float:
        """The widest entry of a column, in reference px."""
        vals = [r.get(key, "") for r in res.rows] + [""]
        return max(self.measure(v, 22 if key == "time" else 19) for v in vals) / self.k
