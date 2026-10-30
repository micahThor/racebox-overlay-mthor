# racebox-overlay

Burns RaceBox lap-timer data into your onboard video: lap timer with live
delta, sector times, track map with your position, speed, and a G-force
circle (or a lean-angle gauge for bike sessions). Runs on your Mac, uses
ffmpeg with the VideoToolbox hardware encoder.

```
┌──────────────────────┐                    ┌──────────────┐
│ LAP 3/5      -0.32   │                    │   track map  │
│ 1:12.4               │                    │      ●       │
│ LAST 58.53 BEST 57.68│                    └──────────────┘
│ [S1 18.28][S2 11.7][S3]
└──────────────────────┘
┌──────────────┐                                ┌──────────┐
│ 101 MPH      │                                │ G circle │
│ ▬▬▬▬▬▬▬▬▬▬   │                                │    ●     │
│ ALT  YAW     │                                └──────────┘
└──────────────┘
```

## Setup (macOS)

```bash
brew install ffmpeg python
cd racebox-overlay
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Try it on generated sample data first:

```bash
python tools/make_sample.py sample
racebox-overlay info sample/racebox_session.csv
racebox-overlay preview sample/test_video.mp4 sample/racebox_session.csv --offset 12.5 --at 1:30
open preview.png
```

## Workflow

**1. Export the video.** In Insta360 Studio, reframe your 360 clip and export
an MP4 (H.264 or H.265, any aspect ratio — 16:9 and 9:16 both lay out fine).
Exporting the whole clip untrimmed makes syncing easiest.

**2. Export the data.** On racebox.pro → My Sessions → open the session →
Export → **CSV**. (VBO also loads, but it has no lap numbers, so laps will
start at an automatically chosen line unless you give a track file.)

**3. Check the session:**

```bash
racebox-overlay info session.csv
```

Lists every timed lap with sector times, best and theoretical-best lap.

**4. Sync, and check with a preview.** Scrub the video to the frame where you
cross the start/finish line at the start of a timed lap (lap 2 is usually
easy to spot). Then:

```bash
racebox-overlay preview video.mp4 session.csv --sync-lap 2@3:41.20 --at 3:42 --at 6:10
```

Look at the PNGs: the lap timer should read ~0.8 s in the first one and the
map dot should match where you are on track. If it's slightly off, add
`--nudge 0.2` (positive moves the data later relative to the video).

Other sync options:
- `--offset 12.5` — the video starts 12.5 s after the RaceBox started logging.
- nothing — uses the camera's recorded start time. Reframed exports often
  lose it; point `--time-source` at the original `.insv` instead. Expect it
  to be within a second or two, then fine-tune with `--nudge`.

**5. Render:**

```bash
racebox-overlay render video.mp4 session.csv --sync-lap 2@3:41.20 -o onboard.mp4
```

Add `--start 3:30 --end 12:00` to render only part of the clip.

## Options worth knowing

| option | |
|---|---|
| `--units kmh` | km/h and metres (default mph and feet) |
| `--scale 1.2` | make the HUD bigger or smaller |
| `--hide map,gforce` | drop panels (`laps`, `map`, `speed`, `gforce`) |
| `--sectors 4` | auto-split laps into N equal-distance sectors (default 3, 0 = off) |
| `--track file.json` | your own start/finish and sector points (see below) |
| `--delta-ref session-best` | live delta against the session's best lap instead of the best lap so far |
| `--codec hevc` | smaller files; `--bitrate 60M` to set quality |
| `--quality best` | software x264/x265, slower but best quality |
| `--font path.ttf` | any font; defaults to DIN Alternate Bold on macOS |

## Sectors and track files

Without a track file, sectors are equal-distance thirds of your best lap,
so they won't match the sectors in the RaceBox app. To use real sector
points, save the auto ones and edit them:

```bash
racebox-overlay info session.csv --save-track mytrack.json
```

Replace the `sectors` lat/lon pairs with the points you want (right-click
the track in Google Maps to copy coordinates). Reuse the file for every
session at that track with `--track mytrack.json`. See
`examples/track.example.json`.

## How it works

- `rbo/telemetry.py` — reads RaceBox CSV/VBO, matches columns by name, and
  checks each sensor channel against the GPS path, flipping it if the sign
  convention is backwards.
- `rbo/timing.py` — lap boundaries from the RaceBox lap column, refined to
  the exact line crossing between GPS samples; sector gates; live delta by
  distance into the lap against the reference lap.
- `rbo/hud.py` — Pillow drawing. Panels are packed into a small "atlas"
  image each frame, so only ~25% of the frame's pixels go through the pipe.
- `rbo/video.py` — ffmpeg crops the panels back out, overlays them, and
  encodes with VideoToolbox, copying the original audio.

## Ideas for next steps

- Automatic sync by matching the Insta360's gyro to the RaceBox yaw rate.
- A small GUI to scrub the video and set the sync point visually.
- Lap comparison (ghost of best lap on the map, delta graph).
- Overlay themes and a layout editor.
