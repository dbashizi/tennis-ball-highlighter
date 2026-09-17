# Local helper service API (v1)

A FastAPI app bound to `127.0.0.1:8765`. It stores tracking files on disk and records them in SQLite (`~/.tbh/tbh.sqlite3`; tracking files in `~/.tbh/tracks/`). Override the location with `TBH_HOME`.

CORS: allow origins `chrome-extension://*` and `https://www.youtube.com`. Preflights that carry `Access-Control-Request-Private-Network` get `Access-Control-Allow-Private-Network: true`. All bodies are JSON (`Content-Type: application/json`).

Requests whose `Host` header isn't `127.0.0.1`, `localhost` or `[::1]` are rejected with `400`, to guard against DNS rebinding. Timestamps are ISO 8601 UTC with milliseconds, for example `2026-09-17T09:00:00.000Z`.

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
  "created_at": "2026-09-17T08:58:00.000Z",
  "updated_at": "2026-09-17T09:00:00.000Z",
  "job": null
}
```
- `status` is one of `ready | queued | processing | failed`.
  - A queued or running job takes precedence; otherwise the status is `ready` whenever a track exists, even if a later job failed.
- `track_url` is `null` until a track exists.
- `job`:
  - while a job is queued or running, the job object (see below);
  - when the status is `failed`, the failed job, so its `error` can be shown;
  - otherwise `null`.

### `GET /v1/tracks/{video_id}`
The `track.json` document (see `track-format.md`), or 404. Served with gzip compression.

### `GET /v1/videos`
A list of video records (without the track data), newest first.

### `POST /v1/jobs`
```json
{ "url": "https://www.youtube.com/watch?v=YTkyRTsiIaY", "start": 359.0, "end": 372.0 }
```
- `start` and `end` are optional; if both are missing, the whole video is processed.
  - If only one is given, the range is open on the other side.
  - `start` must be `>= 0`, and `end > start`.
- The URL is normalised to a video id. Accepted forms: `watch?v=`, `youtu.be/`, `/shorts/`, `/live/`, `/embed/`, `m.youtube.com`, and extra parameters such as `t=`.
  - Anything else gets `422 {"detail": "..."}`.
- Returns `202` with the job object.
- If a job for the same video and range is already queued or processing, that job is returned instead of starting a new one.
- If the video already has a track, the new track is merged into it: segments are combined and rows in overlapping ranges are replaced.

### `GET /v1/jobs/{job_id}`
```json
{
  "job_id": "7f3c…",
  "video_id": "YTkyRTsiIaY",
  "url": "https://www.youtube.com/watch?v=YTkyRTsiIaY",
  "status": "processing",
  "stage": "detect",
  "progress": 0.42,
  "message": "frame 270/650",
  "error": null,
  "start": 359.0,
  "end": 372.0,
  "created_at": "…",
  "updated_at": "…"
}
```
- `stage` is one of `queued | download | decode | detect | track | finalize | done`.
- `status` is one of `queued | processing | ready | failed`.
- `error` is set when `status` is `failed`. After a restart, a job that was running has the error `"interrupted"`; a job whose video was deleted has `"cancelled"`.
- `url` (the canonical URL), `start` and `end` echo the request.
- Unknown job: `404 {"detail": "unknown job"}`.

### `DELETE /v1/videos/{video_id}`
Removes the record and its tracking file, and cancels its queued or running jobs. Returns `200 {"deleted": "<video_id>"}`, or `404 {"detail": "unknown video"}`.

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
