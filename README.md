# Tennis Ball Highlighter

Makes the ball easier to see in old tennis matches on YouTube. A thin ring, in a colour that contrasts with the court, is drawn around the ball. The ball keeps its original colour. When the ball is blurred into a streak, the ring follows the streak as a thin pill shape instead of covering it.

No footage is stored. The video is processed offline into a small **tracking file** (ball positions per frame). A Chrome extension draws the ring over the normal YouTube player from that file.

```
YouTube URL ─▶ tbh-service (localhost) ─▶ pipeline: download range → TrackNet → smoothing → ring colour
                    │  SQLite: video id → tracking file                     (media deleted afterwards)
                    ▼
             Chrome extension ─▶ canvas ring over the YouTube player, synced to playback
```

## Quick start

Requirements:
- macOS on Apple Silicon (MPS) or any machine with PyTorch; CPU works but is slow;
- `uv`;
- `ffmpeg`;
- Chrome 116 or later.

```bash
uv sync
```

```bash
uv run tbh-service run
```

1. Keep the service running (it listens on `127.0.0.1:8765`).
2. In Chrome, open `chrome://extensions`, turn on Developer mode, click **Load unpacked** and choose `extension/`.
3. Open https://www.youtube.com/watch?v=YTkyRTsiIaY&t=359. The track for 5:59–6:12 is already registered, so the ring appears during that rally.
4. On any other video, the extension offers to **create tracking** for the whole video or a time range. The service downloads only that range, processes it (about 2.5× real time on an M4 Max) and then deletes the media.

The first job downloads the TrackNet weights (about 43 MB) into `data/weights/`.

To register the sample track in a fresh setup:

```bash
uv run tbh-service import samples/YTkyRTsiIaY.track.json
```

## Pieces

| Path | What | Docs |
|---|---|---|
| `src/tbh/pipeline/` | Offline tracking: range download with verified time offset, TrackNet detection, outlier rejection, Kalman/RTS smoothing, streak and radius measurement, contrast ring colour; `tbh` CLI | [docs/pipeline.md](docs/pipeline.md) |
| `src/tbh/service/` | Local FastAPI + SQLite service: video lookup, job queue with progress, track storage and merging; `tbh-service` CLI | [docs/service.md](docs/service.md), [docs/api.md](docs/api.md) |
| `extension/` | Chrome MV3 extension: synced overlay, create-tracking prompt, popup settings | [docs/extension.md](docs/extension.md) |
| `docs/track-format.md` | The tracking-file contract and exact rendering rules | |
| `samples/` | Tracking files (positions only) | |

## Useful commands

Process a range and write a track without the service:

```bash
uv run tbh process "https://www.youtube.com/watch?v=YTkyRTsiIaY" --start 5:59 --end 6:12 --out /tmp/track.json --keep-media
```

Render a local preview video with the ring burned in (for checking only):

```bash
uv run tbh preview data/clips/YTkyRTsiIaY_359-372.mp4 samples/YTkyRTsiIaY.track.json --out data/previews/check.mp4
```

Run the Python tests:

```bash
uv run pytest
```

Run the extension unit tests:

```bash
node --test "extension/test/*.test.js"
```

Run the headless browser checks (needs Chrome for Testing; see docs/extension.md):

```bash
node extension/dev/e2e.mjs
```

## Known limitations

- **Hits and bounces:** the ring disappears for about 0.2–0.4 s around hits, where the player hides the ball. Gaps across a hit or bounce aren't guessed.
- **Faint far-court balls:** weak detections near the far player are sometimes dropped.
- **Guessed positions:** interpolated rows can be 5–10 px off near a contact point.
- **Ring colour:** on green or red hard courts it's almost always near-black. Near-white is chosen over bright backgrounds, such as white lines or signage.
- **Half-pixel offset:** the pipeline writes positions as pixel indices, while the renderer treats them as pixel centres. The resulting offset is half a source pixel (≈0.25 CSS px), which isn't visible.

## Notes

- Downloading from YouTube is against its terms of service. This is a personal experiment, and only positions are kept.
- The TrackNet weights (yastrebksv/TrackNet) have no declared licence; treat them as personal/research use. The model code here was written from the paper.
