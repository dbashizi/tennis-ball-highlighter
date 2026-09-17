"""Hybrid gap recovery: a trajectory-guided classical search where TrackNet found nothing.

For frames inside short track gaps, or just past the ends of a run, we predict
where the ball should be. At full resolution we then look for a ball-like blob
in the difference from a background median of neighbouring frames:

* the blob is lighter than the background (balls are yellow or white on the court);
* its minor-axis radius is close to the expected radius;
* its streak length is plausible for the local speed;
* it doesn't touch the search window border.

The best blob per frame becomes an extra candidate with a modest score. The
tracker is then re-run, and its outlier test decides whether the candidate fits.
"""

from __future__ import annotations

from collections import deque

import cv2
import numpy as np

from .radius import crop_window, median_background
from .track import TrackPoint
from .video import VideoInfo, iter_frames


def _shot_of(frame: int, cuts: list[int]) -> int:
    return int(np.searchsorted(np.asarray(sorted(cuts)), frame, side="right"))


def plan_searches(points: list[TrackPoint], cuts: list[int], n_frames: int, fps: float, W: float,
                  max_gap: int = 12, extend: int = 3) -> dict[int, tuple[float, float, float]]:
    """frame -> (pred_x, pred_y, search_radius) for frames without a real detection near the track.

    Only detected points are used as anchors; interpolated frames are searched too.
    """
    by = {p.frame: p for p in points if p.detected}
    frames = sorted(by)
    plan: dict[int, tuple[float, float, float]] = {}
    base = 0.012 * W
    for a, b in zip(frames, frames[1:]):
        gap = b - a - 1
        if gap < 1 or gap > max_gap or _shot_of(a, cuts) != _shot_of(b, cuts):
            continue
        A, B = by[a], by[b]
        step = np.hypot(B.x - A.x, B.y - A.y) / (b - a)
        for f in range(a + 1, b):
            w = (f - a) / (b - a)
            d = min(f - a, b - f)
            # Across a gap the ball may bounce or be hit: allow a generous radius.
            plan[f] = (A.x + w * (B.x - A.x), A.y + w * (B.y - A.y), base + 0.8 * step * d)
    # Run ends: extrapolate with the local velocity.
    for f in frames:
        p = by[f]
        for direction in (1, -1):
            if (f + direction) in by:
                continue
            q = by.get(f - direction)
            if q is None:
                continue
            # Forward velocity (px/frame): q is at frame f - direction.
            vx, vy = (p.x - q.x) * direction, (p.y - q.y) * direction
            sp = np.hypot(vx, vy)
            for k in range(1, extend + 1):
                g = f + direction * k
                if g < 0 or g >= n_frames or g in by or _shot_of(g, cuts) != _shot_of(f, cuts):
                    break
                if g not in plan:
                    plan[g] = (p.x + vx * (g - f), p.y + vy * (g - f), base + 0.5 * sp * k)
    return plan


def find_blob(crop: np.ndarray, bg: np.ndarray, px: float, py: float, radius: float, r_exp: float,
              speed: float) -> tuple[float, float, float] | None:
    """Best ball-like blob in crop near (px, py). Returns (x, y, score) in crop coords."""
    f = crop.astype(np.float32)
    b = bg.astype(np.float32)
    lum = lambda im: 0.114 * im[..., 0] + 0.587 * im[..., 1] + 0.299 * im[..., 2]  # noqa: E731
    dl = cv2.GaussianBlur(lum(f) - lum(b), (0, 0), 0.8)
    dc = cv2.GaussianBlur(np.sqrt(((f - b) ** 2).sum(axis=2)), (0, 0), 0.8)
    noise = float(np.median(np.abs(dc - np.median(dc)))) * 1.4826 + 1.0
    thr = max(8.0, 4.0 * noise)
    mask = ((dc >= thr) & (dl > 2.0)).astype(np.uint8)
    n, lab, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    h, w = mask.shape
    best = None
    for k in range(1, n):
        x, y, bw, bh, area = stats[k]
        if x == 0 or y == 0 or x + bw >= w or y + bh >= h or area < 4:
            continue
        cx, cy = cents[k]
        dist = np.hypot(cx + 0.5 - px, cy + 0.5 - py)
        if dist > radius:
            continue
        ys, xs = np.nonzero(lab == k)
        cov = np.cov(np.stack([xs, ys]).astype(float)) + np.eye(2) / 12.0
        lam = np.sort(np.linalg.eigvalsh(cov))
        r_minor = 2.0 * np.sqrt(max(lam[0], 1e-3))
        r_major = 2.0 * np.sqrt(max(lam[1], 1e-3))
        if not (0.5 * r_exp <= r_minor <= 1.8 * r_exp):
            continue
        if r_major > 2.5 * r_exp + 0.8 * speed:
            continue
        if area > np.pi * (1.8 * r_exp) * (1.8 * r_exp + 0.8 * speed) * 2.0:
            continue
        peak = float(dc[ys, xs].max())
        if peak < 2.0 * thr:
            continue
        # Isolation: a ball flies over a static background. Players, shadows and
        # compression flicker come with other changes nearby.
        iso_r = 3.0 * r_exp + 0.6 * speed
        yy0, yy1 = max(0, int(cy - iso_r * 2)), min(h, int(cy + iso_r * 2) + 1)
        xx0, xx1 = max(0, int(cx - iso_r * 2)), min(w, int(cx + iso_r * 2) + 1)
        loose = (dc[yy0:yy1, xx0:xx1] >= 0.5 * thr).astype(np.uint8)
        _, llab = cv2.connectedComponents(loose, connectivity=8)
        own_ids = set(np.unique(llab[lab[yy0:yy1, xx0:xx1] == k])) - {0}
        own = np.isin(llab, list(own_ids))
        if own.sum() > 4 * area or (loose.astype(bool) & ~own).sum() > 0.5 * area:
            continue
        score = min(1.0, (peak / thr - 1.0) / 2.0) * (1.0 - 0.5 * dist / radius)
        wts = dc[ys, xs]
        mx = float((xs * wts).sum() / wts.sum()) + 0.5
        my = float((ys * wts).sum() / wts.sum()) + 0.5
        if best is None or score > best[2]:
            best = (mx, my, score)
    return best


def recover(info: VideoInfo, points: list[TrackPoint], cuts: list[int], r_of_frame, i0: int, i1: int,
            max_gap: int = 12) -> dict[int, tuple[float, float, float]]:
    """Returns {abs_frame: (x, y, score)} of recovered candidates (analysis-frame px)."""
    W = info.width
    plan = plan_searches(points, cuts, i1, info.analysis_fps, W, max_gap=max_gap)
    plan = {f: v for f, v in plan.items() if i0 <= f < i1}
    if not plan:
        return {}
    by = {p.frame: p for p in points if p.detected}
    lo, hi = max(i0, min(plan) - 12), min(i1, max(plan) + 13)
    out: dict[int, tuple[float, float, float]] = {}
    buf: deque[tuple[int, np.ndarray]] = deque()

    def process(fc: int):
        if fc not in plan:
            return
        frames = dict(buf)
        if fc not in frames:
            return
        px, py, rad = plan[fc]
        rad = float(min(rad, 0.06 * W))
        half = int(np.ceil(rad + 0.015 * W))
        crop, x0, y0 = crop_window(frames[fc], px, py, half)
        if crop is None:
            return
        shot = _shot_of(fc, cuts)
        bgs = []
        for off in (-4, 4, -8, 8, -12, 12):
            j = fc + off
            if j not in frames or _shot_of(j, cuts) != shot:
                continue
            q = by.get(j)
            if q is not None and abs(q.x - px) < half and abs(q.y - py) < half:
                continue  # the ball itself is inside that window
            c, bx0, by0 = crop_window(frames[j], px, py, half)
            if c is None or c.shape != crop.shape or (bx0, by0) != (x0, y0):
                continue
            e = float(np.percentile(np.abs(c.astype(np.int16) - crop.astype(np.int16)).max(axis=2), 75))
            bgs.append((e, c))
        bgs.sort(key=lambda t: t[0])
        bg = median_background([c for _, c in bgs[:4]])
        if bg is None:
            return
        # Local speed estimate for the streak-length limit.
        nb = [by[g] for g in (fc - 2, fc - 1, fc + 1, fc + 2) if g in by]
        speed = 0.0
        if len(nb) >= 2:
            speed = float(np.hypot(nb[-1].x - nb[0].x, nb[-1].y - nb[0].y) / max(1, nb[-1].frame - nb[0].frame))
        blob = find_blob(crop, bg, px - x0, py - y0, rad, float(r_of_frame(fc)), speed)
        if blob is not None and blob[2] > 0.15:
            out[fc] = (blob[0] + x0, blob[1] + y0, 0.25 + 0.35 * blob[2])

    for idx, fr in iter_frames(info, start_index=lo, end_index=hi):
        buf.append((idx, fr))
        if len(buf) > 25:
            buf.popleft()
        process(idx - 12)
    last = buf[-1][0] if buf else lo
    for c in range(max(lo, last - 11), last + 1):
        process(c)
    return out
