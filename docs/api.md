# Local helper service API (v1)

A FastAPI app bound to `127.0.0.1:8765`. It stores tracking files on disk and records them in SQLite (`~/.tbh/tbh.sqlite3`; tracking files in `~/.tbh/tracks/`). Override the location with `TBH_HOME`.

CORS: allow origins `chrome-extension://*` and `https://www.youtube.com`. All bodies are JSON.

## Endpoints

### `GET /v1/health`
`{"ok": true, "version": "0.1.0", "detector": "tracknet-v2"}`

### `GET /v1/videos/{video_id}`
The video's record, or `404 {"detail": "unknown video"}`.
```json
{
  "video_id": "YTkyRTsiIaY",
  "url": "https://www.youtube.com/watch?v=YTkyRTsiIaY",
  "status": "ready",
  "segments": [{"start": 359.0, "end": 372.0}],
  "track_url": "/v1/tracks/YTkyRTsiIaY",
  "updated_at": "2026-09-17T09:00:00Z",
  "job": null
}
```
`status` is one of `ready | queued | processing | failed`. While a job is running, `job` is the job object (see below).

### `GET /v1/tracks/{video_id}`
The `track.json` document (see `track-format.md`), or 404. Served with gzip compression.

### `GET /v1/videos`
A list of video records (without the track data), newest first.

### `POST /v1/jobs`
```json
{ "url": "https://www.youtube.com/watch?v=YTkyRTsiIaY", "start": 359.0, "end": 372.0 }
```
- `start` and `end` are optional; if both are missing, the whole video is processed.
- Returns `202` with the job object.
- If a job for the same video and range is already queued or processing, that job is returned instead of starting a new one.
- If the video already has a track, the new track is merged into it: segments are combined and rows in overlapping ranges are replaced.

### `GET /v1/jobs/{job_id}`
```json
{
  "job_id": "7f3c…",
  "video_id": "YTkyRTsiIaY",
  "status": "processing",
  "stage": "detect",
  "progress": 0.42,
  "message": "frame 270/650",
  "error": null,
  "created_at": "…",
  "updated_at": "…"
}
```
- `stage` is one of `queued | download | decode | detect | track | finalize | done`.
- `status` is one of `queued | processing | ready | failed`.

### `DELETE /v1/videos/{video_id}`
Removes the record and its tracking file.

## Pipeline entry point used by the service

```python
from tbh.pipeline.api import process_video

track: dict = process_video(
    url: str,
    start: float | None,
    end: float | None,
    work_dir: pathlib.Path,         # temporary; deleted afterwards unless keep_media
    progress: Callable[[str, float, str], None],  # (stage, 0..1, message)
    keep_media: bool = False,
)
```

`process_video` downloads only the requested range, decodes it, detects and tracks the ball, computes ring colours, and returns the track dict. It deletes all downloaded media unless `keep_media` is set.

Jobs run one at a time in a background worker thread, because the GPU is shared.
