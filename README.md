# racebox-overlay

Burns RaceBox lap-timer data into your onboard video (made for an
Insta360 GO 3S, works with any camera): lap timer with live
delta, sector times, track map with your position, speed, and a G-force
circle (or a lean-angle gauge for bike sessions). Runs on your Mac, uses
ffmpeg with the VideoToolbox hardware encoder.

The overlay has four panels, each movable, resizable and optional:

- **Lap timer:** running lap time, live delta to your best lap, last and
  best lap, and a sector bar that fills as you ride each sector, turning
  purple (session best), green (improved) or yellow.
- **Track map:** your racing line with a motorcycle marker showing where you
  are, plus the start/finish line and sector points.
- **Speedometer:** a 0–70 mph gauge (0–120 km/h in metric) with a red zone
  near the top, plus altitude and yaw rate. Change the top speed with
  `--speed-max` or in the editor.
- **G-force** circle with a trail (or a **lean angle** gauge for bike-mode
  RaceBox sessions).

Everything is drawn at twice the output resolution and scaled down, so
gauges and curves stay smooth even in 4K.

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

## The editor

```bash
racebox-overlay gui VID_20261024_103015.mp4 session.csv
```

This opens the editor in your browser: your video with the overlay on top,
and a lap timeline under it showing where each lap falls.

- **Sync tab:** press *Auto-sync*, or pause on the frame where you cross
  the start/finish line, choose the lap, and press *Set sync here*. Nudge by
  a frame or a tenth of a second until the timer resets exactly on the line.
  The lap segments on the timeline move as you adjust, so you can see
  straight away whether laps start where the video says they do.
- **Look & layout tab:** pick a theme, mph or km/h, and overall size; show
  or hide panels and resize them; drag panels on the video to move them.
- **Render video** at the bottom renders the whole clip, or a section if
  you enter From/To times, to `VID_..._overlay.mp4` next to the original.

Everything saves as you go to `VID_....mp4.overlay.json` next to the
video. `render` and `preview` read that file, so after using the editor
`racebox-overlay render VID_20261024_103015.mp4` needs no other options.
Command-line options still override what's saved; `--no-project` ignores
the file.

Keys: Space plays/pauses, Left/Right step one frame, Shift+Left/Right one
second. The GO 3S can record H.265; Safari and Chrome on a Mac play it, but
if the video won't load in your browser, switch the camera to H.264.

## Crew chief radio

Adds a race engineer on the radio to your video: short spoken calls at the
right moments, with a team-radio sound, and the bike's own audio dipping
under each call.

- **Every lap:** "Lap four, one twenty-nine eight. Purple lap, three tenths
  quicker." or "Just a tenth off."
- **Purple sectors:** "Purple sector two."
- **Theoretical best:** "There's a one twenty-nine flat in you."
- **Mistakes, compared with your best lap so far, only when they cost time:**
  "Wide at turn five, cost you two tenths.", "Braked early into turn three,
  half a second lost.", "Turn two, carried twelve miles an hour less."
  Running wide is always mentioned, even when it cost nothing.
- **Good news:** "Good braking into turn three, picked up two tenths."
- **Top speed:** "New top speed, one eighteen."
- **Consistency:** "Three laps within two tenths. Nice and consistent."

Turn numbers are corners the app finds itself, counted from the
start/finish line, so they may not match the track's official numbering.
A corner is only called out if you ran 2.5 m or more wide, braked 12 m or
more earlier, or carried noticeably less speed through it. At most two
mistakes are called per lap, so it stays a crew chief rather than a
commentator.

Turn it on with the switch in the editor's **Crew chief** tab (where you can
also pick the voice, set the speed, see every call and play each one), or
from the command line:

```bash
racebox-overlay calls VID.mp4 session.csv              # list the calls with their video times
racebox-overlay calls VID.mp4 --wav radio.wav          # hear the radio track on its own
racebox-overlay render VID.mp4 --crew-chief            # add it to the video
racebox-overlay voices                                 # the voices on your Mac
```

The voice is macOS's built-in `say`, so it's free and works offline. It uses
"Daniel" (British English) if installed, otherwise your Mac's default; choose
another with `--voice` or in the editor. More voices can be downloaded in
System Settings → Accessibility → Spoken Content → System voice → Manage
Voices.

## Themes

Four built in: **carbon** (dark cards, default), **daylight** (light cards
for bright, washed-out footage), **neon** (outlined, cyan), and **minimal**
(no cards, outlined text). Pick one in the editor or with `--theme neon`.

Make your own as a JSON file. Start from a built-in and change only what you
want; the full list of keys is in `rbo/themes.py`:

```json
{ "base": "carbon", "accent": [0, 160, 255], "bg": [0, 20, 40, 190], "radius": 6 }
```

Then use `--theme mytheme.json`, or put the file's path in the `theme` field
of the `.overlay.json`.

## Options worth knowing

| option | |
|---|---|
| `--units kmh` | km/h and metres (default mph and feet) |
| `--theme neon` | carbon, daylight, neon, minimal, or a theme `.json` |
| `--speed-max 80` | top of the speedometer (default 70 mph / 120 km/h) |
| `--workers 4` | CPU cores drawing the overlay (default: all but one) |
| `--crew-chief` / `--no-crew-chief` | spoken crew chief calls in the audio |
| `--voice Daniel` | which macOS voice the crew chief uses |
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
- `rbo/hud.py` — Pillow drawing, at 2x resolution then scaled down for
  smooth edges. Panels are packed into a small "atlas" image each frame, so
  only ~25% of the frame's pixels go through the pipe, and frames are drawn
  on several CPU cores at once.
- `rbo/sync.py` — auto-sync: reads the IMU log from the `.insv` trailer (or
  measures image motion), then slides it along the RaceBox yaw rate with a
  normalised cross-correlation to find the best match.
- `rbo/gui.py` + `gui.html` — the editor: a small local web server that
  streams the video to your browser and draws overlay frames on request,
  using the same drawing code as the final render.
- `rbo/crewchief.py` — finds corners on your best lap, compares each lap with
  it (time, line, braking point, corner speed), turns findings into calls,
  speaks them, adds the radio sound and schedules them so none overlap.
- `rbo/themes.py`, `rbo/project.py` — themes, and the per-video settings file.
- `rbo/video.py` — ffmpeg crops the panels back out, overlays them, and
  encodes with VideoToolbox, copying the original audio.

## Ideas for next steps

- Lap comparison (ghost of best lap on the map, delta graph).
- Join split recordings (long sessions spanning several video files).
- Live crew chief calls in your helmet while riding (needs a live Bluetooth
  link to the RaceBox and a phone app).
