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
| finalize | `refine.py`, `ringcolor.py`, `trackfile.py`, `api.py` | **Radius smoothing:** a robust per-shot model `r = a + b*y` (depth follows the image row for a fixed broadcast camera, and b >= 0). The measurement/model ratio is median-filtered and Gaussian-filtered (sigma 6 frames), clamped to 0.75-1.3, and r is clamped to 0.12-0.65% of width. **Ring colour:** exactly the spec rule (median of the 1.3r-2.5r annulus in linear RGB, max-WCAG candidate, magenta only at 1.2x, 15%/3-frame hysteresis). Hysteresis resets at cuts and at track breaks longer than 8 frames. **Confidence:** the detection score smoothed along the piece, times fit quality, boosted when the full-res blob confirms a ball, and decaying across gaps. Rows below 0.35 are dropped; renderers hide rows below 0.5 by default. |

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
| ring colour | `#101010` on every row (see the note above) |
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
| full run: download 5.8 s, decode 1.7 s, detect 25.3 s, track and refine 4.1 s | **36.9 s for the 13 s clip** (0.35x real time) |
| worst case seen under heavy contention (load average about 17) | 0.3 frames/s for detection |

Previews (local only): `data/previews/YTkyRTsiIaY_preview.mp4`, `..._side_by_side.mp4`,
`..._contact.png`.

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
- **Motion blur.** r follows the streak's half-width, so on long streaks the ring
  (radius 1.1r) overlaps the streak's ends. The spec asks for a circle; a capsule
  would fit streaks better.
- **Bounce/hit flags** use a conservative heuristic tuned on one clip. They have
  few false labels but miss about half of the events.
- **Gap recovery** (classical search) rarely finds anything reliable on this clip
  (1-2 frames). Looser settings mostly found player limbs, lines and banner text.
- **Rendering-rule edge cases** (reported to the extension side):
  - *"Nearest row within 1/fps".* If the renderer samples at the exact frame pts and
    that frame has no row, the previous frame's row is exactly 1/fps away. An
    inclusive comparison then draws the ring one frame late (up to about 40 px behind a
    fast ball). `preview.py` uses a strict comparison (< 1/fps - 1 ms).
  - *Interpolating at arbitrary `currentTime`.* Interpolating mid-frame puts the ring
    ahead of the displayed frame. Sampling at the frame's `mediaTime`
    (`requestVideoFrameCallback`) avoids this.
- **Throughput** depends heavily on machine load (see the metrics). Detection dominates.
- yt-dlp warns that no JavaScript runtime (deno) is installed. Downloads still work
  today, but YouTube extraction may need one in the future.
