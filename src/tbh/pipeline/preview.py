"""LOCAL verification tool: burn the overlay into a copy of the clip.

It follows the rendering rules in docs/track-format.md:

1. For time t, linearly interpolate x, y and r between the rows on either side
   if they are at most 2.5/fps apart. Otherwise use the nearest row within
   1/fps, or draw nothing. ``ring`` comes from the nearest row.
2. Draw one ring. Its inner edge is at r_px and its outer edge at ratio*r_px
   (ratio 1.2), so the stroke is centred at (1+ratio)/2*r_px with width
   (ratio-1)*r_px. The stroke has a minimum of 1.5 CSS px. When clamped, the
   inner edge stays at r_px and the outer edge grows.
   CSS px are converted to video px assuming the player shows the video
   ``display_width`` CSS px wide (default 1280).
3. No fill, glow or trail.

The ring is rasterised with exact anti-aliased annulus coverage, so sub-pixel
radii and stroke widths are honoured. cv2.circle only supports integer thickness.
"""

from __future__ import annotations

import bisect
import subprocess
from pathlib import Path

import cv2
import numpy as np

from .trackfile import FLAG_BOUNCE, FLAG_HIT, FLAG_INTERPOLATED
from .video import ffmpeg_bin, iter_frames, open_video


def hex_to_bgr(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16)


class TrackSampler:
    """Rendering rule 1 (the same lookup the extension performs)."""

    def __init__(self, track: dict, conf_threshold: float = 0.5) -> None:
        f = track["fields"]
        self.ix = {k: f.index(k) for k in f}
        self.rows = [r for r in track["frames"] if r[self.ix["conf"]] >= conf_threshold]
        self.ts = [r[self.ix["t"]] for r in self.rows]
        self.fps = float(track["video"]["fps"])

    def at(self, t: float) -> dict | None:
        if not self.rows:
            return None
        i = bisect.bisect_left(self.ts, t)
        before = self.rows[i - 1] if i > 0 else None
        after = self.rows[i] if i < len(self.rows) else None
        # An exact hit counts as "after" (t_after == t).
        ix = self.ix
        cands = [r for r in (before, after) if r is not None]
        nearest = min(cands, key=lambda r: abs(r[ix["t"]] - t))
        if before is not None and after is not None and after[ix["t"]] - before[ix["t"]] <= 2.5 / self.fps + 1e-9:
            span = after[ix["t"]] - before[ix["t"]]
            w = 0.0 if span <= 0 else (t - before[ix["t"]]) / span
            vals = {k: before[ix[k]] + w * (after[ix[k]] - before[ix[k]]) for k in ("x", "y", "r")}
        elif abs(nearest[ix["t"]] - t) < 1.0 / self.fps - 1e-3:
            # "Within 1/fps" is taken as strictly less than one frame (1 ms slack for
            # rounding). Otherwise a missing row would repeat the previous frame's
            # position one frame late.
            vals = {k: nearest[ix[k]] for k in ("x", "y", "r")}
        else:
            return None
        vals["ring"] = nearest[ix["ring"]]
        vals["conf"] = nearest[ix["conf"]]
        vals["flags"] = nearest[ix["flags"]]
        vals["row_t"] = nearest[ix["t"]]
        return vals


def ring_geometry(r_px: float, css_to_px: float, ratio: float = 1.2, min_stroke_css: float = 1.5):
    """Return (inner, outer) radii in video px, per rule 3 (ring around the ball)."""
    inner = r_px
    width = max((ratio - 1.0) * r_px, min_stroke_css * css_to_px)
    return inner, inner + width


def draw_ring(img: np.ndarray, cx: float, cy: float, inner: float, outer: float,
              color_bgr: tuple[int, int, int]) -> None:
    """Anti-aliased annulus [inner, outer] with exact per-pixel coverage (4x4 supersampling)."""
    h, w = img.shape[:2]
    x0, x1 = int(np.floor(cx - outer - 1)), int(np.ceil(cx + outer + 1))
    y0, y1 = int(np.floor(cy - outer - 1)), int(np.ceil(cy + outer + 1))
    x0c, y0c, x1c, y1c = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1c <= x0c or y1c <= y0c:
        return
    ss = 4
    offs = (np.arange(ss) + 0.5) / ss
    ys = (np.arange(y0c, y1c)[:, None] + offs[None, :]).reshape(-1)
    xs = (np.arange(x0c, x1c)[:, None] + offs[None, :]).reshape(-1)
    d = np.hypot(xs[None, :] - cx, ys[:, None] - cy)
    inside = ((d >= inner) & (d <= outer)).astype(np.float32)
    cov = inside.reshape(y1c - y0c, ss, x1c - x0c, ss).mean(axis=(1, 3))[..., None]
    roi = img[y0c:y1c, x0c:x1c].astype(np.float32)
    col = np.array(color_bgr, np.float32)[None, None, :]
    img[y0c:y1c, x0c:x1c] = (roi * (1 - cov) + col * cov + 0.5).astype(np.uint8)


def _zoom_inset(canvas: np.ndarray, src: np.ndarray, cx: float, cy: float, box: int = 60, zoom: int = 5,
                corner: str = "tr") -> None:
    h, w = src.shape[:2]
    x0 = int(np.clip(round(cx) - box // 2, 0, w - box))
    y0 = int(np.clip(round(cy) - box // 2, 0, h - box))
    crop = src[y0:y0 + box, x0:x0 + box]
    big = cv2.resize(crop, None, fx=zoom, fy=zoom, interpolation=cv2.INTER_NEAREST)
    bh, bw = big.shape[:2]
    X = canvas.shape[1] - bw - 10 if corner == "tr" else 10
    Y = 10
    canvas[Y:Y + bh, X:X + bw] = big
    cv2.rectangle(canvas, (X - 1, Y - 1), (X + bw, Y + bh), (255, 255, 255), 1)


def render_preview(media: str | Path, track: dict, out: str | Path, *, side_by_side: bool = False,
                   debug: dict | None = None, display_width: float = 1280.0,
                   time_offset: float | None = None, inset: bool | None = None,
                   conf_threshold: float = 0.5, ratio: float = 1.2, min_stroke_css: float = 1.5,
                   crf: int = 18) -> dict:
    """Write a preview video. ``debug`` = {"candidates": {abs_frame: [[x,y,s],...]}, "raw": {...}}.

    Returns simple stats (frames written, frames with a ring).
    """
    info = open_video(media)
    if time_offset is None:
        time_offset = (track.get("generator", {}).get("source", {}) or {}).get("time_offset")
    if time_offset is None:
        time_offset = track["segments"][0]["start"] if track.get("segments") else 0.0
    sampler = TrackSampler(track, conf_threshold)
    W, H = info.width, info.height
    css_to_px = W / float(display_width)
    if inset is None:
        inset = side_by_side
    ow = W * 2 if side_by_side else W
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{ow}x{H}", "-r", f"{info.analysis_fps:g}", "-i", "-", "-c:v", "libx264", "-preset", "medium",
           "-crf", str(crf), "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    n = drawn = 0
    cand = (debug or {}).get("candidates", {})
    try:
        for idx, frame in iter_frames(info):
            t = time_offset + float(info.pts[idx])
            orig = frame.copy()
            img = orig.copy()
            s = sampler.at(t)
            if s is not None:
                cx, cy, r = s["x"] * W, s["y"] * H, s["r"] * W
                inner, outer = ring_geometry(r, css_to_px, ratio, min_stroke_css)
                draw_ring(img, cx, cy, inner, outer, hex_to_bgr(s["ring"]))
                drawn += 1
            if debug is not None:
                for c in cand.get(idx, cand.get(str(idx), [])):
                    x, y, sc = c
                    col = (255, 255, 0)
                    cv2.drawMarker(img, (int(round(x)), int(round(y)) + 14), col, cv2.MARKER_TRIANGLE_UP, 8, 1)
                    cv2.putText(img, f"{sc:.2f}", (int(x) + 6, int(y) + 26), cv2.FONT_HERSHEY_SIMPLEX, 0.35, col, 1)
                label = f"#{idx} t={t:.3f}"
                if s is not None:
                    fl = s["flags"]
                    tags = ("I" if fl & FLAG_INTERPOLATED else "D") + ("B" if fl & FLAG_BOUNCE else "") + \
                           ("H" if fl & FLAG_HIT else "")
                    label += f" conf={s['conf']:.2f} r={s['r'] * W:.1f}px {tags} {s['ring']}"
                cv2.putText(img, label, (12, H - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4)
                cv2.putText(img, label, (12, H - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 1)
            if inset and s is not None:
                _zoom_inset(img, img, s["x"] * W, s["y"] * H)
                if side_by_side:
                    _zoom_inset(orig, orig, s["x"] * W, s["y"] * H)
            canvas = np.hstack([orig, img]) if side_by_side else img
            proc.stdin.write(np.ascontiguousarray(canvas).tobytes())
            n += 1
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed writing {out}")
    return {"frames": n, "frames_with_ring": drawn, "css_to_px": css_to_px}


def render_contact_sheet(media: str | Path, track: dict, out: str | Path, *, n: int = 36, cols: int = 6,
                         crop: int = 64, zoom: int = 3, display_width: float = 1280.0,
                         time_offset: float | None = None, conf_threshold: float = 0.5) -> dict:
    """Grid of zoomed crops around the ball, spread over the processed range, with the ring drawn.

    Each tile pairs the original crop (left) with the ringed crop (right). Frames
    without a visible ring are shown centred on the last known position and
    labelled "no ring".
    """
    info = open_video(media)
    if time_offset is None:
        time_offset = (track.get("generator", {}).get("source", {}) or {}).get("time_offset")
    if time_offset is None:
        time_offset = track["segments"][0]["start"] if track.get("segments") else 0.0
    sampler = TrackSampler(track, conf_threshold)
    W, H = info.width, info.height
    css_to_px = W / float(display_width)
    total = len(info.pts)
    picks = sorted(set(np.linspace(0, total - 1, n).round().astype(int).tolist()))
    tiles = []
    last = (W / 2, H / 2)
    for idx, frame in iter_frames(info):
        if idx not in picks:
            continue
        t = time_offset + float(info.pts[idx])
        s = sampler.at(t)
        if s is not None:
            last = (s["x"] * W, s["y"] * H)
        cx, cy = last
        x0 = int(np.clip(round(cx) - crop // 2, 0, W - crop))
        y0 = int(np.clip(round(cy) - crop // 2, 0, H - crop))
        orig = frame[y0:y0 + crop, x0:x0 + crop].copy()
        ring = orig.copy()
        if s is not None:
            inner, outer = ring_geometry(s["r"] * W, css_to_px)
            draw_ring(ring, cx - x0, cy - y0, inner, outer, hex_to_bgr(s["ring"]))
        big = [cv2.resize(im, None, fx=zoom, fy=zoom, interpolation=cv2.INTER_NEAREST) for im in (orig, ring)]
        tile = np.hstack([big[0], np.full((crop * zoom, 2, 3), 255, np.uint8), big[1]])
        label = f"{t:.2f}s"
        if s is None:
            label += " no ring"
        else:
            fl = s["flags"]
            label += f" c{s['conf']:.2f}" + (" interp" if fl & FLAG_INTERPOLATED else "") + \
                     (" bounce" if fl & FLAG_BOUNCE else "") + (" hit" if fl & FLAG_HIT else "")
        cv2.putText(tile, label, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 0, 0), 3)
        cv2.putText(tile, label, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1)
        tiles.append(np.pad(tile, ((2, 2), (2, 2), (0, 0))))
    while len(tiles) % cols:
        tiles.append(np.zeros_like(tiles[0]))
    grid = np.vstack([np.hstack(tiles[k:k + cols]) for k in range(0, len(tiles), cols)])
    # Header: a downscaled overview frame with the analysed video id and range.
    head = np.zeros((28, grid.shape[1], 3), np.uint8)
    seg = track["segments"][0] if track.get("segments") else {"start": 0, "end": 0}
    cv2.putText(head, f"{track.get('video_id')}  {seg['start']:.2f}-{seg['end']:.2f}s  "
                f"left: original crop, right: overlay ring (x{zoom})", (8, 19),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), np.vstack([head, grid]))
    return {"tiles": len(picks), "out": str(out)}
