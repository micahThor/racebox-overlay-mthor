# racebox-overlay

Burns RaceBox lap-timer data into your onboard video (made for an
Insta360 GO 3S, works with any camera): lap timer with live
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

# auto-sync test inputs: a rotating-view video and a fake .insv with a gyro log
python tools/make_sync_sample.py sample/racebox_session.csv sample
racebox-overlay sync sample/turning_video.mp4 sample/racebox_session.csv --gyro sample/turning_video.insv
```

## Workflow

**1. Get the video.** Copy the MP4 straight off the camera (Insta360 GO 3S,
Ace Pro, or any camera). Use the original file rather than one exported from
the Insta360 app: originals carry the camera's gyro log, which makes sync
automatic. For a 360 camera (X3/X4), reframe in Insta360 Studio and export
an MP4, and keep the original `.insv` for syncing (see below).

**2. Export the data.** On racebox.pro → My Sessions → open the session →
Export → **CSV**. (VBO also loads, but it has no lap numbers, so laps will
start at an automatically chosen line unless you give a track file.)

**3. Check the session:**

```bash
racebox-overlay info session.csv
```

Lists every timed lap with sector times, best and theoretical-best lap.

**4. Sync.** Insta360 cameras record their gyro inside the original MP4.
The tool matches it against the RaceBox yaw rate (every corner is a
fingerprint in both), so the two line up to within a frame or two:

```bash
racebox-overlay sync VID_20261024_103015.mp4 session.csv
```

```
auto-sync: matching gyro recorded in VID_20261024_103015.mp4 against the RaceBox yaw rate (camera clock as a rough anchor)
  best match: --offset 12.54, lap 1 at 0:47.162   (match 0.86)
reuse with:  --offset 12.54
```

Check it with a preview:

```bash
racebox-overlay preview VID_20261024_103015.mp4 session.csv --offset 12.54 --at 0:48 --at 6:10
```

The lap timer should read ~0.8 s in the first PNG and the map dot should
match where you are. Fine-tune with `--nudge 0.1` if needed (positive moves
the data later relative to the video).

Things to know about auto-sync:
- Laps look alike, so a match one lap away can score nearly as well. The
  camera's own clock (time-zone mistakes corrected) narrows it to the right
  lap. If it's still unsure it says **NOT SURE** and lists the other
  candidates; preview them.
- If the file has no gyro log (an app export, another camera brand), it
  measures sideways motion in the video instead. That's slower and a bit
  less precise, but works on anything.
- 360 cameras: run it on your reframed export with `--gyro` pointing at the
  original `.insv`. The export should start where the `.insv` starts; if
  you trimmed the start in Studio, add `--export-start 8.0` (seconds cut).

Manual options, if auto-sync can't work:
- `--sync-lap 2@3:41.20`: lap 2 crosses the start line at 3:41.20 in the video
  (scrub to that frame yourself).
- `--offset 12.5`: the video starts 12.5 s after the RaceBox started logging.

`render` and `preview` sync automatically too when given no sync option and
an Insta360 file (or with `--auto-sync`), but running `sync` once and reusing
`--offset` saves repeating the work.

**5. Render:**

```bash
racebox-overlay render video.mp4 session.csv --offset 12.54 -o onboard.mp4
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
- `rbo/sync.py` — auto-sync: reads the IMU log from the `.insv` trailer (or
  measures image motion), then slides it along the RaceBox yaw rate with a
  normalised cross-correlation to find the best match.
- `rbo/video.py` — ffmpeg crops the panels back out, overlays them, and
  encodes with VideoToolbox, copying the original audio.

## Ideas for next steps

- A small GUI to scrub the video and set the sync point visually.
- Lap comparison (ghost of best lap on the map, delta graph).
- Overlay themes and a layout editor.
