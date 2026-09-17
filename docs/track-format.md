# Tracking file format (`track.json`), schema version 1

The tracking file is the only thing that crosses from processing (`tbh.pipeline`)
to drawing (the Chrome extension). No video pixels are ever stored.

## Example

```json
{
  "schema_version": 1,
  "video_id": "YTkyRTsiIaY",
  "source_url": "https://www.youtube.com/watch?v=YTkyRTsiIaY",
  "created_at": "2026-09-17T09:00:00Z",
  "generator": { "name": "tbh-pipeline", "version": "0.1.0", "detector": "tracknet-v2" },
  "video": { "width": 1280, "height": 720, "fps": 50.0 },
  "segments": [ { "start": 359.0, "end": 372.0 } ],
  "fields": ["t", "x", "y", "r", "conf", "ring", "flags"],
  "frames": [
    [359.020, 0.51234, 0.43120, 0.00410, 0.93, "#101010", 0],
    [359.040, 0.51502, 0.42710, 0.00410, 0.88, "#101010", 1]
  ]
}
```

## Fields

- `video.width`, `video.height`: size in pixels of the frames that were analysed. Normalised coordinates don't depend on it; it's there for debugging.
- `video.fps`: analysis frame rate after deinterlacing, which may be double the source rate.
- `segments`: time ranges (seconds of the YouTube video) that were processed.
  - Inside a segment, a time with no row means "no confident ball; draw nothing".
  - Outside every segment, there's simply no data.
- `fields` / `frames`: a compact table with one row per analysed frame where the ball is shown. Rows are sorted by `t`.

| field | type | meaning |
|---|---|---|
| `t` | float, seconds | Presentation time **in the original YouTube video's timeline** (not the clip's). Compare directly with `video.currentTime`. |
| `x`, `y` | float 0..1 | Ball centre as a fraction of the video's **content** width and height (excluding any letterbox bars the player adds). |
| `r` | float | Ball radius as a fraction of content **width**. |
| `conf` | float 0..1 | Confidence after smoothing. Renderers may hide rows below a threshold (default 0.5). |
| `ring` | `"#rrggbb"` | Ring colour chosen by the pipeline to contrast with the background immediately around the ball in this frame. |
| `flags` | int bitmask | `1` = interpolated (not directly detected), `2` = bounce, `4` = hit. |
| `sl` | float, optional | Motion-blur **half-length**: the distance from the streak's centre to the centre of each end cap, as a fraction of content **width**. `0` means a sharp, round ball. Present only if listed in `fields`. |
| `sa` | float, optional | Streak direction in radians, in image coordinates (x right, y down). Ignored when `sl` is 0. |

`fields` may contain extra columns in any order; readers must look columns up by name and ignore ones they don't know. Files without `sl`/`sa` are valid and render as plain circles.

## Rendering rules (the overlay spec)

Drawing a frame at time `t`:

1. Find the rows on either side of `t` and linearly interpolate `x`, `y`, `r` and `sl` between them if they are no more than `2.5 / fps` seconds apart. Otherwise use the nearest row only if it's within `0.5 / fps` (the same frame), or draw nothing. Take `ring` and `sa` from the nearest row. Sample `t` at the displayed frame's time (`requestVideoFrameCallback` `mediaTime`), not at an arbitrary `currentTime`.
2. Convert to screen coordinates using the rendered content rectangle of the `<video>` element (accounting for `object-fit: contain` letterboxing).
3. Draw a ring **around** the ball without covering it. For a blurred ball the "ball" is the whole streak, a stadium (pill) shape: a rectangle of length `2·sl_px` and width `2·r_px` along direction `sa`, with semicircular caps of radius `r_px`. The ring is the same shape, offset outward; with `sl = 0` it's the circle described below. The ball keeps its original colour.
   - Inner edge = ball edge: `r_px`.
   - Outer edge = `1.2 × r_px` (the ring's outer diameter is 20% larger than the ball's).
   - Draw it as one circle stroke: radius `1.1 × r_px`, width `0.2 × r_px`, colour `ring`.
   - Because the stroke is so thin, clamp it to a minimum of `1.5` CSS pixels. When clamped, keep the inner edge at `r_px` and let the outer edge grow.
   - The ratio (`1.2`) and minimum stroke are user settings.
4. Draw the stadium only when `sl_px ≥ 0.5 · r_px`; otherwise draw a circle (tiny streaks aren't worth the shape change). Users can force "circle only" in settings. In that mode, draw the circle at the streak centre with radius `r_px` as before. It may then overlap the streak, which is the reason stadium is the default.
5. Don't draw a fill, glow or trail by default.

## Ring colour rule (pipeline)

For each frame, sample the pixels in an annulus from `1.3 r` to `2.5 r` around the ball centre. The ball itself is excluded.

1. Compute the median colour of the annulus in linear RGB, then its relative luminance `L`.
2. Choose a candidate colour with maximum WCAG contrast ratio against that background. The candidates are:
   - near-black `#101010`;
   - near-white `#f5f5f5`;
   - saturated magenta `#ff00ff`, a colour that doesn't occur on courts (grass, clay, hard court, crowd) and keeps the ring distinct from a yellow ball.

   Prefer black or white unless magenta gives at least 1.2× more contrast. In practice magenta never wins, because black or white always has a contrast of at least about 4.6. It's kept only for future rule changes.
3. Apply hysteresis: switch colour only if the new candidate's contrast beats the current one by at least 15% for 3 consecutive frames. This stops flicker along background edges.
