# Chrome extension

The extension (Manifest V3, plain JavaScript, no build step) draws the ball ring over YouTube's player. It uses tracking files served by the local helper service (`docs/api.md`) and follows the rendering rules in `docs/track-format.md`.

## Install (load unpacked)

1. Start the helper service in the repo: `uv run tbh-service run`. It listens on `127.0.0.1:8765`.
2. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and choose the `extension/` folder.
3. Open a YouTube watch page, for example `https://www.youtube.com/watch?v=YTkyRTsiIaY&t=359`.
4. Reload any YouTube tabs that were open before installing. Content scripts only attach to new page loads.

Chrome 116 or later is required. Branded Chrome ignores the `--load-extension` command-line flag since version 137, but **Load unpacked** in the UI still works. Automated runs use Chrome for Testing (see "Browser checks" below).

To regenerate the icons: `uv run python extension/tools/make_icons.py`.

## How it works

```
youtube.com tab                          extension                          local
┌──────────────────────────┐   runtime   ┌─────────────────────┐   fetch   ┌──────────────────┐
│ content.js (loader)      │  messages   │ background.js       │ ────────▶ │ tbh-service      │
│  └ content-main.js       │ ──────────▶ │  (service worker,   │           │ 127.0.0.1:8765   │
│     └ app.js ─ overlay.js│ ◀────────── │   client.js)        │ ◀──────── │                  │
│              └ prompt.js │             └─────────────────────┘           └──────────────────┘
└──────────────────────────┘                    ▲ popup.html/js (settings, status)
```

| File | Role |
|---|---|
| `src/content.js` | Classic content script. It runs `import(chrome.runtime.getURL("src/content-main.js"))`, because MV3 content scripts can't be modules. The imported modules are listed in `web_accessible_resources`. |
| `src/content-main.js` | YouTube glue: finds `#movie_player` / `video.html5-main-video`, listens for `yt-navigate-finish`, `yt-page-data-updated` and `popstate`, and watches settings and theater/miniplayer attribute changes. It also answers popup messages (`tbh:getState`, `tbh:openPrompt`, `tbh:recheck`). |
| `src/app.js` | Per-video lifecycle, independent of the environment. It looks up the service, loads and caches tracks, and runs the create-tracking job and its polling. The dev page-sim runs it too. |
| `src/overlay.js` | Canvas, layout, frame sync and drawing. |
| `src/track.js` | Parses the schema into typed arrays, binary search, and interpolation per the spec. Pure. |
| `src/geometry.js` | Content-rect letterbox math, ring radius and width, and m:ss parsing. Pure. |
| `src/prompt.js` | The in-player create-tracking UI, in a shadow root. |
| `src/background.js` / `src/client.js` | Message router and HTTP client for the service. The client never rejects: results are `{ok, data}` or `{ok:false, error: unreachable \| timeout \| http \| bad-json}`. |
| `src/popup.*` | Service status, the current video's status, and settings. |
| `src/settings.js` | Defaults, clamping, and `chrome.storage.sync` access. |

### Networking

Content scripts never fetch `localhost`. Every service call is a `chrome.runtime.sendMessage` to the service worker, which fetches with `host_permissions` for `http://127.0.0.1:8765/*`. This avoids mixed-content and Private Network Access blocks on youtube.com.

Parsed tracks are cached in memory per tab (in the content script) and dropped when a job for that video finishes. They are not written to `chrome.storage.local`.

### Per-video flow

1. The video ID comes from `location` (only `/watch?v=`). A change of ID resets all state: polling stops, the prompt hides and the overlay's track is cleared. Stale async results are dropped using a generation counter.
2. The content script calls `GET /v1/videos/{id}`:
   - **`ready` with a track:** it calls `GET /v1/tracks/{id}`, parses the track and draws.
   - **`queued` / `processing`:** it shows progress and polls the job. If the video already has a track (a merge job), that track keeps drawing meanwhile.
   - **`failed` with no track:** it shows the error with **Try again**.
   - **404:** it offers to create tracking, unless the user chose **Don't ask again** for this video (stored in `chrome.storage.local` under `dismissed`, capped at 500 IDs) or turned off **Offer tracking on untracked videos**.
   - **Unreachable:** nothing appears in the page. The popup explains how to start the service, and the page retries quietly every 60 s.
3. **Create tracking** sends `POST /v1/jobs`, with `{url}` or `{url, start, end}`. It then polls `GET /v1/jobs/{id}` every second and shows the stage and percent. On `ready`, it re-fetches the track and starts drawing. On `failed`, it shows the error.

A 1 s tick also re-checks the URL and whether the `<video>` element was replaced, and rebinds if needed.

### Overlay

- **Canvas:** one `<canvas>` (`pointer-events: none`) inserted next to the `<video>`. It is sized and positioned to the video's content rect: the element box minus `object-fit: contain` letterboxing, read from the computed style, with `cover` and `fill` also handled. The backing store is scaled by `devicePixelRatio`.
- **Layout updates:** a ResizeObserver on the video and its parent, plus a MutationObserver on the video's `style`/`class` and the player's `class`. Also window `resize`, `fullscreenchange`, video `resize`/`loadedmetadata`, theater and miniplayer attribute changes, and a 400 ms safety re-check while drawing.
- **Sync:** `requestVideoFrameCallback`, using `metadata.mediaTime`. While paused, redraws (after a seek or resize) use the PTS of the frame on screen, which can be up to a frame before `currentTime`. The fallback, when rVFC is missing, is `requestAnimationFrame` plus `currentTime`. A watchdog re-arms rVFC if frames play but no callback arrives for 1 s.
- **Sampling:** `floorIndex` is a binary search with a sequential hint, so it is O(1) during normal playback. Interpolation follows the spec exactly:
  - Rows no more than `2.5/fps` apart are interpolated.
  - Otherwise the nearest row is used if it is within `1/fps`; if not, nothing is drawn.
  - `ring` and `flags` come from the nearest row.
  - Nothing is drawn outside segments or below the confidence threshold.
  - `conf` is interpolated like `x`, `y` and `r`.
- **Ring:** one stroke with `width = max((ratio − 1)·r_px, minStroke)` and `radius = r_px + width/2`. That gives 1.1 r / 0.2 r by default, and keeps the inner edge on the ball when the width is clamped. There is no fill, glow or trail. Per-frame state uses preallocated objects.
- **Hidden** while `#movie_player` has `ad-showing`, while `ytd-app[miniplayer-is-active]` is set (or the player is inside `ytd-miniplayer`), in Picture-in-Picture, and when the layout is unknown.

### In-page prompt

- **Placement:** top-right of the player (lower in fullscreen), in a shadow root, clear of the control bar. It is hidden during ads.
- **Behaviour:**
  - It opens expanded and collapses to a 40 px ring icon after 6 s without hover or focus. The icon fades with the player controls (`ytp-autohide`) and shows a progress arc while a job runs.
  - Key and click events are stopped at the shadow root, so typing `k`, `f` or digits in the time fields doesn't trigger YouTube hotkeys.
  - `Esc` collapses the card and focuses the icon. The *Whole video* / *Time range* chips are a radio group that responds to arrow keys.
- **Time range:** defaults to the current time ± 30 s, clamped to the video, and editable as `m:ss` or `h:mm:ss`. Until the user edits it, it refreshes on expand, hover and focus, and after an ad ends. **Around now** resets it.
- **Buttons:** **Don't ask again** remembers the dismissal for this video. **Close** on the error card and the × button don't. The popup's **Create tracking** or **Track another part** reopens the prompt and clears the dismissal.

## Settings (popup, `chrome.storage.sync` key `settings`)

| Setting | Default | Range |
|---|---|---|
| Show the ring (`enabled`) | on | |
| Ring size (`ratio`, outer edge / ball radius) | 1.2 | 1.1 to 1.5 |
| Minimum ring width (`minStroke`, CSS px) | 1.5 | 0.5 to 4 |
| Hide below confidence (`minConf`) | 0.5 | 0 to 1 |
| Offer tracking on untracked videos (`autoOffer`) | on | |
| Show debug info on the video (`debug`) | off | |

Debug mode prints the following in the top-left of the video:

- track time, media time and the sync source;
- the sampling mode and the row indices and times;
- `x`, `y`, `r`, `conf`, `ring` and `flags`.

It also draws low-confidence rows as a dashed white ring.

Changes apply live to all tabs. The action badge shows `on` / `off` when a track is loaded, `NN%` while a job runs and `!` after a failure.

## Development

### Unit tests

```
node --test "extension/test/*.test.js"      # Node 22+; Node 25 needs the glob, not a bare directory
```

These cover:

- schema parsing (reordered `fields`, validation, sorting);
- `floorIndex` against a linear scan, including random arrays and hints;
- segment bounds;
- interpolation (the gap threshold of exactly `2.5/fps`, nearest within `1/fps`, outside segments, the confidence threshold, a different fps);
- letterbox and pillarbox math;
- ring radius and width clamping;
- m:ss parsing, the default range, video-ID parsing, settings clamping;
- the HTTP client (errors, timeout, request bodies).

`extension/package.json` exists only to mark the sources as ES modules for Node. Chrome ignores it.

### Overlay harness

```
python3 extension/dev/serve.py        # static server with HTTP Range support (needed for seeking), serves the repo root on :8000
open http://127.0.0.1:8000/extension/dev/harness.html
```

The harness runs the real `overlay.js` over a local clip.

**Query parameters:**

- `video`, `track`, `offset` (track time = clip time + offset);
- `box` = `16x9` | `4x3` (letterbox) | `21x9` (pillarbox) | `tall`;
- `debug=1`, `fallback=1` (forces the rAF path), `t` (start time in the clip), `rate`;
- `preset` = `synthetic` | `real`.

**Presets:**

- `?preset=synthetic` (the default) plays `extension/dev/synthetic/synthetic.mp4` with its track (offset 100). Regenerate them with `uv run python extension/dev/make_synthetic.py`. The mp4 is git-ignored.
- `?preset=real` plays `data/clips/YTkyRTsiIaY_359-372.mp4` with `samples/YTkyRTsiIaY.track.json`, offset 359.

The page's player box is resizable (drag the corner). Controls: step one frame with `,` / `.`, simulate an ad, and adjust the settings live.

**Synthetic data:** the clip has a 0.04 s interpolation gap, a 0.5 s hole with no rows, a 0.5 s low-confidence stretch, and a white band where the ring colour flips to `#101010`.

**Ball error readout:** for the synthetic clip, the harness finds the yellow ball in each presented frame and compares it with the ring centre. Frames drawn from the nearest row are counted separately, because by rule they show a position up to `1/fps` old.

### Page sim and mock service

```
python3 extension/dev/mock_service.py --port 8766 --job-seconds 6
open "http://127.0.0.1:8000/extension/dev/sim.html?v=SYNTHETIC00"
```

**`sim.html`:** a fake YouTube watch page (`ytd-app`, `#movie_player`, a control bar) that runs `app.js` with a direct CORS client (`?api=`, default `http://127.0.0.1:8766`). Its buttons:

- navigate between IDs, the way YouTube's SPA does;
- replace the `<video>` element;
- toggle an ad, the miniplayer, theater mode and control autohide;
- forget dismissals.

It also lists any key presses that reached the page.

**`mock_service.py`** (stdlib only) implements `health`, `videos`, `tracks`, `jobs` (create, poll, dedupe), `DELETE` and CORS:

- Jobs step through the stages on a timer.
- A finished job's track comes from a source file cut to the requested range: `SYNTHETIC00` uses the synthetic track and `YTkyRTsiIaY` uses `samples/…`. Otherwise it builds a synthetic circular path. Results merge like the real service.
- `FAILFAILFAI` fails halfway.
- `--preload ID=PATH` serves a track as ready.

To try the extension itself without the pipeline:

```
python3 extension/dev/mock_service.py --preload YTkyRTsiIaY=samples/YTkyRTsiIaY.track.json   # on :8765
```

### Browser checks (headless)

```
python3 extension/dev/serve.py &
python3 extension/dev/mock_service.py --port 8766 --job-seconds 6 &
python3 extension/dev/mock_service.py --preload YTkyRTsiIaY=samples/YTkyRTsiIaY.track.json &   # :8765, for the YouTube part
node extension/dev/e2e.mjs            # or: node extension/dev/e2e.mjs harness sim extension
```

`e2e.mjs` drives Chrome for Testing over CDP (`cdp.mjs`, no dependencies). It finds the Playwright-cached binary, or you can set `CHROME=`. Screenshots are written to `data/scratch/extension-shots/`.

| Suite | What it checks |
|---|---|
| **harness** | Ring-to-ball error while playing (16:9 and letterboxed 4:3). Paused seeks to times between frames. Each rendering rule. Ring geometry. Ad hiding. Resize tracking. The rAF fallback. |
| **sim** | The offer's default range. Placement clear of the controls. Hidden during ads. Hotkey isolation. Validation. Esc and focus. Dismissal persistence. Auto-collapse. The full create → progress → ready → drawing flow. Rebinding after `<video>` replacement. Miniplayer and theater mode. SPA reset. The failure UI. |
| **extension** | Loads the unpacked extension. The service worker runs. The popup renders both service states. Settings round-trip. The message router. On real youtube.com: the content script reports state, the ring pixels are drawn on the player's content rect, and SPA navigation to an untracked video shows the prompt. |

The CDP helper can also evaluate code in the content script's isolated world. The content script exposes `globalThis.__tbh`, the app, in that world only, for debugging.

## Known limitations

- **YouTube DOM:** the extension depends on `#movie_player`, `video.html5-main-video`, the `ad-showing`, `ytp-autohide` and `ytp-fullscreen` classes, `ytd-app[miniplayer-is-active]`, `ytd-watch-flexy[theater]` and the `yt-navigate-finish` event. If YouTube renames these, the overlay may misalign or stop.
  - The 1 s tick and the 400 ms layout re-check limit the damage from missed events. They don't fix renames.
  - Shorts, embeds (`youtube-nocookie.com`, `/embed/`) and `m.youtube.com` are not supported.
- **Ads:** the overlay and prompt hide while `ad-showing` is set. A mid-roll that YouTube doesn't flag this way would get a ring from the wrong timeline.
- **Miniplayer and Picture-in-Picture:** the overlay is hidden in both. Alignment in the miniplayer's scaled, animated container isn't reliable, and a PiP window can't be drawn on.
- **Fullscreen:** supported, because the canvas lives inside the player. Canvas size follows `devicePixelRatio` changes on the next layout check.
- **Paused frames:** the paused redraw uses the last presented frame's PTS when it is within 0.1 s of `currentTime`. Right after a seek, the ring can be one frame ahead for a moment, until rVFC reports the new frame.
- **Nearest-row rule:** at the edge of a detection gap, the spec's "nearest row within 1/fps" draws a position up to 1/fps old. The synthetic clip moves at about 600 px/s, which shows up as a 13 px lag at 640 px width. This is by design.
- **Large tracks:** a whole-match track is sent as one message from the service worker. Messages are limited to 64 MiB, which is roughly 1M rows. Tracks are cached only in memory.
- **Service worker restarts:** the per-tab badge state is kept in memory and resets if Chrome stops the worker. Content scripts re-report on their next state change.
- **Headless YouTube playback:** in the automated check, YouTube sometimes loads but never buffers past a seek. The check therefore verifies the paused-frame ring at the seek target, not continuous playback.
