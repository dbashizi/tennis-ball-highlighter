# Ball-tracking pipeline (`tbh.pipeline`)

The pipeline turns a YouTube time range into a `track.json` (see `track-format.md`).
It works offline, can use future frames, and stores positions only.

## Running it

```bash
uv sync
# YouTube URL: only the requested range is downloaded. Media is deleted afterwards unless --keep-media.
uv run tbh process "https://www.youtube.com/watch?v=YTkyRTsiIaY" --start 5:59 --end 6:12 \
    --out samples/YTkyRTsiIaY.track.json --keep-media --work-dir data/clips

# Local file: --time-offset is the source time of the file's first frame.
uv run tbh process data/clips/YTkyRTsiIaY_359-372.mp4 --time-offset 359 --start 5:59 --end 6:12 \
    --out /tmp/t.json --debug-out data/previews/cands.json

# Local verification tools (they never go into the extension)
uv run tbh preview data/clips/YTkyRTsiIaY_359-372.mp4 samples/YTkyRTsiIaY.track.json --out data/previews/p.mp4
uv run tbh preview ... --side-by-side          # original | overlay, with a 5x zoom inset of the ball
uv run tbh preview ... --circle-only           # the user setting: circle at the streak centre
uv run tbh preview ... --debug data/previews/cands.json --conf 0   # raw detections + per-frame labels
uv run tbh contact data/clips/YTkyRTsiIaY_359-372.mp4 samples/YTkyRTsiIaY.track.json --out sheet.png
uv run tbh validate samples/YTkyRTsiIaY.track.json
uv run tbh merge old.json new.json --out merged.json

uv run pytest          # unit tests plus synthetic end-to-end tests (no network)
```

Times can be given as seconds, `m:ss` or `h:mm:ss`.

Python API (used by the service), as in `docs/api.md`:

```python
from tbh.pipeline.api import process_video
track = process_video(url, start, end, work_dir, progress, keep_media=False,
                      # extras: time_offset= (local files), detector="auto|tracknet|classical",
                      # device=, video_id=, source_url=, verify_offset=True, recover_gaps=True
                      )
```

`progress(stage, fraction, message)` is called with the stages `download`, `decode`,
`detect`, `track`, `finalize` and `done`. `track` covers linking and the
full-resolution refinement.

## How it works

| step | module | what |
|---|---|---|
| download | `download.py` | Runs yt-dlp with `download_ranges` and `force_keyframes_at_cuts`. It picks the best H.264 stream up to 1080p and re-encodes the cut at CRF 14, because the default CRF 23 visibly smears the ball. It then **verifies the start time** (see below). |
| decode | `video.py` | Uses ffprobe for size, fps and packet timestamps, and ffmpeg `idet` for interlacing. Interlaced sources are deinterlaced with `bwdif=mode=send_field`, which doubles the rate (analysis fps = 2x, pts + half a frame). Explicit stream field order wins for parity. Frames are streamed through a pipe, so memory is O(1) in the clip length. |
| detect | `detect.py`, `models/tracknet.py` | Runs TrackNet on 640x360 (3 stacked BGR frames: t, t-1, t-2) on MPS in batches of 4, falling back to CPU. It reads the **soft** heatmap `1 - p(background)` rather than the argmax, which gives about 15% more detections on weak balls. It is computed sparsely because a dense 256-way logsumexp is 3x slower than the network. Peaks come from connected components, at most 4 candidates per frame. Camera cuts are detected with an HSV-histogram Bhattacharyya distance plus a mean grey difference. Near a cut or at the clip start, a frame takes its context from the next frames. |
| track | `track.py` | Removes static false positives (a spot visited at least 3 separate times). Builds tracklets under constant-velocity prediction with a physical speed limit (7% of width per frame at 25 fps). Accepts tracklets greedily with overlap trimming. Runs a two-sided consistency test for outliers (worst first). Splits into pieces at kinks with a quadratic fit per piece. Fills gaps of up to 8 frames with flag 1, choosing a model per gap: cubic Hermite if both sides agree, a **V (kink) model** if the two flight lines meet inside the gap (for gaps of at most 6 frames), or a one-sided quadratic extrapolation if the kink is at the gap's edge. Otherwise the gap is left empty; it never guesses across hidden bounces or hits. An interpolated frame snaps to a weak TrackNet candidate lying within 1.5% of width. Bounce (2) and hit (4) labels come from a conservative velocity heuristic. |
| refine | `refine.py`, `radius.py`, `recover.py` | Runs a full-resolution pass with a +-12 frame buffer. Each point is measured against a **background median** of neighbouring frames, choosing the frames whose surroundings best match (players and shadows move). The blob gives the **centre** (diff-weighted centroid) and the **radius**: `2*sqrt(lambda_min)` for round blobs and `sqrt(3*lambda_min)` for streaks. That is the half-width *across* the streak, never its length. A trajectory-guided classical search in gaps (the hybrid step) adds only isolated, ball-shaped, lighter-than-background blobs. |
| streak | `radius.py`, `refine.py` | **Motion blur (`sl`, `sa`).** A looser mask (30% of the blob's peak difference) keeps the faint tail. Along and across the principal axis, the half-extents come from 1.5/98.5 percentiles, and `sl = along - across`. Blur widens both equally, so this is the centre-to-cap-centre distance; the centre moves to the streak's midpoint. `sl` is smoothed with a weighted mean over +-2 frames of the same piece. Neighbours are rescaled to the row's speed, and an exposure model `sl = k * abs(v)` (weight 0.3) is included, with k fitted per clip (0.29 here, i.e. the shutter is open about 58% of the frame). Rows without a measurement (interpolated ones included) use `k * abs(v)` from the final positions. `sl` is capped at `0.75*abs(v) + 0.5*r` and 4% of width. `sa` averages measured directions and the velocity direction as doubled-angle vectors (removing the pi ambiguity), then flips to point along the motion. At bounce/hit frames without a direct measurement, `sl = 0`: the exposure straddles the contact, so the streak is a V, and a circle is less wrong than a misoriented pill. A measurement whose midpoint is away from the track point is still accepted if the track point lies on the measured streak. |
| finalize | `refine.py`, `ringcolor.py`, `trackfile.py`, `api.py` | **Radius smoothing:** a robust per-shot model `r = a + b*y` (depth follows the image row for a fixed broadcast camera, and b >= 0). The measurement/model ratio is median-filtered and Gaussian-filtered (sigma 6 frames), clamped to 0.75-1.3, and r is clamped to 0.12-0.65% of width. **Ring colour:** the spec rule (median in linear RGB, max-WCAG candidate, magenta only at 1.2x, 15%/3-frame hysteresis). The annulus is taken **around the stadium outline**: pixels 1.3r-2.5r from the streak's centre segment, so the streak itself is never sampled. It is computed in a second full-res pass, once the final centre, r, sl and sa are known. Hysteresis resets at cuts and at track breaks longer than 8 frames. **Confidence:** the detection score smoothed along the piece, times fit quality, boosted when the full-res blob confirms a ball, and decaying across gaps. Rows below 0.35 are dropped; renderers hide rows below 0.5 by default. |

### Output columns

The pipeline writes `fields = [t, x, y, r, conf, ring, flags, sl, sa]`. `trackfile.validate`/`merge`
look every column up by name, accept files without `sl`/`sa` (read as 0) and ignore unknown
columns. `merge` remaps old rows to the new file's columns, filling `sl = sa = 0`.
(`src/tbh/service/merge.py` does the same; the coordinator updated it.)

### Preview drawing

`preview.py` follows the overlay spec. It samples at each frame's pts:
- It interpolates x, y, r and sl between rows at most 2.5/fps apart. Otherwise it uses the nearest row within 0.5/fps. `ring` and `sa` come from the nearest row.
- The ring is drawn by exact sub-pixel coverage of `r <= dist(pixel, streak segment) <= r + stroke`, with `stroke = max(0.2 r, 1.5 CSS px)`. So the inner edge is always on the ball or streak outline.
- The stadium is used only when `sl_px >= 0.5 r_px`; otherwise, or with `--circle-only`, it draws a circle.
- The contact sheet shows three panels per cell: original, circle ring (old), stadium ring (new).

### TrackNet's lead along the motion

On this footage TrackNet marks the **leading end** of a motion streak (where the
ball is at the end of the exposure), not its middle. The refine pass measures
the real streak centre and fits the lead per clip: 0.21 frames of motion on
the test clip, about 3-8 px for fast balls. That shift is applied to every
position that has no blob measurement of its own.

### Ring colour note

`#101010` and `#f5f5f5` bracket magenta's luminance, so under a
pure-luminance WCAG contrast one of them always beats magenta by far more than
1.2x. **Magenta is never chosen.** This is implemented as specified and tested
with fake luminances. On this clip every row is `#101010`: the green court
(L ~ 0.17-0.20) is exactly where black and white contrast are equal (about 4.2
each), so the hysteresis keeps the first choice, and the red surround and grey
banners favour black.

## Detector source and licence

- **Weights:** `tracknet_yastrebksv.pt` (42.9 MB, sha256 `c735bc1a...6653076`) from the unofficial
  PyTorch TrackNet by yastrebksv (https://github.com/yastrebksv/TrackNet; Google Drive id
  `1XEYZ4myUN7QT-NeBYJI0xteLsvs-ZAOl`). It was trained on the TrackNet tennis dataset (10 broadcast
  matches, 1280x720). It is downloaded on first use with gdown into `data/weights/` (or
  `$TBH_WEIGHTS_DIR`, else `~/.tbh/weights`) and never committed.
- **Licence:** the repository declares **no licence** (GitHub API `license: null`, and no LICENSE
  file in TrackNet or TennisProject, checked 2026-09-17). So no code was copied.
  `models/tracknet.py` was written from the paper (Huang et al., *TrackNet*, AVSS 2019,
  arXiv:1907.03698), with parameter names chosen to match the checkpoint. The weights
  carry no licence either: treat them as personal and research use only, and don't
  redistribute them.
- **Fallback:** if the weights can't be downloaded, `detector="auto"` falls back to
  `ClassicalDetector` (symmetric 3-frame difference, yellow/white prior, small-blob
  filter, optional ROI). It is what the offline end-to-end tests use. It is far
  weaker than TrackNet on real broadcast footage and was not tuned on the real clip.

## Timing verification (test clip)

- The source is YouTube format 137: 1920x1080 H.264, **25 fps, progressive**. ffprobe
  reports `field_order=progressive`, and `idet` on the clip gives 303 progressive,
  0 TFF, 0 BFF and 22 undetermined frames. So there is no deinterlacing and the
  analysis runs at 25 fps.
- The first frame of the cut is at pts 0. Two checks confirm it is source time **359.000**:
  1. The clip's first 10 frames were matched against a download padded by 5 s (354-377)
     at offset 125 frames = 5.000 s (MSE 0.31, next best 4.2). The last frames match
     at 12.6 s, which is consistent.
  2. The clip's first frames were matched against the stream read with `ffmpeg -copyts`
     (original timestamps): best pts 359.000 (MSE 0.26, next best 4.2).
- Check 2 now runs automatically on every URL download (`download.verify_clip_offset`)
  and is recorded in `generator.source.timing_check`. The final run reported
  `verified: true, offset 359.0, delta 0.000, mse_best 0.17, mse_next 4.16`.
- Rows' `t` = offset + packet pts, so t is 359.000, 359.040, ... in the original timeline.

## Results on the test clip (5:59-6:12)

These figures are from the committed `samples/YTkyRTsiIaY.track.json`, a real URL run on an M4 Max over MPS.

| metric | value |
|---|---|
| frames analysed | 325 (13.00 s, 1920x1080, 25p) |
| frames with a row | 260 (**80.0%**) |
| rows with conf >= 0.5 (drawn by default) | 254 (**78.2%**) |
| interpolated rows (flag 1) | 23 (**7.1%** of frames) |
| rows with a full-res radius/centre measurement | 241 |
| radius | 2.9-3.5 px (0.15-0.18% of width; ball diameter about 0.33% of width) |
| ring colour | `#101010` on every row (see the note above; unchanged with the stadium annulus) |
| rows drawn as a stadium (`sl >= 0.5 r`) | 233 of 260 |
| streak half-length `sl` (median) | 5.9 px; the median streak length/width `(sl + r)/r` is 2.9, up to about 6 |
| exposure constant k (`sl ~ k*abs(v)`) | 0.29 |
| bounces / hits labelled | 4 / 4 (the labels I checked by eye matched the footage; several events are missed) |
| static false-positive candidates removed | 7 (a fixed bright spot in the stands near (1710,150)) |
| outliers rejected | 4 |
| timing check | verified, offset 359.000 s |

**Manual spot check (30 frames, every 11th frame from 3 to 322, zoomed crops judged by eye):**

| outcome | frames | count |
|---|---|---|
| ring on the ball (centre within about 3 px) | 3, 14, 25, 36, 47, 58, 69, 80, 102, 113, 124, 135, 157, 179, 212, 223, 234, 245, 256, 267, 278 | 21 |
| ring 5-8 px off (it overlaps the ball's edge) | 190 (the bounce frame), 201 (interpolated just after a hit) | 2 |
| ring shown, ball not visible to confirm | 146 (on the court near the net), 322 (in front of a banner) | 2 |
| no ring, ball not visible (occluded or off-court) | 91, 168, 300, 311 | 4 |
| missed: ball visible, no ring | 289 (the ball flying past the far player after the winner) | 1 |

So 21 of the 25 drawn rings were clearly right, 2 were slightly off, and 2 were unverifiable;
of the 24 frames where the ball was visible, 23 had a ring (96%). No ring was seen on a player,
line, logo or shoe in the spot check, the contact sheet or the frame-by-frame review of the gap
regions. The earlier false positives (the banner logo, a player's shoe at frame 257, the
ball-boy area at 310) are all rejected.

**Speed** (325 frames, detection dominates):

| | |
|---|---|
| TrackNet alone, quiet machine | 27-33 frames/s (batch 4, fp32, MPS; fp16 was slower) |
| detection stage in the final run (with decode and peaks; other agents were using the GPU and Chrome) | 13 frames/s |
| full run (with streaks): download 5.7 s, decode 1.8 s, detect 19.1 s (17.5 fps), track and refine and ring pass 5.2 s | **31.8 s for the 13 s clip** (0.41x real time) |
| worst case seen under heavy contention (load average about 17) | 0.3 frames/s for detection |

Previews (local only): `data/previews/YTkyRTsiIaY_preview.mp4` (stadium), `..._preview_circle.mp4`
(`--circle-only`), `..._side_by_side.mp4`, `..._contact.png` (original | circle | stadium).

**Stadium ring, before and after (visual check of about 30 zoomed frames):**
- Before, the circle sat on the middle of almost every streak.
- After, on 26 of 27 checked streak frames, the pill surrounds the visible streak with the correct orientation and a thin stroke. Sharp or round balls (for example 359.76 s, 360.48 s, 367.52 s) keep a plain circle.
- The exception was the bounce frame 190: before the fix, the pill was nearly horizontal while the streak was diagonal. It now falls back to a circle.
- Interpolated rows (136, 201, 258) get pills along the flight. Where the interpolated position is a few px off, the pill sits beside the streak (136).

## Known limitations

- **Occlusion and contact.** Around hits the ball is often hidden by the player or
  merges with the racket or skin. TrackNet misses it, and the pipeline deliberately
  doesn't guess across gaps that hide a bounce or hit (such as frames 60-67 and 91-98 on
  the clip). Expect the ring to disappear for 0.2-0.4 s around many hits.
- **Weak detections near the far player** (small ball over clay and banners) are often
  below the tracker's thresholds, for example frames 99-100 and 289.
- **Interpolated rows** are accurate in smooth flight (within 3 px in tests). Near
  kinks they can be 5-10 px off (1-3 ball radii); confidence is lowered there.
- **Radius.** This footage is upscaled SD. The ball is about 6 px wide at 1080p (r ~
  0.16% of width), below the usual 0.4-1.2% diameter range for HD broadcasts, so the
  lower clamp is set at 0.12%. The r(y) depth model assumes a fixed camera; with
  cuts it is refitted per shot, but zooms or pans inside a shot are not modelled.
- **Motion blur.** Handled with the stadium ring (see above). Before, the circle ring
  (radius about 1.1r at the streak centre) sat on top of the streak and hid its middle.
  Now the pill encloses the visible streak (a synthetic test checks that every pixel above
  half contrast lies inside the ring's inner edge). The ends are the faint part: the loose
  mask is at 30% of peak, so the faintest tail tips (under about 15% of the exposure) can
  poke out by a pixel or two. The sharper Hermite/V fills also make interpolated rows' pills
  follow the flight. Their position error (5-10 px near kinks) is more visible as a pill
  than it was as a circle.
- **Contact frames.** At a bounce or hit the real streak is V-shaped. A straight pill can't
  enclose it; without a direct measurement those rows fall back to a circle.
- **Bounce/hit flags** use a conservative heuristic tuned on one clip. They have
  few false labels but miss about half of the events.
- **Gap recovery** (classical search) rarely finds anything reliable on this clip
  (1-2 frames). Looser settings mostly found player limbs, lines and banner text.
- **Rendering rules.** The spec now uses "nearest row within 0.5/fps" and samples at
  `requestVideoFrameCallback` `mediaTime`. That resolves the one-frame-late ring and the
  mid-frame lead reported earlier.
- **Throughput** depends heavily on machine load (see the metrics). Detection dominates.
- yt-dlp warns that no JavaScript runtime (deno) is installed. Downloads still work
  today, but YouTube extraction may need one in the future.
