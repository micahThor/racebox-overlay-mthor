# racebox-overlay

Burns RaceBox lap-timer data into your onboard video (made for an
Insta360 GO 3S, works with any camera): lap timer with live
delta, sector times, track map with your position, speed, and a G-force
circle (or a lean-angle gauge for bike sessions). Runs on your Mac, uses
ffmpeg with the VideoToolbox hardware encoder.

The overlay has these panels, each movable, resizable and optional:

- **Lap timer:** running lap time, live delta to your best lap, last and
  best lap, and a sector bar that fills as you ride each sector, turning
  purple (session best), green (improved) or yellow.
- **Track map:** your racing line with a motorcycle marker showing where you
  are, plus the start/finish line and sector points.
- **Speedometer:** a 0–70 mph gauge (0–120 km/h in metric) with a red zone
  near the top. Change the top speed with
  `--speed-max` or in the editor.
- **Ghost lap** (optional): your best lap's footage in a picture-in-picture,
  matched to your position on track.
- **Race mode** (optional): a live standings tower from MYLAPS Speedhive
  timing that reorders as passes happen, with race calls on the radio.
- **G-force** circle with a trail (or a compact **lean angle** gauge for
  bike-mode RaceBox sessions: the angle, L or R, and one simple indicator).
- **Throttle & brake** gauge: a bar that fills green upward when you're
  accelerating and red downward when you're braking, the G figure, and
  THROTTLE / COAST / BRAKE. See "Throttle & brake" below.

Everything is drawn at twice the output resolution and scaled down, so
gauges and curves stay smooth even in 4K.

## Setup (macOS)

```bash
brew install ffmpeg python
cd racebox-overlay
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
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
racebox-overlay gui
```

This opens the editor in your browser. Choose your video and RaceBox file
with the **Choose…** buttons (your Mac's normal file picker), or click one
of your recent videos; a video you've used before remembers its RaceBox file
and every setting. **Open…** at the top switches to other files later. You
can still name the files on the command line:
`racebox-overlay gui VID_20261024_103015.mp4 session.csv`.

You'll see your video with the overlay on top, and a lap timeline under it
showing where each lap falls.

- **Sync tab:** press *Auto-sync*, or pause on the frame where you cross
  the start/finish line, choose the lap, and press *Set sync here*. Nudge by
  a frame or a tenth of a second until the timer resets exactly on the line.
  The lap segments on the timeline move as you adjust, so you can see
  straight away whether laps start where the video says they do.
- **Look & layout tab:** pick a theme, mph or km/h, and overall size; show
  or hide panels and resize them; drag panels on the video to move them.
  Hover a panel on the video for its handles: the corner resizes it evenly;
  the side and bottom handles change only its width or height. Those work on
  the panels whose contents re-flow: the track map (the track fits the new
  shape), the standings (narrower cuts long names, taller spreads the rows),
  the lap timer (width) and the ghost (its video stays in proportion inside).
  The round gauges only resize evenly so they don't distort. **Reset shape**
  in the panel list undoes it.
  Under **Lap timer & sectors**, choose what it shows: **Lap time**,
  **Sectors**, **Last lap**, **Best lap**. The card resizes to fit: just the
  lap time is a small card, all four is the full-size one (command line:
  `--lap-items time,best`).
  Under **Track map**, turn the map in quarter turns: **0°** (as recorded),
  **90°**, **180°** or **270°** clockwise, or **↻** for the next quarter turn.
  A turned map keeps its straights straight; a tall track turned 90° suits a
  wide frame and vice versa. From the command line: `--map-rotate 90`.
- **Render video** at the bottom renders the whole clip, or a section, to
  `VID_..._overlay.mp4` next to the original. To trim, scrub to where it
  should begin and press **Set start here** (or the `[` key), then to the end
  and press **Set end here** (`]`); you can also type From/To times. The
  parts that won't be rendered are shaded on the timeline, and **Clear**
  goes back to the whole video. The trim is saved, so `racebox-overlay
  render` uses it too (`--start 0` renders everything).
- **The Terminal** where you started the editor prints a clear result for
  every sync: `auto-sync SUCCEEDED`, `FINISHED BUT NOT SURE` (with the other
  candidates) or `FAILED` (with why), which method it used (the camera's
  gyro log, or the slower motion-in-the-picture fallback), and syncs set by
  hand and for a ghost from another session.

Everything saves as you go to `VID_....mp4.overlay.json` next to the
video. `render` and `preview` read that file, so after using the editor
`racebox-overlay render VID_20261024_103015.mp4` needs no other options.
Command-line options still override what's saved; `--no-project` ignores
the file.

Keys: Space plays/pauses, Left/Right step one frame, Shift+Left/Right one
second. The GO 3S can record H.265; Safari and Chrome on a Mac play it, but
if the video won't load in your browser, switch the camera to H.264.

## The editor on the web, for your club

`racebox-overlay serve` runs the same editor as a web app that club members sign in
to. Each person has their own uploaded videos, RaceBox files and saved layouts, renders
take turns, and finished videos download from the editor. `infra/` sets it up on AWS
(one EC2 server behind CloudFront, Cognito sign-in, S3 for downloads), and GitHub
Actions deploys it on every push. The server sleeps when nobody's using it. Setup,
costs and adding members are in [docs/aws.md](docs/aws.md).

## Ghost lap

A picture-in-picture window plays your fastest lap's footage alongside
the current lap. It's matched to where you are on track, not to the clock,
so the ghost is always in the same corner as you: you can see exactly
where your line, braking point and speed differ. The label says how far
ahead or behind the ghost is ("ghost 12 m ahead").

- The ghost is your fastest lap that's entirely inside the video (its
  footage is needed), so record the whole session in one clip if you can.
- It shows during every other timed lap, and hides during the out-lap,
  the in-lap and the fastest lap itself.
- Turn it on with the switch at the top of the editor's **Look & layout**
  tab, where the preview plays the ghost live, or with `--ghost`. Move and
  resize it like any other panel.

### A ghost from another session

The ghost can also be any lap from another RaceBox file and its video at the
same track: your own lap from another day, or a friend's.

- In the editor, choose **Another session** under the ghost switch, paste the
  RaceBox file and its video with the **Choose…** buttons (or paste their
  paths), press **Load laps** and pick the lap.
  Laps are numbered as that RaceBox file numbers them.
- From the command line:
  `racebox-overlay render VID.mp4 --ghost --ghost-session other.csv --ghost-video other.mp4 --ghost-lap 4`
- The other video needs its own sync. If you've opened it in the editor
  before, that sync is used; otherwise it's auto-synced, and the result is
  saved. Nudge it with the ghost sync buttons (or `--ghost-offset`) if the
  ghost's picture doesn't match where its lap is.
- The other lap is cut at *this* session's start/finish line, so both laps
  start at the same spot even if the two RaceBoxes put the line a few metres
  apart. If the lap doesn't follow this track, or it isn't inside its video
  with that sync, you're told why.
- The label reads `GHOST · OCT 12 · LAP 4  1:04.88` (the date is left out
  when it's the same day). The gap, and the crew chief's ghost calls and
  corner comparisons, work as they do with your own lap. GPS can drift a
  metre or two between days, so treat small line differences as noise.

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
- **Against the ghost** (when the ghost lap is on): where you stand at the
  sector splits ("Ghost eight metres ahead", "Four metres up on the ghost"),
  overtakes ("You're ahead of the ghost!"), corners where you won or lost
  real time on it ("Lost two tenths to the ghost through turn six"), and the
  margin at the line ("Beat the ghost by three metres"). While the ghost is
  on, corner mistakes are also judged against the ghost lap, so the voice
  and the picture always agree.

Turn numbers are corners the app finds itself, counted from the
start/finish line, so they may not match the track's official numbering.
A corner is only called out if you ran 2.5 m or more wide, braked 12 m or
more earlier, or carried noticeably less speed through it. At most two
mistakes are called per lap, so it stays a crew chief rather than a
commentator.

**Too chatty, or not enough?** Everything is adjustable, per video:

| Setting | Default | Command-line option |
|---|---|---|
| Running wide: metres outside your best line | 2.5 m | `--wide 4` |
| Braking early: metres earlier than your best lap | 12 m | `--early-brake 20` |
| Slow corner: speed lost through it | ~4 mph | `--slow-corner 6` (mph or km/h, per `--units`) |
| A corner mistake must cost at least | 0.1 s | `--min-loss 0.2` |
| Most corner mistake calls per lap | 2 | `--max-corner-calls 1` |
| Consistent laps: spread | 0.35 s | `--consistent-within 0.5` |
| Ghost gap update: change since the last one | 5 m | (editor) |

Any kind of call can be switched off: `--calls-off topspeed,consistency`
(kinds: lap, sector, theoretical, corner, good, topspeed, consistency, ghost).
In the editor these are sliders and switches in the Crew chief tab, and the
list of calls updates as you move them, so you can see exactly what you'll
hear before rendering. Each check is separate: if you raise the "wide"
distance, a slow corner on that lap can still be called as slow.

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

## Race mode

For races timed by MYLAPS. Turn it on for a race and leave it off for
practice; it's a separate switch from everything else.

```
racebox-overlay race SESSION.csv https://speedhive.mylaps.com/sessions/12935135 --number 96
racebox-overlay render VID.mp4 --race --race-source https://speedhive.mylaps.com/sessions/12935135 --race-number 96
```

Or open the editor's **Race** tab, switch it on, paste the Speedhive link and
pick your number. Settings save with the video, so after that `--race` or
`--no-race` is enough.

- **Standings tower:** lap X/Y, FINAL LAP and FINISHED with a chequered flag,
  your position, every rider's number, name and gap to the rider ahead
  (`+1 LAP` when lapped). Rows slide to their new places when a pass
  happens, with green/red arrows for a few seconds. `--race-class` (or the
  checkbox) shows only your class. Drag it like any panel. The lap counter
  on the lap timer switches to race laps too.
- **One race panel:** in race mode the track map and the standings are a
  single panel (top right by default): the map on top, the standings under
  it, and the class colour key along the bottom. Move, resize and reshape it as one ("Track
  map & standings" in the panel list); the map's turn setting still applies.
  Hide either part to get the other on its own.
- **Off the picture:** the race panel starts in the right strip of the frame
  round the video (see "The frame round the video" below), so it doesn't
  cover the picture; move it in the Race tab or by dragging.
- **The field on the map:** every other rider still racing is shown on the
  track map as a tag with their race number, and you are a bigger circle
  (a square in Retro Arcade) with your own race number in it, so you stand out. When several classes race together, each
  class has its own colour: the tags are coloured by class, a key along the
  bottom of the race panel says which colour is which, and the standings mark each row with the
  same colour. Positions between timing-line
  crossings are estimates (see below). Turn the field off with
  `--no-race-field` or the checkbox in the Race tab.
- **Race calls** (with the crew chief radio on; they replace the plain lap
  call):
  - **The start**, said before turn one: who you got or who got you off the
    line and your position into turn one ("Good launch, past Eifert. P nine
    into turn one."). Then, once through it: "Got Demboske into turn one. P
    eight out of turn one. Up two from the grid."
  - **At the line:** lap time, position and laps to go, "Last lap!", your
    best lap, fastest lap of the race.
  - **Passes:** "You're past Eifert. P nine." and "Eifert is through. P ten."
  - **Gaps:** to the rider ahead (closing or growing) and to anyone right
    behind; blue flags before the leader laps you.
  - **The flag:** your overall and class finish and places made from the grid.
  Each group can be switched off (`--calls-off raceline,racepos,racegap`).
- **Traffic awareness:** a lap where you spent time within a second of the
  rider ahead (any rider on track, lapped or not) is a traffic lap.
  - A slow or wide corner while you're right behind someone is called as
    "Held up behind Eifert in turn three", not a mistake.
  - The line call says "Stuck behind Eifert most of that lap" (then "Still
    behind Eifert, four tenths, you're closing").
  - Corner comparisons and the ghost lap use your best **clean-air** lap,
    so you're not compared with a lap you spent in someone's wake (if every
    lap was in traffic, the best lap is used).
  - The editor's lap table and `racebox-overlay race` mark each lap as clean
    air or traffic. How close counts as traffic is a slider in the Crew chief
    tab (default 1 second).

How it works: Speedhive records when every rider crosses the timing line.
Your own crossings are the same moments as your RaceBox laps, so the race
lines itself up with your data automatically (the editor shows how well
they matched). Positions at the line are exact. Between crossings, each
rider's progress is estimated from their lap time and the shape of your lap,
and yours comes from your RaceBox, so a pass shows up about where it
happened instead of at the next line. It's an estimate: a pass made and
undone within one lap can't be seen. The results CSV isn't enough (it only
has final positions), so give the session link; it's downloaded once and
saved next to the video as `VIDEO.race.json`.

To try it without a race of your own: `python tools/make_race_sample.py sample/`
makes a RaceBox session with the lap times of #96 in Speedhive session
12935135.

The RaceBox's first lap in a race is the warm-up lap and the wait on the
grid, so in race mode the lap timer starts at the race start: that lap isn't
timed, counted, used as a best or called by the crew chief (laps are numbered
from the start, like Speedhive's). The editor's Sync tab still lists it, for
lining up.

On the map, every rider who's on track has a tag at their spot on the track.
When riders are nose to tail and their tags overlap, the one ahead in the
race is on top (your own marker is always on top). While the field is
lined up on the grid before the start, everyone is shown in grid order around
your spot (rows of two). A rider in the standings with no tag on the map says
why in place of the gap: **NO TIME** (Speedhive has no lap times for them;
they still get a tag, drawn a second ahead of the timed rider behind them in
the running order, since that's the best estimate there is),
**PIT** (they started late from the pits and aren't out yet), **OUT**
(retired), or **FIN** (finished).

Passes in the standings (and the crew chief's pass calls) are only ones the
timing confirms: two riders swap places only if they cross the next line the
other way round. When they do, the swap is shown where the estimate of their
positions puts it, mid-lap, rather than waiting for the line.

Riders in the official results with no lap times (Speedhive sometimes has
none for a rider whose transponder didn't read, even the winner) are still
in the standings, kept at their official finishing place for the whole race
with no gap shown; they can't be put on the map. The Race tab's summary
lists them.

### Qualifying and heat results at the start

A race day runs qualifying, then heat races, then the main. In the Race tab,
under **Earlier sessions**, choose which session came before this video's
under **Session** (you have to pick one), and paste its Speedhive link under
**Results**:

- **Qualifying**: this video is the heat race. The qualifying results show at
  the start.
- **Heat race**: this video is the main. The heat results show at the start.

Switching the session keeps the link; **Clear** removes it.

The results show big over the picture, in the current theme, for the first
10 seconds of the rendered video (from the render's start, so a trimmed
render shows them at its own start), fading in and out. Every rider in the
session is listed (non-finishers at the end, marked DNF / DNS), with all
their results:

- qualifying: position, race number, rider, class (with its colour, and
  their place in class when there's more than one), laps, best lap and the
  gap to pole;
- heat: the same, plus the finishing time, with the gap to the winner (or
  laps down), and **GRID**: where each rider started the heat, with the
  places they gained (green, up) or lost (red, down). The grid comes from
  Speedhive's lap chart; it's left out if Speedhive doesn't have one.

All classes in the session are listed together, in overall finishing order.

A big field gets tighter rows so everyone fits. Your row is highlighted (from
your rider number in race mode). The panels on the picture
step aside while the results show; panels in the frame's strips stay. Leave
both empty to skip it. This works whether or not race mode is on for this
video. Results are downloaded once and kept next to the video.

The earlier session also sets the starting grid in race mode: a main's grid
is the heat's finishing order (all classes together), a heat's is the
qualifying order. That's what the standings show before the start, what
"up / down from the grid" counts from, and (for a heat, when qualifying is
given too) the GRID column on the heat's results. Without it the grid comes
from Speedhive's lap chart, whose "start positions" are really the order the
field first crossed the line after the start, not the grid.

From the command line: `--qual-source LINK` or `--heat-source LINK`.

## Saved layouts

Once a video looks the way you like, keep that look for other videos: at the
top of **Look & layout**, type a name under **Saved layouts** ("Race day",
"Practice") and click **Save current**. Saving under an existing name
updates it. To use one on another video, pick it and click **Use**.

A saved layout keeps the theme, units, overall size and speedometer top
speed; where every panel goes, how big it is, which are shown, what the lap
timer shows and how the map is turned; the frame round the video (strip
sizes, alignment and colour); and the event name and logos. It doesn't keep
anything that belongs to one video: sync, trim, ghost, race, the track name
or the date.

Tick **Start new videos with "…"** and every video you open for the first
time starts from that layout (videos you've already set up keep their own
settings). Layouts are saved in `~/.racebox-overlay/layouts/`.

From the command line: `--preset "Race day"` renders with a saved layout on
top of the video's own settings.

## Throttle & brake

The RaceBox doesn't know your throttle or brake lever, but it measures how
hard the bike speeds up and slows down, and that's what this gauge shows:

- green, filling up from the middle: accelerating, labelled THROTTLE;
- red, filling down: braking (BRAKE, from 0.18 G of slowing);
- in between, grey: COAST (rolling off, engine braking, holding speed).

The bar's full scale is set from the session (at least 0.6 G each way), with
a tick every 0.5 G. The figure comes from two sources blended together: GPS
speed gives the true level but blurs it over about a second; the RaceBox's
accelerometer gives the quick changes but is noisy and thrown off by the
bike pitching under braking. The gauge takes the level from GPS and the
detail from the accelerometer (or GPS alone if the file has no accelerometer
column). It's its own panel ("Throttle & brake"), so it can be turned off,
sized and placed like the others; `--hide accel` turns it off from the
command line.

## The frame round the video

Every render has a frame round the picture: strips on the left, the right,
above and underneath, drawn as one piece in the theme's style so they read as a single
border whatever size each strip is. They share one fill and texture (carbon
weave, broadcast pinstripes, a HUD grid, arcade scanlines, vintage leather
grain), one outline round the outside, and a trim hugging the picture, whose
corners are shaped to match (rounded, cut, stepped or square). The
output frame grows to make room (the video itself isn't stretched or cropped):

- each side strip is the width that would make an upright video 4:5 (a
  1530x2752 GO 3S clip gets 672 pixels a side) or a wide one 21:9 (1080p gets
  600 pixels);
- the top and bottom strips are slim bars across the full width, 12% of the
  video's longer side (230 pixels for 1080p, 330 for a 1530x2752 upright
  clip), sized for a row of small panels like speed, the lap timer and lean.
  The side strips sit between them, beside the picture.

The strips are always on; make each one wider or narrower with its slider
(10-200%) under **Frame round the video** in **Look & layout**. **Colour**
there picks the frame's colour (the theme's trim, texture and outline stay,
and adapt to a light or dark colour); **Theme colour** goes back to the
theme's own. From the command line: `--frame-color "#0b2545"`. A bigger strip
makes the output bigger and its full-size panels with it (a tall panel like
the race panel is limited by the strip's height). From the command line the
sizes come from the project file (`"strip_size": {"left": 1, "right": 1.4,
"top": 0.6, "bottom": 0.8}`).

Put any panel in a strip: choose it under the panel in the Panels list ("On
the video", "Left strip", ...), or drag the panel into the strip on the
preview. Drag along a strip to change the order, or back onto the video to
take it out. Panels in a side strip stack top to bottom, each as wide as the
strip; panels in the top or bottom strip sit side by side, each as tall as
it, as a row centred over or under the video (not the whole frame); all of a strip's panels
shrink together if they don't fit. Each strip also says where its panels
sit: a side strip's stack at the **Top**, **Center** or **Bottom**; the top or
bottom strip's row at the **Left** or **Right** end of the frame, or
**Center** (centred over or under the video). A panel in a strip always
fills it (the strip's own slider sets the size), so it has no size slider or
corner handle of its own. Unless you
move them, the race panel starts in the right strip, the logos at either end
of the bottom strip and the event name, track name and date in a row in the
top strip.

From the command line: `--place race=right,speed=bottom,gforce=bottom`
(`video` puts a panel back on the picture).

## Event and track

At the bottom of **Look & layout**, under **Event & track**:

- **Event name** (who's running the event), **Track name** and **Date &
  time**: type the text. Each is its own small card in the theme, with a label
  (EVENT, TRACK, DATE); in a strip they line up at the same width and text
  size. The track name starts as the track in your RaceBox file, and the date
  as when the session started (in your computer's time zone); **Reset** goes
  back to those. Empty a box to leave that panel off.
- **Event logo** and **Track logo**: **Choose…** an image. A PNG with a clear
  background looks best; empty margins round it are trimmed off. Logos are
  shown as they are, without a card.

Each one appears in the Panels list, where you can turn it off, size it, and
put it in a strip or on the video like any other panel.

From the command line: `--event-name "SFL MiniGP Round 6" --track-name "..."
--date "..." --event-logo logo.png --track-logo track.png` (`''` turns one
off; `--place event_logo=left,date=bottom` moves them).

## Portrait (upright) video

A helmet camera like the GO 3S records upright: a tall, narrow picture.
For those videos the overlay switches to a portrait layout that keeps the
middle of the frame, the track ahead, clear:

- lap timer and track map across the top, in the sky;
- the race standings down the left and the ghost down the right, just under
  them;
- speed, throttle & brake and lean side by side at the bottom like a dash, a little larger,
  over the tank and hands rather than the road.

The ghost window keeps the ghost video's own shape (tall for an upright
video), and its label wraps onto two lines when the window is narrow. Any
panel can still be dragged, resized or hidden in the editor.

## Themes

Colour themes: **carbon** (dark cards, default), **daylight** (light cards
for bright, washed-out footage), **neon** (outlined, cyan), and **minimal**
(no cards, outlined text).

Styled themes change the shapes too, on every panel (lap timer, map and
marker, speed, lean or G, ghost window, race tower) and the crew chief's
radio sound:

| Theme | Look | Speed / lean | Map marker | Race tower | Radio |
|---|---|---|---|---|---|
| **broadcast** (TV Broadcast) | slanted navy cards with a red stripe, condensed type | big number with a slanted bar; centre-zero lean bar | red badge with your race number | position boxes, class colour stripes, red header | clean digital link with chirps |
| **hud** (Fighter HUD) | no cards, green lines in corner brackets, monospaced | scrolling speed tape; artificial horizon with roll scale | target diamond with heading line | bracketed row, P09-style positions | clipped military radio, roger beep |
| **arcade** (Retro Arcade) | pixel font, magenta borders, scanlines | LED bar graphs | pixel-art bike | 1ST/2ND hi-score table, your row blinks after a pass | 8-bit, with blips |
| **vintage** (Vintage Gauges) | cream faces in brass bezels, serif type, paper grain | needle dials with an odometer window; inclinometer | enamel badge | pit-board numbers, your row in yellow | crackly old two-way |

Pick one in the editor or with `--theme hud`. The styled themes use fonts
that come with macOS (Futura Condensed, Menlo, Rockwell) and fall back to
similar ones elsewhere; the arcade font is built in.

Make your own as a JSON file. Start from a built-in and change only what you
want; the full list of keys is in `rbo/themes.py`:

```json
{ "base": "carbon", "accent": [0, 160, 255], "bg": [0, 20, 40, 190], "radius": 6,
  "gauge": "dial", "marker": "target", "radio": "digital" }
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
| `--ghost` / `--no-ghost` | picture-in-picture of your best lap |
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
- `rbo/ghost.py` — matches each moment of a lap to the same point on your
  best lap and streams that lap's frames for the picture-in-picture.
- `rbo/crewchief.py` — finds corners on your best lap, compares each lap with
  it (time, line, braking point, corner speed), turns findings into calls,
  speaks them, adds the radio sound and schedules them so none overlap.
- `rbo/race.py` — race mode: downloads Speedhive timing, lines it up with
  your RaceBox laps, works out the running order and the passes, and writes
  the race calls. The order is the timing screen's (laps completed, then who
  crossed the line first); a pass between two riders is only shown if the
  line confirms it (they cross the next line the other way round), and then
  at the moment the mid-lap estimate puts it, so a guess about where someone
  is on track never shows a pass that didn't happen.
- `rbo/themes.py`, `rbo/project.py` — themes, and the per-video settings file.
- `rbo/video.py` — ffmpeg crops the panels back out, overlays them, and
  encodes with VideoToolbox, copying the original audio.

### Render speed

A render runs three things at once: Python drawing the moving panels (on
every CPU core but one), ffmpeg decoding the camera video, and the encoder.
What keeps it quick:

- The camera's video is decoded on the Mac's media engine.
- The Mac's H.264 hardware encoder only goes up to 4096x2304. An upright
  GO 3S clip in the frame is bigger than that (about 2874x3412), and above
  that size macOS quietly switches to a slow software H.264 encoder. So a
  frame that size is encoded as HEVC on the hardware instead (the terminal
  says so); it plays on Macs, iPhones and YouTube. `--quality best` still
  uses the software encoder you asked for.
- The themed frame is turned into video once and the camera picture laid
  into it, instead of blending a full-size see-through frame over every
  picture.
- Panels that never change (event, track and date names, logos) are drawn
  once; only the moving panels are redrawn and shrunk each frame.

The terminal (and the editor's render bar) show frames per second while it
runs.

## Ideas for next steps

- Lap comparison (ghost of best lap on the map, delta graph).
- Join split recordings (long sessions spanning several video files).
- Live crew chief calls in your helmet while riding (needs a live Bluetooth
  link to the RaceBox and a phone app).
