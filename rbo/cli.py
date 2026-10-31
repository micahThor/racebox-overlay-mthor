"""racebox-overlay command line.

    racebox-overlay info    SESSION.csv                       laps, sectors, sync hints
    racebox-overlay preview VIDEO.mp4 SESSION.csv --at 1:23   still frames to check layout & sync
    racebox-overlay render  VIDEO.mp4 SESSION.csv -o out.mp4  the finished video
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import crewchief, project, sync, telemetry, timing, video
from .hud import Hud
from .timing import fmt_time


def parse_clock(s: str) -> float:
    """'83.5', '1:23.5' or '0:01:23.5' -> seconds."""
    total = 0.0
    for part in s.strip().split(":"):
        total = total * 60 + float(part)
    return total


def clock(s: float) -> str:
    sign = "-" if s < 0 else ""
    s = abs(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{sign}{int(h)}:{int(m):02d}:{sec:06.3f}" if h else f"{sign}{int(m)}:{sec:06.3f}"


# ---------------------------------------------------------------- loading

def apply_project(args) -> None:
    """Fill options the user didn't give from <video>.overlay.json (saved by the GUI)."""
    proj = project.load(args.video) if getattr(args, "video", None) and not args.no_project \
        else dict(project.DEFAULTS, layout={})
    args.project = proj
    if not args.session:
        args.session = proj.get("session")
        if not args.session:
            sys.exit("no session given, and none saved for this video yet")
        print(f"session: {args.session} (from {project.path_for(args.video).name})")
    for k in ("units", "scale", "theme", "track", "sectors", "speed_max",
              "crew_chief", "voice", "voice_rate", "call_settings", "ghost",
              "race", "race_source", "race_number", "race_class", "race_field"):
        if getattr(args, k, None) is None:
            setattr(args, k, proj.get(k) if proj.get(k) is not None else project.DEFAULTS[k])
    args.layout = proj.get("layout") or {}


def load_session(args):
    tel = telemetry.load(args.session)
    track = timing.load_track(args.track)
    tm = timing.Timing(tel, track=track, n_sectors=args.sectors, delta_ref=args.delta_ref)
    return tel, tm


def resolve_offset(args, tel, tm, vinfo: dict | None) -> tuple[float, str]:
    """Return (offset, how) where telemetry_time = video_time + offset."""
    if args.sync_lap:
        try:
            n, at = args.sync_lap.replace("=", "@").split("@", 1)
            n = int(n)
        except ValueError:
            sys.exit("--sync-lap looks like  3@4:12.40  (lap 3 starts at 4:12.40 in the video)")
        if not 1 <= n <= len(tm.laps):
            sys.exit(f"--sync-lap: there are {len(tm.laps)} timed laps")
        off = tm.laps[n - 1].t_start - parse_clock(at)
        how = f"lap {n} starts at {at} in the video"
    elif args.offset is not None:
        off = tel.t[0] + parse_clock(args.offset) if ":" in args.offset else tel.t[0] + float(args.offset)
        how = f"video starts {args.offset}s into the data"
    elif args.auto_sync or args.gyro:
        off, how = run_auto_sync(args, tel, tm, vinfo)
    elif getattr(args, "project", {}).get("offset") is not None:
        off = tel.t[0] + float(args.project["offset"])
        how = f"saved sync from {project.path_for(args.video).name} (offset {args.project['offset']:.2f}s)"
    elif sync.has_trailer(args.video):
        off, how = run_auto_sync(args, tel, tm, vinfo)
    else:
        created = None
        if args.time_source:
            created = video.probe(args.time_source)["created"]
        elif vinfo:
            created = vinfo["created"]
        if created is None:
            off, how = tel.t[0], "no sync info: assuming video and data start together (use --sync-lap)"
        else:
            off, how = _auto_offset(created, tel, vinfo["duration"] if vinfo else 0)
    off += args.nudge
    if args.nudge:
        how += f", nudged {args.nudge:+.2f}s"
    return off, how


def clock_prior(args, tel, vinfo) -> float | None:
    """Rough offset from the camera's recorded start time, or None."""
    src = args.gyro or args.time_source
    created = video.probe(src)["created"] if src else (vinfo or {}).get("created")
    if created is None:
        return None
    off, how = _auto_offset(created, tel, (vinfo or {}).get("duration", 0))
    if "doesn't match" in how:
        return None
    return off + (args.export_start if args.gyro else 0.0)


def run_auto_sync(args, tel, tm, vinfo) -> tuple[float, str]:
    prior = clock_prior(args, tel, vinfo)
    if not args.gyro and sync.has_trailer(args.video):
        src = f"gyro recorded in {Path(args.video).name}"
    else:
        src = f"gyro in {Path(args.gyro).name}" if args.gyro else "motion in the video"
    print(f"auto-sync: matching {src} against the RaceBox yaw rate"
          + (" (camera clock as a rough anchor)" if prior is not None else ""))
    try:
        r = sync.auto_sync(args.video, tel, gyro=args.gyro, export_start=args.export_start,
                           prior=prior)
    except sync.SyncError as e:
        sys.exit(f"auto-sync failed: {e}\nUse --sync-lap instead.")

    def where(off):
        rel = off - tel.t[0]
        lap1 = f", lap 1 at {clock(tm.laps[0].t_start - off)}" if tm.laps else ""
        return f"--offset {rel:.2f}{lap1}"

    print(f"  best match: {where(r.offset)}   (match {r.score:.2f})")
    if not r.confident:
        why = "weak match" if r.score < 0.5 else "other laps match almost as well"
        print(f"  NOT SURE ({why}). Preview to check; other candidates:")
        for off, sc in r.alternatives:
            print(f"    {where(off)}   (match {sc:.2f})")
    return r.offset, f"auto ({r.method})"


def _auto_offset(created: float, tel, duration: float) -> tuple[float, str]:
    """Camera clocks are often a time zone off (Insta360 writes local time
    labelled as UTC). Try whole-hour shifts and keep the one that overlaps the
    data best."""
    d0, d1 = tel.t[0], tel.t[-1]
    best, best_ov = 0, -1.0
    for h in range(-14, 15):
        s = created + h * 3600
        ov = min(s + max(duration, 1), d1) - max(s, d0)
        if ov > best_ov + 1:
            best, best_ov = h, ov
    if best_ov <= 0:
        return d0, "camera clock doesn't match the data; assuming they start together (use --sync-lap)"
    stamp = datetime.fromtimestamp(created, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    note = f" (shifted {best:+d}h for time zone)" if best else ""
    return created + best * 3600, f"camera clock {stamp} UTC{note}; typically within a second or two"


def race_cache(args) -> Path:
    base = getattr(args, "video", None) or args.session
    return Path(base).with_name(Path(base).name + ".race.json")


def load_race(args, tel, tm, quiet: bool = False):
    """The Race for race mode, or None when it's off."""
    if not getattr(args, "race", None):
        return None
    from . import race as racemod
    if not args.race_source:
        sys.exit("race mode needs the Speedhive session: --race-source "
                 "https://speedhive.mylaps.com/sessions/<number>")
    try:
        data = racemod.load(args.race_source, race_cache(args))
        if not args.race_number:
            rows = sorted(data["classification"]["rows"], key=lambda r: r["position"])
            sys.exit("race mode needs your start number: --race-number N. Riders: "
                     + ", ".join(f"#{str(r['startNumber']).strip()} {r['name'].strip()}" for r in rows))
        r = racemod.Race(data, tm, str(args.race_number))
    except racemod.RaceError as e:
        sys.exit(f"race mode: {e}")
    if not quiet:
        print(f"race: #{r.me.number} {r.me.name}, P{r.me.grid or '?'} on the grid -> P{r.me.finish_pos}; "
              f"{r.aligned_laps} line crossings matched your RaceBox laps (within {r.align_error:.2f} s)")
    return r


# ---------------------------------------------------------------- commands

def cmd_info(args):
    if args.sectors is None:
        args.sectors = 3
    tel, tm = load_session(args)
    meta = " · ".join(f"{k}: {v}" for k, v in tel.meta.items() if k in ("Data Source", "Track", "Date UTC"))
    start = datetime.fromtimestamp(tel.t[0], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    rate = (len(tel.t) - 1) / tel.duration if tel.duration else 0
    print(f"{Path(args.session).name}: {len(tel.t)} samples at {rate:.0f} Hz, "
          f"{clock(tel.duration)} long, starts {start}")
    if meta:
        print(f"  {meta}")
    chans = [n for n in ("alt", "g_long", "g_lat", "lean_deg", "yaw_rate", "lap") if getattr(tel, n) is not None]
    print(f"  channels: speed, position, {', '.join(chans)}{'  (bike mode)' if tel.is_bike else ''}")
    if tm.auto_line:
        print("  NOTE: no lap info in this file, so the start/finish line was placed automatically.\n"
              "        Lap times are right but laps start at the wrong spot. Export CSV from\n"
              "        RaceBox instead, or give a --track file.")
    if not tm.laps:
        print("  no timed laps found")
        return
    best = min(tm.laps, key=lambda l: l.time)
    print(f"\n  {'lap':>4}  {'time':>9}  {'starts in data':>14}   sectors")
    for l in tm.laps:
        secs = "  ".join(fmt_time(s) for s in l.sectors) if l.sectors else "-"
        star = " *" if l is best else ""
        print(f"  {l.number:>4}  {fmt_time(l.time):>9}  {clock(l.t_start - tel.t[0]):>14}   {secs}{star}")
    sec = [l.sectors for l in tm.laps if l.sectors]
    if sec:
        theo = sum(min(s[i] for s in sec) for i in range(len(sec[0])))
        print(f"\n  best {fmt_time(best.time)} (lap {best.number}), theoretical {fmt_time(theo)}")
    if not args.track:
        print(f"  sectors: {'auto, ' + str(args.sectors) + ' equal parts of the best lap' if tm.sector_gates else 'none'}")
    if args.save_track:
        Path(args.save_track).write_text(json.dumps(tm.track_dict(), indent=2) + "\n")
        print(f"  saved start/finish and sector points to {args.save_track}")


def _setup(args):
    apply_project(args)
    vinfo = video.probe(args.video)
    tel, tm = load_session(args)
    offset, how = resolve_offset(args, tel, tm, vinfo)
    hide = set(filter(None, (args.hide or "").split(",")))
    args._race = race = load_race(args, tel, tm)
    ghost_cfg = None
    if args.ghost:
        from .ghost import choose_ref
        ref = choose_ref(tm, offset, vinfo["duration"])
        if ref is None:
            print("ghost: no complete lap in this video to use as the ghost, so it's off")
        else:
            ghost_cfg = {"ref": ref, "offset": offset, "duration": vinfo["duration"]}
            print(f"ghost: lap {tm.laps[ref].number} ({fmt_time(tm.laps[ref].time)}), "
                  "your fastest lap in this video")
    try:
        hud = Hud(tel, tm, vinfo["width"], vinfo["height"], units=args.units, scale=args.scale,
                  font=args.font, hide=hide, theme=args.theme, layout=args.layout,
                  speed_max=args.speed_max, ghost=ghost_cfg, race=race,
                  race_class=bool(args.race_class), race_field=bool(args.race_field))
    except ValueError as e:
        sys.exit(str(e))
    print(f"sync: {how}")
    if tm.laps:
        v1 = tm.laps[0].t_start - offset
        inside = 0 <= v1 <= vinfo["duration"]
        print(f"      -> lap 1 starts at {clock(v1)} in the video" + ("" if inside else " (outside this clip)"))
    v_end = tel.t[-1] - offset
    if v_end < 0 or tel.t[0] - offset > vinfo["duration"]:
        print("      WARNING: the data doesn't overlap this video at all with this sync")
    return vinfo, tel, tm, offset, hud


def cmd_preview(args):
    vinfo, tel, tm, offset, hud = _setup(args)
    times = [parse_clock(a) for a in args.at] if args.at else [vinfo["duration"] / 2]
    base = Path(args.output or "preview.png")
    for i, vt in enumerate(times):
        frame = video.grab_frame(args.video, vt)
        img = hud.compose(frame, vt + offset, ghost_frame=lambda gvt, w, h: np.asarray(
            video.grab_frame(args.video, gvt).resize((w, h))))
        out = base if len(times) == 1 else base.with_name(f"{base.stem}_{i + 1}{base.suffix}")
        img.save(out)
        print(f"  {clock(vt)} -> {out}")


def cmd_sync(args):
    args.auto_sync = True
    apply_project(args)
    vinfo = video.probe(args.video)
    tel, tm = load_session(args)
    off, _ = resolve_offset(args, tel, tm, vinfo)
    print(f"\nreuse with:  --offset {off - tel.t[0]:.2f}")
    if args.save:
        proj = args.project
        proj.update(session=str(Path(args.session).resolve()), offset=round(off - tel.t[0], 3))
        print(f"saved to {project.save(args.video, proj)}")


def call_settings(args) -> dict:
    """Saved crew chief settings, with any command-line options on top."""
    s = dict(getattr(args, "call_settings", None) or {})
    for opt, key in (("wide", "wide_m"), ("early_brake", "early_brake_m"),
                     ("min_loss", "min_loss"), ("max_corner_calls", "max_corner_calls"),
                     ("consistent_within", "consistency_s")):
        if getattr(args, opt, None) is not None:
            s[key] = getattr(args, opt)
    if getattr(args, "slow_corner", None) is not None:      # given in display units
        s["slow_corner_kmh"] = args.slow_corner * (1.609344 if args.units == "mph" else 1)
    if getattr(args, "calls_off", None) is not None:
        off = [k.strip() for k in args.calls_off.split(",") if k.strip()]
        from .race import RACE_KINDS
        kinds = list(crewchief.KINDS) + list(RACE_KINDS)
        bad = [k for k in off if k not in kinds]
        if bad:
            sys.exit(f"--calls-off: unknown {', '.join(bad)}; choose from {', '.join(kinds)}")
        s["off"] = off
    return s


def chief(args, tel, tm, offset: float | None = None, duration: float | None = None):
    ghost_ref = None
    if getattr(args, "ghost", None) and offset is not None and duration is not None:
        from .ghost import choose_ref
        ghost_ref = choose_ref(tm, offset, duration)
    race = getattr(args, "_race", None)
    if race is None and getattr(args, "race", None):
        race = args._race = load_race(args, tel, tm)
    return crewchief.CrewChief.from_settings(tel, tm, units=args.units, settings=call_settings(args),
                                             ghost_ref=ghost_ref, race=race)


def radio_style(args) -> str:
    from .themes import load_theme
    try:
        return load_theme(getattr(args, "theme", None)).get("radio", "classic")
    except ValueError:
        return "classic"


def make_radio(args, tel, tm, offset: float, start: float, end: float, out_wav: str,
               quiet: bool = False):
    """Work out the crew chief calls for video times start..end and write the
    radio track. Returns the calls that were placed."""
    if crewchief.tts_backend() is None:
        sys.exit("crew chief needs a speech engine: `say` (built into macOS) wasn't found")
    voice = getattr(args, "voice", None) or crewchief.default_voice()
    calls = chief(args, tel, tm, offset, getattr(args, "_duration", None)).calls()

    def prog(i, n):
        if not quiet:
            print(f"\r  recording crew chief calls {i}/{n}", end="", flush=True)

    track, placed = crewchief.build_track(calls, start + offset, end + offset, voice=voice,
                                          rate=int(getattr(args, "voice_rate", None) or 190),
                                          progress=prog, style=radio_style(args))
    if not quiet:
        print()
    crewchief.write_wav(out_wav, track)
    return placed


def cmd_voices(args):
    vs = crewchief.list_voices()
    if vs:
        print("\n".join(vs))
        print(f"\ndefault: {crewchief.default_voice()}")
    else:
        print("no `say` voices found; they're built into macOS")


def cmd_calls(args):
    apply_project(args)
    vinfo = video.probe(args.video)
    tel, tm = load_session(args)
    offset, how = resolve_offset(args, tel, tm, vinfo)
    print(f"sync: {how}")
    start = parse_clock(args.start) if args.start else 0.0
    end = parse_clock(args.end) if args.end else vinfo["duration"]
    args._duration = vinfo["duration"]
    if args.wav:
        placed = make_radio(args, tel, tm, offset, start, end, args.wav)
        print(f"wrote {args.wav}")
    else:
        placed = [c for c in chief(args, tel, tm, offset, vinfo["duration"]).calls()
                  if start <= c.t - offset <= end]
        for c in placed:
            c.start = c.t
    for c in placed:
        print(f"  {clock(c.start - offset):>10}  {c.text}")
    if not placed:
        print("  no calls in this part of the video")


def cmd_race(args):
    """Check a race against a RaceBox session, without any video."""
    if args.sectors is None:
        args.sectors = 3
    tel, tm = load_session(args)
    args.video, args.race = None, True
    args.race_source, args.race_number, args.units = args.source, args.number, args.units or "mph"
    r = load_race(args, tel, tm, quiet=True)
    print(r.summary())
    print("\n  running order after each of your laps:")
    for i, c in enumerate(r.me.cross):
        order = r.order_at(c + 0.05)
        print(f"  lap {i + 1}: " + " ".join(("[" + n + "]") if n == r.me.number else n for n in order))
    args.call_settings, args.ghost = {}, None
    print("\n  race calls (time into the RaceBox session):")
    from .race import race_calls
    for c in race_calls(r, units=args.units):
        print(f"  {clock(c.t - tel.t[0]):>10}  {c.text}")


def cmd_render(args):
    vinfo, tel, tm, offset, hud = _setup(args)
    start = parse_clock(args.start) if args.start else 0.0
    end = parse_clock(args.end) if args.end else None
    out = args.output or str(Path(args.video).with_name(Path(args.video).stem + "_overlay.mp4"))
    print(f"rendering {vinfo['width']}x{vinfo['height']} @ {float(vinfo['fps']):.2f} fps -> {out}")
    radio = None
    args._duration = vinfo["duration"]
    if args.crew_chief:
        import tempfile
        radio = str(Path(tempfile.mkdtemp()) / "crew_chief.wav")
        placed = make_radio(args, tel, tm, offset, start, end or vinfo["duration"], radio)
        print(f"  {len(placed)} crew chief calls")
    try:
        video.render(args.video, hud, offset, out, start, end, args.codec, args.bitrate, args.quality,
                     workers=args.workers, radio=radio)
    except video.RenderError as e:
        sys.exit(str(e))


def cmd_gui(args):
    from .gui import serve
    serve(args.video, args.session, port=args.port, open_browser=not args.no_browser)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="racebox-overlay",
                                 description="Overlay RaceBox lap timing and telemetry on video.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def session_opts(p):
        p.add_argument("--track", help="track JSON with start_finish / sectors points")
        p.add_argument("--sectors", type=int, default=None,
                       help="auto-split each lap into N sectors when no track file (default 3, 0 = off)")
        p.add_argument("--delta-ref", choices=["best-so-far", "session-best"], default="best-so-far",
                       help="what the live delta compares against")

    def video_opts(p):
        p.add_argument("video")
        p.add_argument("session", nargs="?",
                       help="RaceBox CSV (or VBO) export; optional once saved by the GUI")
        p.add_argument("--no-project", action="store_true",
                       help="ignore settings saved next to the video by the GUI")
        session_opts(p)
        g = p.add_argument_group("sync (pick one; default: automatic for Insta360 files, else camera clock)")
        g.add_argument("--sync-lap", metavar="N@TIME",
                       help="lap N crosses the start line at TIME in the video, e.g. 2@3:41.20")
        g.add_argument("--offset", metavar="SECONDS",
                       help="the video starts this far into the data (can be negative)")
        g.add_argument("--auto-sync", action="store_true",
                       help="find the offset by matching camera rotation to the RaceBox yaw rate")
        g.add_argument("--gyro", metavar="INSV",
                       help="original Insta360 .insv to take the camera gyro from (implies --auto-sync)")
        g.add_argument("--export-start", type=float, default=0.0, metavar="SECONDS",
                       help="if the export was trimmed: how far into the .insv it starts")
        g.add_argument("--time-source", metavar="FILE",
                       help="read the camera clock from this file (e.g. the original .insv)")
        g.add_argument("--nudge", type=float, default=0.0, help="shift the sync by this many seconds")
        s = p.add_argument_group("look")
        s.add_argument("--units", choices=["mph", "kmh"], default=None, help="default mph")
        s.add_argument("--scale", type=float, default=None, help="HUD size multiplier")
        s.add_argument("--theme", help="carbon, daylight, neon, minimal, broadcast, hud, arcade, vintage, or a theme .json file")
        s.add_argument("--ghost", action=argparse.BooleanOptionalAction, default=None,
                       help="picture-in-picture of your best lap, matched to your position")
        s.add_argument("--speed-max", type=float, default=None, metavar="N",
                       help="top of the speedometer scale (default 70 mph / 120 km/h)")
        r = p.add_argument_group("crew chief radio")
        r.add_argument("--crew-chief", action=argparse.BooleanOptionalAction, default=None,
                       help="add spoken crew chief calls to the video's audio")
        r.add_argument("--voice", help="macOS voice name (see: racebox-overlay voices)")
        r.add_argument("--voice-rate", type=int, default=None, help="words per minute (default 190)")
        r.add_argument("--wide", type=float, metavar="M",
                       help="call 'wide' at this many metres outside your best line (default 2.5)")
        r.add_argument("--early-brake", type=float, metavar="M",
                       help="call 'braked early' at this many metres earlier (default 12)")
        r.add_argument("--slow-corner", type=float, metavar="N",
                       help="call a slow corner at this much less speed, in mph or km/h (default ~4 mph)")
        r.add_argument("--min-loss", type=float, metavar="S",
                       help="a corner mistake must cost at least this many seconds (default 0.1)")
        r.add_argument("--max-corner-calls", type=int, metavar="N",
                       help="most corner mistake calls per lap (default 2)")
        r.add_argument("--consistent-within", type=float, metavar="S",
                       help="laps within this spread count as consistent (default 0.35)")
        r.add_argument("--calls-off", metavar="LIST",
                       help="kinds of call to leave out: " + ",".join(crewchief.KINDS))
        rc = p.add_argument_group("race mode (MYLAPS Speedhive)")
        rc.add_argument("--race", action=argparse.BooleanOptionalAction, default=None,
                        help="standings tower and race calls (needs --race-source and --race-number once)")
        rc.add_argument("--race-source", metavar="LINK", default=None,
                        help="Speedhive session link or number, or a saved race JSON")
        rc.add_argument("--race-number", metavar="N", default=None, help="your start number in that race")
        rc.add_argument("--race-class", action=argparse.BooleanOptionalAction, default=None,
                        help="tower shows only your class")
        rc.add_argument("--race-field", action=argparse.BooleanOptionalAction, default=None,
                        help="show the other riders as dots on the map (default on)")
        s.add_argument("--font", help="path to a .ttf/.ttc/.otf to use")
        s.add_argument("--hide", help="comma list of panels to hide: laps,map,speed,gforce")
        p.add_argument("-o", "--output")

    p = sub.add_parser("info", help="list laps and sectors in a session")
    p.add_argument("session")
    session_opts(p)
    p.add_argument("--save-track", metavar="FILE", help="write the start/finish and sector points as a track file")
    p.set_defaults(fn=cmd_info)

    p = sub.add_parser("preview", help="render still frames to check the layout and sync")
    video_opts(p)
    p.add_argument("--at", action="append", help="video time(s) to preview, e.g. --at 1:20 --at 3:05.5")
    p.set_defaults(fn=cmd_preview)

    p = sub.add_parser("sync", help="work out the video/data offset automatically")
    video_opts(p)
    p.add_argument("--save", action="store_true", help="save the result next to the video")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("calls", help="list the crew chief calls, or save them as audio")
    video_opts(p)
    p.add_argument("--start", help="from this video time")
    p.add_argument("--end", help="to this video time")
    p.add_argument("--wav", metavar="FILE", help="also save the radio track as a WAV to listen to")
    p.set_defaults(fn=cmd_calls)

    p = sub.add_parser("voices", help="list the English macOS voices for the crew chief")
    p.set_defaults(fn=cmd_voices)

    p = sub.add_parser("gui", help="open the sync & layout editor in your browser")
    p.add_argument("video")
    p.add_argument("session", nargs="?", help="RaceBox CSV/VBO (optional once saved)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    p.set_defaults(fn=cmd_gui)

    p = sub.add_parser("race", help="check a Speedhive race against a RaceBox session (no video needed)")
    p.add_argument("session", help="RaceBox CSV/VBO from the race")
    p.add_argument("source", help="Speedhive session link or number, or a saved race JSON")
    p.add_argument("--number", required=True, help="your start number")
    p.add_argument("--units", choices=["mph", "kmh"], default=None)
    session_opts(p)
    p.set_defaults(fn=cmd_race)

    p = sub.add_parser("render", help="render the finished video")
    video_opts(p)
    p.add_argument("--start", help="only render from this video time")
    p.add_argument("--end", help="only render up to this video time")
    p.add_argument("--codec", choices=["h264", "hevc"], default="h264")
    p.add_argument("--bitrate", help="e.g. 40M (default scales with resolution)")
    p.add_argument("--quality", choices=["fast", "best"], default="fast",
                   help="fast = hardware encoder (VideoToolbox) when available")
    p.add_argument("--workers", type=int, default=None,
                   help="CPU cores drawing the overlay in parallel (default: all but one)")
    p.set_defaults(fn=cmd_render)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
