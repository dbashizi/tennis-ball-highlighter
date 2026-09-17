# Local helper service

`tbh.service` is the small HTTP service the Chrome extension talks to. It queues
processing jobs, runs `tbh.pipeline.api.process_video` one job at a time, and
serves the resulting tracking files. The HTTP contract is in [api.md](api.md).

## Running it

```bash
uv run tbh-service run                  # http://127.0.0.1:8765
uv run tbh-service run --port 9000      # another port
```

It binds to `127.0.0.1` only by default. It also rejects requests whose `Host`
header isn't `127.0.0.1`, `localhost` or `[::1]` (or the `--host` you passed).
This guards against DNS-rebinding attacks from web pages.

Check it's up:

```bash
curl http://127.0.0.1:8765/v1/health
curl -H 'content-type: application/json' \
     -d '{"url": "https://youtu.be/YTkyRTsiIaY", "start": 359, "end": 372}' \
     http://127.0.0.1:8765/v1/jobs
```

The service starts even if the pipeline can't be imported (for example, a
missing model or a broken torch install). In that case each job fails and its
`error` field explains why.

## Where data lives

Everything lives under `$TBH_HOME` (default `~/.tbh`):

| path | contents |
|---|---|
| `tbh.sqlite3` (+ `-wal`, `-shm`) | SQLite in WAL mode: `videos` and `jobs` tables. The schema version is kept in `PRAGMA user_version` and migrations run on startup. |
| `tracks/<video_id>.json` | Tracking files (schema v1, see [track-format.md](track-format.md)). |

- **Media:** jobs work in a fresh system temp directory (`tbh-job-*`), which is deleted when the job ends. No video is kept.
- **Removing everything:** stop the service and delete `$TBH_HOME`.

### Job behaviour

- **Order:** jobs run one at a time, in a single background worker.
- **Duplicates:** posting the same video and range while a job for it is queued or processing returns the existing job.
- **Merging:** a new track for a video that already has one is merged into it. Segments are combined, and old rows inside the new segments are replaced.
- **Restarts:** jobs that were `processing` when the service stopped are marked `failed` with the error `interrupted`. Jobs that were still `queued` are picked up again.
- **Deleting a video:** its queued or running jobs are cancelled and marked `failed` with the error `cancelled`.

### Local files (testing only)

With `TBH_ALLOW_LOCAL_FILES=1`, `POST /v1/jobs` also accepts an absolute path or
a `file://` URL to an existing file. Its video id is `local-` followed by the
first 11 hex characters of the SHA-1 of the resolved path.

## CLI

```bash
tbh-service run [--host 127.0.0.1] [--port 8765]
tbh-service import track.json [--url URL] [--replace]
tbh-service list
tbh-service rm <video_id>
```

- `import` validates a schema-v1 tracking file and registers it as `ready`.
  - **Video id:** taken from the file, or from `--url` if given.
  - **Existing track:** by default the file is merged into it. `--replace` overwrites it instead.
- `list` prints one line per video: id, status, last update, segments and URL.
- `rm` deletes the record and its tracking file.

The CLI and a running service can use the same `$TBH_HOME` at the same time, because SQLite runs in WAL mode.

## Auto-start on macOS (optional)

Save the following as `~/Library/LaunchAgents/com.tbh.service.plist`. Adjust
the paths (`which uv` shows where uv is), then load it with
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tbh.service.plist`.
Unload it with `launchctl bootout gui/$(id -u)/com.tbh.service`.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.tbh.service</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/uv</string>
    <string>run</string>
    <string>--project</string>
    <string>/Users/YOU/path/to/tennis-ball-highlighter</string>
    <string>tbh-service</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/Users/YOU/.tbh/service.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/YOU/.tbh/service.log</string>
</dict>
</plist>
```

## Development

```bash
uv run pytest tests/test_service_*.py
```

The tests use a temporary `TBH_HOME` and replace the pipeline with a fake
`process_video` (by monkeypatching `tbh.service.jobs.load_process_video`).
