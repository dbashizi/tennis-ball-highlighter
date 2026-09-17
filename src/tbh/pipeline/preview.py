"""LOCAL verification tool: burn the overlay into a copy of the clip.

It follows the rendering rules in docs/track-format.md:

1. For time t (a frame's pts), linearly interpolate x, y, r and sl between the
   rows on either side if they are at most 2.5/fps apart. Otherwise use the
   nearest row if it is within 0.5/fps (the same frame), or draw nothing.
   ``ring`` and ``sa`` come from the nearest row. Columns are looked up by name;
   files without sl/sa render as circles.
2. Draw one ring around the ball outline. The outline is a stadium (segment
   centre +- sl along sa, thickened by r), or a circle when sl_px < 0.5*r_px or
   in circle-only mode. The inner edge is on the outline (distance r from the
   segment) and the outer edge is at distance ratio*r (ratio 1.2), so the stroke
   width is (ratio-1)*r_px, with a minimum of 1.5 CSS px. When clamped, the
   inner edge stays on the outline and the outer edge grows.
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
from .ringcolor import segment_distance
from .video import ffmpeg_bin, iter_frames, open_video


def hex_to_bgr(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16)


class TrackSampler:
    """Rendering rule 1 (the same lookup the extension performs)."""

    def __init__(self, track: dict, conf_threshold: float = 0.5) -> None:
        f = track["fields"]
        self.ix = {k: f.index(k) for k in f}
        ci = self.ix["conf"]
        self.rows = sorted((r for r in track["frames"] if r[ci] >= conf_threshold), key=lambda r: r[self.ix["t"]])
        self.ts = [r[self.ix["t"]] for r in self.rows]
        self.fps = float(track["video"]["fps"])

    def _get(self, row, name, default=0.0):
        i = self.ix.get(name)
        return row[i] if i is not None else default

    def at(self, t: float) -> dict | None:
        if not self.rows:
            return None
        i = bisect.bisect_left(self.ts, t)
        before = self.rows[i - 1] if i > 0 else None
        after = self.rows[i] if i < len(self.rows) else None
        cands = [r for r in (before, after) if r is not None]
        g = self._get
        nearest = min(cands, key=lambda r: abs(g(r, "t") - t))
        if before is not None and after is not None and g(after, "t") - g(before, "t") <= 2.5 / self.fps + 1e-9:
            span = g(after, "t") - g(before, "t")
            w = 0.0 if span <= 0 else (t - g(before, "t")) / span
            vals = {k: g(before, k) + w * (g(after, k) - g(before, k)) for k in ("x", "y", "r", "sl")}
        elif abs(g(nearest, "t") - t) <= 0.5 / self.fps + 1e-6:
            vals = {k: g(nearest, k) for k in ("x", "y", "r", "sl")}
        else:
            return None
        vals["sa"] = g(nearest, "sa")
        vals["ring"] = g(nearest, "ring")
        vals["conf"] = g(nearest, "conf")
        vals["flags"] = g(nearest, "flags")
        vals["row_t"] = g(nearest, "t")
        return vals


def ring_geometry(r_px: float, css_to_px: float, ratio: float = 1.2, min_stroke_css: float = 1.5):
    """Return (inner, outer) distances from the ball's centre line in video px (rule 3)."""
    inner = r_px
    width = max((ratio - 1.0) * r_px, min_stroke_css * css_to_px)
    return inner, inner + width


def ring_shape(r_px: float, sl_px: float, circle_only: bool = False) -> float:
    """Effective half-length for drawing: 0 (circle) unless it's a real streak (rule 4)."""
    if circle_only or sl_px < 0.5 * r_px:
        return 0.0
    return float(sl_px)


def draw_ring(img: np.ndarray, cx: float, cy: float, inner: float, outer: float,
              color_bgr: tuple[int, int, int], sl: float = 0.0, sa: float = 0.0) -> None:
    """Anti-aliased ring whose inner edge is at distance ``inner`` and outer edge at ``outer``
    from the segment centre +- sl along sa (a stadium; a circle when sl = 0).
    Uses exact per-pixel coverage with 4x4 supersampling."""
    h, w = img.shape[:2]
    ex = abs(sl * np.cos(sa)) + outer + 1
    ey = abs(sl * np.sin(sa)) + outer + 1
    x0, x1 = int(np.floor(cx - ex)), int(np.ceil(cx + ex))
    y0, y1 = int(np.floor(cy - ey)), int(np.ceil(cy + ey))
    x0c, y0c, x1c, y1c = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1c <= x0c or y1c <= y0c:
        return
    ss = 4
    offs = (np.arange(ss) + 0.5) / ss
    ys = (np.arange(y0c, y1c)[:, None] + offs[None, :]).reshape(-1)
    xs = (np.arange(x0c, x1c)[:, None] + offs[None, :]).reshape(-1)
    d = segment_distance(xs[None, :], ys[:, None], cx, cy, sl, sa)
    inside = ((d >= inner) & (d <= outer)).astype(np.float32)
    cov = inside.reshape(y1c - y0c, ss, x1c - x0c, ss).mean(axis=(1, 3))[..., None]
    roi = img[y0c:y1c, x0c:x1c].astype(np.float32)
    col = np.array(color_bgr, np.float32)[None, None, :]
    img[y0c:y1c, x0c:x1c] = (roi * (1 - cov) + col * cov + 0.5).astype(np.uint8)


def draw_sample(img: np.ndarray, s: dict, W: int, H: int, css_to_px: float, circle_only: bool = False,
                ratio: float = 1.2, min_stroke_css: float = 1.5, dx: float = 0.0, dy: float = 0.0) -> None:
    """Draw the ring for one sampled row (``TrackSampler.at``) onto img (offset by dx, dy)."""
    r = s["r"] * W
    sl = ring_shape(r, s["sl"] * W, circle_only)
    inner, outer = ring_geometry(r, css_to_px, ratio, min_stroke_css)
    draw_ring(img, s["x"] * W - dx, s["y"] * H - dy, inner, outer, hex_to_bgr(s["ring"]), sl, s["sa"])


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
                   crf: int = 18, circle_only: bool = False) -> dict:
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
                draw_sample(img, s, W, H, css_to_px, circle_only, ratio, min_stroke_css)
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
                    label += (f" conf={s['conf']:.2f} r={s['r'] * W:.1f}px sl={s['sl'] * W:.1f}px "
                              f"sa={np.degrees(s['sa']):.0f} {tags} {s['ring']}")
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


def render_contact_sheet(media: str | Path, track: dict, out: str | Path, *, n: int = 36, cols: int = 4,
                         crop: int = 64, zoom: int = 3, display_width: float = 1280.0,
                         time_offset: float | None = None, conf_threshold: float = 0.5) -> dict:
    """Grid of zoomed crops around the ball, spread over the processed range, with the ring drawn.

    Each tile shows three panels: the original crop, the circle-only ring, and the stadium ring. Frames
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
        circle = orig.copy()
        stadium = orig.copy()
        if s is not None:
            draw_sample(circle, s, W, H, css_to_px, circle_only=True, dx=x0, dy=y0)
            draw_sample(stadium, s, W, H, css_to_px, circle_only=False, dx=x0, dy=y0)
        big = [cv2.resize(im, None, fx=zoom, fy=zoom, interpolation=cv2.INTER_NEAREST)
               for im in (orig, circle, stadium)]
        sep = np.full((crop * zoom, 2, 3), 255, np.uint8)
        tile = np.hstack([big[0], sep, big[1], sep, big[2]])
        label = f"{t:.2f}s"
        if s is None:
            label += " no ring"
        else:
            label += f" sl/r={s['sl'] / max(s['r'], 1e-9):.1f}"
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
                f"each cell: original | circle ring (old) | stadium ring (new), x{zoom}", (8, 19),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), np.vstack([head, grid]))
    return {"tiles": len(picks), "out": str(out)}
