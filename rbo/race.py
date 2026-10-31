"""Race mode: the running order of a real race, from MYLAPS Speedhive timing.

Speedhive records the moment every rider crosses the timing line. From that:

* positions at each line crossing are exact;
* between crossings, each rider's progress round the lap is estimated from
  their lap time and the shape of *your* lap (where it's fast and slow), and
  your own progress comes from your RaceBox, so a pass shows up where it
  happened rather than at the next crossing. A change of position has to
  hold for a moment before it counts, so estimates near a tie don't flicker;
* your Speedhive crossings and your RaceBox laps are the same events, which
  lines the whole field up with your data (and video) automatically.

Data comes from Speedhive's public results API for a session link such as
https://speedhive.mylaps.com/sessions/12935135, and is cached next to the
video so it only has to be downloaded once.
"""
from __future__ import annotations

import json
import math
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .timing import Timing

API = "https://api2.mylaps.com/sessions/{sid}/{ep}"
HOLD = 1.5          # seconds a new order must hold before it counts
STEP = 0.1          # seconds between race-state samples
SLIDE = 0.6         # seconds a row takes to slide to its new place


class RaceError(Exception):
    pass


# ---------------------------------------------------------------- loading

def session_id(source: str) -> str | None:
    m = re.search(r"sessions?/(\d+)", str(source)) or re.fullmatch(r"\s*(\d{5,})\s*", str(source))
    return m.group(1) if m else None


def fetch(sid: str) -> dict:
    out = {"session": int(sid)}
    for ep in ("classification", "alllaptimes", "lapchart"):
        req = urllib.request.Request(API.format(sid=sid, ep=ep),
                                     headers={"User-Agent": "racebox-overlay", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                out[ep] = json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            raise RaceError(f"couldn't download Speedhive {ep} for session {sid}: {e}") from e
    return out


def load(source: str, cache: Path | None = None) -> dict:
    """A Speedhive session link or number, or a saved JSON file."""
    p = Path(str(source)).expanduser()
    if p.exists():
        if p.suffix.lower() == ".csv":
            raise RaceError("that's the results CSV, which only has final positions. Race mode needs "
                            "the lap-by-lap timing: give the Speedhive session link instead "
                            "(e.g. https://speedhive.mylaps.com/sessions/12935135)")
        return json.loads(p.read_text())
    sid = session_id(source)
    if not sid:
        raise RaceError(f"not a Speedhive session link, number or file: {source}")
    if cache and cache.exists():
        data = json.loads(cache.read_text())
        if str(data.get("session")) == sid:
            return data
    data = fetch(sid)
    if cache:
        cache.write_text(json.dumps(data))
    return data


def _laptime(s) -> float:
    s = str(s or "").strip()
    if not s:
        return float("nan")
    parts = [float(x) for x in s.split(":")]
    v = 0.0
    for x in parts:
        v = v * 60 + x
    return v


def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip()


# ---------------------------------------------------------------- model

@dataclass
class Rider:
    number: str
    name: str
    cls: str
    finish_pos: int
    class_pos: int | None
    raw: np.ndarray                  # crossing times, Speedhive clock (s)
    lap_times: list[float]
    lead_laps: list[int]
    grid: int | None = None
    cross: np.ndarray = field(default=None)   # crossing times on the RaceBox clock

    @property
    def last_name(self) -> str:
        return self.name.split(" ")[-1] if self.name else f"#{self.number}"


@dataclass
class Event:
    t: float
    kind: str        # start, cross, gain, lose, finish, chequered, lapped, fastest
    data: dict


class Race:
    def __init__(self, data: dict, timing: Timing, number: str, offset: float | None = None):
        self.tm, self.tel = timing, timing.tel
        rows = {int(r["position"]): r for r in data["classification"]["rows"]}
        self.riders: list[Rider] = []
        for c in data["alllaptimes"]:
            r = rows.get(int(c["position"]))
            if r is None or not c.get("laps"):
                continue
            laps = sorted(c["laps"], key=lambda l: l["lap"])
            self.riders.append(Rider(
                number=str(r.get("startNumber", "")).strip(), name=_clean(r.get("name")),
                cls=_clean(r.get("resultClass")), finish_pos=int(r["position"]),
                class_pos=r.get("positionInClass"),
                raw=np.array([l["timeOfDay"] / 1000.0 for l in laps]),
                lap_times=[_laptime(l.get("lapTime")) for l in laps],
                lead_laps=[int(l.get("leadLap") or l["lap"]) for l in laps]))
        if not self.riders:
            raise RaceError("no lap times in this Speedhive session")
        grid = data.get("lapchart", {}).get("positionRows") or []
        for i, row in enumerate(grid):
            if row and isinstance(row[0], dict):
                rd = self.rider(str(row[0].get("startNumber", "")))
                if rd:
                    rd.grid = i + 1
        self.me = self.rider(number)
        if self.me is None:
            raise RaceError(f"no rider number {number} in this race; numbers: "
                            + ", ".join(r.number for r in sorted(self.riders, key=lambda r: r.finish_pos)))
        self.total_laps = max(len(r.raw) for r in self.riders)
        self.offset, self.align_error, self.aligned_laps = (offset, 0.0, 0) if offset is not None \
            else self._align()
        for r in self.riders:
            r.cross = r.raw + self.offset
        self.start = self._race_start()
        self._shape = self._lap_shape()
        self._timeline()

    def rider(self, number: str) -> Rider | None:
        n = str(number).strip().lower()
        return next((r for r in self.riders if r.number.lower() == n), None)

    # ------------------------------------------------------------ aligning clocks

    def _align(self):
        """Match my Speedhive crossings to my RaceBox lap boundaries."""
        laps = self.tm.laps
        if not laps:
            raise RaceError("no laps found in the RaceBox data to line the race up with")
        X = sorted({round(t, 3) for l in laps for t in (l.t_start, l.t_end)})
        Y = list(self.me.raw)
        best = None
        for s in range(-len(Y) + 1, len(X)):
            pairs = [(i, i - s) for i in range(len(X)) if 0 <= i - s < len(Y)]
            if len(pairs) < 2:
                continue
            d = np.array([X[i] - Y[j] for i, j in pairs])
            med = float(np.median(d))
            good = np.abs(d - med) < 0.35
            score = (int(good.sum()), -float(np.std(d[good])) if good.sum() > 1 else 0)
            if best is None or score > best[0]:
                best = (score, med, float(np.max(np.abs(d[good] - med))) if good.any() else 9.9)
        if best is None or best[0][0] < 2:
            raise RaceError("couldn't match your Speedhive laps to your RaceBox laps. Is the "
                            "rider number right, and is this the RaceBox session from that race?")
        return best[1], best[2], best[0][0]

    def _race_start(self) -> float:
        # green flag: each rider's first crossing minus their first lap time
        st = [r.cross[0] - r.lap_times[0] for r in self.riders
              if r.lead_laps and r.lead_laps[0] == 1 and np.isfinite(r.lap_times[0])]
        return float(min(st)) if st else float(min(r.cross[0] for r in self.riders) - 60)

    def _lap_shape(self):
        """Fraction of the lap covered vs fraction of the lap time, from my best lap."""
        if not self.tm.laps:
            return np.array([0.0, 1.0]), np.array([0.0, 1.0])
        k = min(range(len(self.tm.laps)), key=lambda i: self.tm.laps[i].time)
        el, d = self.tm._lap_profiles[k]
        return el / el[-1], d / max(d[-1], 1e-6)

    # ------------------------------------------------------------ progress and order

    def progress(self, r: Rider, t: float) -> float:
        """Laps completed plus the fraction of the current lap, at time t."""
        c = r.cross
        n = int(np.searchsorted(c, t, side="right"))
        if n >= len(c):
            return float(len(c))
        if n > 0:
            prev = c[n - 1]
        else:      # first lap: from when this rider left the line (late or pit-lane starts too)
            prev = c[0] - r.lap_times[0] if np.isfinite(r.lap_times[0]) else self.start
            prev = max(prev, self.start)
            if t < prev:
                return 0.0
        nxt = c[n]
        if r is self.me and self.tel.t[0] <= prev and nxt <= self.tel.t[-1]:
            d0, d1, d = np.interp([prev, nxt, t], self.tel.t, self.tm.dist)   # my real progress
            frac = (d - d0) / max(d1 - d0, 1e-6)
        else:
            tau = (t - prev) / max(nxt - prev, 1e-6)
            frac = float(np.interp(tau, *self._shape))
        return n + min(max(frac, 0.0), 0.999)

    def _key(self, r: Rider, t: float):
        n = int(np.searchsorted(r.cross, t, side="right"))
        if n >= len(r.cross):                      # finished: laps, then who finished first
            return (float(len(r.cross)), -float(r.cross[-1]))
        return (self.progress(r, t), 0.0)

    def raw_order(self, t: float) -> list[Rider]:
        if t < self.start:
            return sorted(self.riders, key=lambda r: (r.grid or 99, r.finish_pos))
        return sorted(self.riders, key=lambda r: self._key(r, t), reverse=True)

    def _timeline(self):
        """Running order every STEP seconds, with hysteresis, plus events."""
        t0 = self.start - 5
        t1 = max(r.cross[-1] for r in self.riders) + 5
        self.times = np.arange(t0, t1, STEP)
        shown = [r.number for r in self.raw_order(t0)]
        cand, since = None, None
        self.orders: list[list[str]] = []
        self.changes: list[tuple[float, list[str], list[str]]] = []    # (t, before, after)
        crossings = sorted(float(c) for r in self.riders for c in r.cross)
        ci = 0
        for t in self.times:
            raw = [r.number for r in self.raw_order(t)]
            forced = False
            while ci < len(crossings) and crossings[ci] <= t:     # a line crossing is exact
                forced, ci = True, ci + 1
            if raw != shown:
                if forced or (cand == raw and t - since >= HOLD):
                    self.changes.append((float(t), shown, raw))
                    shown, cand = raw, None
                elif cand != raw:
                    cand, since = raw, t
            else:
                cand = None
            self.orders.append(shown)
        self.events = self._events()

    def order_at(self, t: float) -> list[str]:
        i = int(np.clip((t - self.times[0]) / STEP, 0, len(self.times) - 1))
        return self.orders[i]

    def pos(self, number: str, t: float, cls: str | None = None) -> int:
        order = self.order_at(t)
        if cls:
            order = [n for n in order if self.rider(n).cls == cls]
        return order.index(number) + 1

    # ------------------------------------------------------------ events

    def _events(self) -> list[Event]:
        me = self.me.number
        ev = [Event(self.start, "start", {"grid": self.me.grid})]
        for t, before, after in self.changes:
            b, a = before.index(me), after.index(me)
            if a < b:
                passed = [n for n in before[a:b] if after.index(n) > a]
                ev.append(Event(t, "gain", {"pos": a + 1, "passed": passed}))
            elif a > b:
                by = [n for n in after[b:a] if before.index(n) > b]
                ev.append(Event(t, "lose", {"pos": a + 1, "by": by}))
        leader = min(self.riders, key=lambda r: r.finish_pos)
        ev.append(Event(float(leader.cross[-1]), "chequered", {"leader": leader.number}))
        all_laps = []      # (time, lap time) for every lap of every rider, for "fastest lap"
        for r in self.riders:
            for i, lt in enumerate(r.lap_times):
                if i > 0 and np.isfinite(lt):
                    all_laps.append((float(r.cross[i]), lt, r.number))
        for i, c in enumerate(self.me.cross):
            n = i + 1
            lt = self.me.lap_times[i]
            done = n >= len(self.me.cross)
            ahead_gap = behind_gap = None
            order = self.order_at(c + 0.05)
            p = order.index(me)
            same_lap = lambda g: g if g is not None and 0 <= g < 0.8 * lt else None
            if p > 0:
                ra = self.rider(order[p - 1])
                if len(ra.cross) >= n:
                    ahead_gap = same_lap(float(c - ra.cross[n - 1]))
            if p + 1 < len(order):
                rb = self.rider(order[p + 1])
                if len(rb.cross) >= n:
                    behind_gap = same_lap(float(rb.cross[n - 1] - c))
            prior = [x[1] for x in all_laps if x[0] < c and x[2] != me]
            fastest = n > 1 and np.isfinite(lt) and (not prior or lt < min(prior))
            ev.append(Event(float(c), "finish" if done else "cross",
                            {"lap": n, "time": lt, "pos": p + 1, "left": self.total_laps - n,
                             "ahead": order[p - 1] if p > 0 else None, "ahead_gap": ahead_gap,
                             "behind": order[p + 1] if p + 1 < len(order) else None,
                             "behind_gap": behind_gap, "fastest": fastest,
                             "class_pos": self.pos(me, c + 0.05, self.me.cls)}))
        # lapped: the leader draws a full lap ahead of me
        lead_prog = lambda t: self.progress(leader, t)
        prev = None
        for t in self.times[::5]:
            if leader is self.me:
                break
            gap = lead_prog(t) - self.progress(self.me, t)
            if prev is not None and prev < 1.0 <= gap:
                ev.append(Event(float(t), "lapped", {"by": leader.number}))
            prev = gap
        return sorted(ev, key=lambda e: e.t)

    # ------------------------------------------------------------ for the HUD

    def board(self, t: float, cls: str | None = None) -> dict:
        """Standings at time t: rows with slide animation and change markers."""
        order = self.order_at(t)
        prev_order, since = order, 99.0
        for ct, before, after in reversed(self.changes):
            if ct <= t:
                prev_order, since = before, t - ct
                break
        if cls:
            order = [n for n in order if self.rider(n).cls == cls]
            prev_order = [n for n in prev_order if self.rider(n).cls == cls]
        k = min(since / SLIDE, 1.0)
        k = k * k * (3 - 2 * k)                      # ease in-out
        rows = []
        for i, n in enumerate(order):
            r = self.rider(n)
            j = prev_order.index(n) if n in prev_order else i
            moved = 0 if since > 4 else (j - i)        # + = moved up recently
            done = int(np.searchsorted(r.cross, t, side="right"))
            gap = None                                  # to the rider ahead, at the last crossing
            if i > 0 and done > 0:
                ra = self.rider(order[i - 1])
                if len(ra.cross) >= done:
                    gap = float(r.cross[done - 1] - ra.cross[done - 1])
                    if not 0 <= gap < 0.8 * float(np.nanmedian(r.lap_times)):
                        gap = None
            rows.append({"number": n, "name": r.last_name, "cls": r.cls, "row": j + (i - j) * k,
                         "moved": moved, "me": r is self.me, "gap": gap,
                         "lapped": self.laps_down(r, t) >= 1})
        me_done = int(np.searchsorted(self.me.cross, t, side="right"))
        return {"rows": rows, "pos": order.index(self.me.number) + 1 if self.me.number in order else None,
                "of": len(order), "lap": min(me_done + 1, self.total_laps), "laps": self.total_laps,
                "finished": me_done >= len(self.me.cross), "started": t >= self.start,
                "final_lap": me_done == self.total_laps - 1}

    def field(self, t: float, cls: str | None = None) -> list[tuple[str, float]]:
        """(number, fraction of the lap from the line) for every other rider
        still racing at time t, for dots on the map. Estimated between line
        crossings, like the running order."""
        if t < self.start:
            return []
        out = []
        for r in self.riders:
            if r is self.me or (cls and r.cls != cls) or t >= r.cross[-1]:
                continue
            if r.cross[0] - (r.lap_times[0] if np.isfinite(r.lap_times[0]) else 0) > t:
                continue                       # not out on track yet
            pr = self.progress(r, t)
            out.append((r.number, pr - math.floor(pr)))
        return out

    def laps_down(self, r: Rider, t: float) -> int:
        lead = self.rider(self.order_at(t)[0])
        if t < self.start or lead is r:
            return 0
        return int(self.progress(lead, t) - self.progress(r, t))

    def summary(self) -> str:
        me = self.me
        lines = [f"{len(self.riders)} riders, {self.total_laps} laps; you: #{me.number} {me.name} "
                 f"({me.cls}), started P{me.grid or '?'}, finished P{me.finish_pos}",
                 f"lined up with your RaceBox laps: {self.aligned_laps} crossings matched, "
                 f"within {self.align_error:.2f} s"]
        for e in self.events:
            if e.kind in ("gain", "lose"):
                who = e.data.get("passed") or e.data.get("by")
                names = ", ".join(f"#{n} {self.rider(n).last_name}" for n in who)
                verb = "passed" if e.kind == "gain" else "passed by"
                lines.append(f"  {e.t - self.start:6.1f}s into the race: {verb} {names} -> P{e.data['pos']}")
        return "\n".join(lines)


# ---------------------------------------------------------------- crew chief

RACE_KINDS = {
    "raceline": "Lap, position and laps to go",
    "racepos": "Passes and the start",
    "racegap": "Gaps to the riders around you",
}


def race_calls(race: Race, on: set[str] | None = None, units: str = "mph") -> list:
    """Spoken race calls: the line, passes, the start, gaps, the flag."""
    from .crewchief import Call, say_gap, say_laptime, words

    on = set(RACE_KINDS) if on is None else on
    me = race.me
    name = lambda n: race.rider(n).last_name
    P = lambda p: f"P {words(p)}"
    joined = lambda ns: " and ".join(name(n) for n in ns)
    out = []
    laps_me = len(me.cross)
    ev = race.events

    # the start: places made or lost by the end of the opening seconds
    if "racepos" in on and me.grid:
        t = race.start + 12
        p = race.pos(me.number, t)
        d = me.grid - p
        if d > 0:
            txt = f"Great start, up {words(d)}, {P(p)}." if d > 1 else f"Good start, up one, {P(p)}."
        elif d < 0:
            txt = f"Lost {words(-d)} off the start, {P(p)}. Plenty of time."
        else:
            txt = f"Clean start, holding {P(p)}."
        out.append(Call(t, txt, "racepos", 2))

    best = None
    prev_ahead = None             # (number, gap) at the previous crossing
    for e in ev:
        d = e.data
        if e.kind == "gain" and "racepos" in on and e.t > race.start + 12:
            out.append(Call(e.t + 0.3, f"You're past {joined(d['passed'])}. {P(d['pos'])}.", "racepos", 1))
        elif e.kind == "lose" and "racepos" in on and e.t > race.start + 12:
            who = joined(d["by"])
            out.append(Call(e.t + 0.3, f"{who} {'is' if len(d['by']) == 1 else 'are'} through. "
                                       f"{P(d['pos'])}.", "racepos", 1))
        elif e.kind == "lapped" and "racepos" in on:
            out.append(Call(e.t - 4, f"Blue flags, {name(d['by'])} is coming through to lap you.",
                            "racepos", 2))
        elif e.kind in ("cross", "finish") and ("raceline" in on or "racegap" in on):
            n, lt, p = d["lap"], d["time"], d["pos"]
            left = laps_me - n
            parts = []
            pb = np.isfinite(lt) and n > 2 and (best is None or lt < best)
            if e.kind == "finish":
                if "raceline" in on:
                    gained = (me.grid - p) if me.grid else 0
                    msg = f"Chequered flag! You finish {P(p)}"
                    if race.me.cls and d.get("class_pos") and d["class_pos"] != p:
                        msg += f", {P(d['class_pos'])} in {race.me.cls}"
                    msg += "."
                    if gained > 0:
                        msg += f" Up {words(gained)} from the grid."
                    elif gained < 0:
                        msg += f" Down {words(-gained)} from the grid."
                    parts.append(msg)
                    parts.append(f"Last lap {say_laptime(lt)}." if np.isfinite(lt) else "")
                    if d.get("behind_gap") is not None and d["behind_gap"] < 1.0:
                        parts.append(f"{name(d['behind'])} just {say_gap(d['behind_gap'])} behind. Great job.")
                    else:
                        parts.append("Great job.")
            else:
                if "raceline" in on:
                    parts.append(f"Lap {words(n)}, {say_laptime(lt)}. {P(p)}." if np.isfinite(lt) else f"Lap {words(n)}. {P(p)}.")
                    if d.get("fastest"):
                        parts.append("Fastest lap of the race!")
                    elif pb:
                        parts.append("Your best lap.")
                    if left == 1:
                        parts.append("Last lap!")
                    else:
                        parts.append(f"{words(left).capitalize()} to go.")
                if "racegap" in on:
                    a, ag = d.get("ahead"), d.get("ahead_gap")
                    b, bg = d.get("behind"), d.get("behind_gap")
                    if a and ag is not None and ag < 2.0:
                        trend = ""
                        if prev_ahead and prev_ahead[0] == a:
                            ch = ag - prev_ahead[1]
                            trend = " and closing" if ch < -0.1 else (" and pulling away" if ch > 0.15 else "")
                        parts.append(f"{name(a)} {say_gap(ag)} ahead{trend}.")
                    if b and bg is not None and bg < 1.0:
                        parts.append(f"{name(b)} right behind, {say_gap(bg)}.")
                prev_ahead = (d.get("ahead"), d.get("ahead_gap")) if d.get("ahead_gap") is not None else None
            if np.isfinite(lt) and n > 1:
                best = lt if best is None else min(best, lt)
            txt = " ".join(x for x in parts if x)
            if txt:
                kind = "raceline" if "raceline" in on else "racegap"
                out.append(Call(e.t + 0.8, txt, kind, 1, n))
    return sorted(out, key=lambda c: (c.t, c.priority))
