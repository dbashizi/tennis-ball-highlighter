"""Full-resolution pass: per-point radius, refined centre and ring colour.

Frames are streamed once, with a buffer of +-12 frames for the background
median. For every track point we store the blob measurement and a small crop
of the current frame. The ring colour is computed from that crop after the
final centre and radius are known.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .radius import R_MAX_FRAC, BallMeasurement, crop_window, measure_ball, median_background, smooth_radii
from .ringcolor import background_luminance, choose_ring_colors
from .track import TrackPoint
from .video import VideoInfo, iter_frames

BG_OFFSETS = (-4, 4, -8, 8, -12, 12)
BG_USE = 4  # background crops used (those that best match the current surroundings)
LEAD_PRIOR = 0.45  # TrackNet marks the leading end of a motion streak (in frames of motion)


@dataclass
class FinalPoint:
    frame: int
    x: float
    y: float
    r: float
    conf: float
    ring: str
    flags: int
    detected: bool
    measured: bool


def _shot_of(frame: int, cuts: list[int]) -> int:
    return int(np.searchsorted(np.asarray(sorted(cuts)), frame, side="right"))


def refine(info: VideoInfo, points: list[TrackPoint], cuts: list[int],
           progress: Callable[[str, float, str], None] | None = None,
           max_gap: int = 8) -> tuple[list[FinalPoint], dict]:
    """Returns (final points, info). info has the fitted TrackNet lead and the measurement count."""
    if not points:
        return [], {"lead": None, "measurements": 0}
    W, H = info.width, info.height
    by_frame = {p.frame: p for p in points}
    first, last = min(by_frame), max(by_frame)
    half = max(16, int(round(0.018 * W)))
    vel = _velocities(by_frame)
    ring_half = int(np.ceil(2.5 * R_MAX_FRAC * W)) + 3
    meas: dict[int, BallMeasurement] = {}
    ring_crops: dict[int, tuple[np.ndarray, int, int]] = {}
    min_sep = 0.008 * W

    buf: deque[tuple[int, np.ndarray]] = deque()
    lo = max(0, first - 12)
    hi = min(len(info.pts), last + 13)
    total = hi - lo

    def process(center_idx: int):
        p = by_frame.get(center_idx)
        if p is None:
            return
        frames = {i: f for i, f in buf}
        cur = frames[center_idx]
        shot = _shot_of(center_idx, cuts)
        vx, vy = vel[center_idx]
        bx_, by_ = (p.raw if p.raw is not None else (p.x, p.y))
        # Search around the expected streak centre, not TrackNet's leading end.
        cx, cy = bx_ - LEAD_PRIOR * vx, by_ - LEAD_PRIOR * vy
        crop, x0, y0 = crop_window(cur, cx, cy, half)
        rc, rx0, ry0 = crop_window(cur, p.x, p.y, ring_half)
        if rc is not None:
            ring_crops[center_idx] = (rc.copy(), rx0, ry0)
        if crop is None:
            return
        bgs = []
        yy, xx = np.mgrid[0:crop.shape[0], 0:crop.shape[1]]
        outside = np.hypot(xx + x0 - cx, yy + y0 - cy) > max(0.008 * W, 0.6 * np.hypot(vx, vy) + 0.005 * W)
        for off in BG_OFFSETS:
            j = center_idx + off
            if j not in frames or _shot_of(j, cuts) != shot:
                continue
            q = by_frame.get(j)
            if q is not None and np.hypot(q.x - p.x, q.y - p.y) < min_sep:
                continue  # the ball is still (almost) here: bad background
            c, bx0, by0 = crop_window(frames[j], cx, cy, half)
            if c is not None and c.shape == crop.shape and (bx0, by0) == (x0, y0):
                # How well do the surroundings match? (players and shadows move)
                dd = np.abs(c.astype(np.int16) - crop.astype(np.int16)).max(axis=2)[outside]
                bgs.append((float(np.percentile(dd, 90)) if dd.size else 0.0, abs(off), c))
        bgs.sort(key=lambda t: (t[0] > 12, t[1], t[0]))
        good = [c for e, _, c in bgs if e <= 12][:BG_USE]
        if len(good) < 2:
            good = [c for _, _, c in sorted(bgs, key=lambda t: t[0])][:3]
        bg = median_background(good)
        if bg is None:
            return
        m = measure_ball(crop, bg, cx - x0, cy - y0, W, search_frac=0.004 + 0.5 * np.hypot(vx, vy) / W)
        if m is not None:
            m.x += x0
            m.y += y0
            meas[center_idx] = m

    done = 0
    for idx, fr in iter_frames(info, start_index=lo, end_index=hi):
        buf.append((idx, fr))
        if len(buf) > 25:
            buf.popleft()
        c = idx - 12
        if c >= lo:
            process(c)
        done += 1
        if progress and done % 10 == 0:
            progress("track", 0.2 + 0.7 * done / max(1, total), f"refine frame {done}/{total}")
    # Tail: process the frames left in the buffer.
    last_idx = buf[-1][0] if buf else lo
    for c in range(max(lo, last_idx - 11), last_idx + 1):
        while buf and buf[0][0] < c - 12:
            buf.popleft()
        process(c)

    info_out: dict = {"measurements": len(meas)}
    fin = _finalize(points, meas, ring_crops, cuts, W, H, max_gap, info_out)
    return fin, info_out


def _velocities(by_frame: dict[int, TrackPoint]) -> dict[int, tuple[float, float]]:
    """Per-frame velocity (px/frame) from the track fit; central differences within a piece."""
    out = {}
    for f, p in by_frame.items():
        a = by_frame.get(f - 1)
        b = by_frame.get(f + 1)
        a = a if a is not None and a.piece == p.piece else None
        b = b if b is not None and b.piece == p.piece else None
        if a is not None and b is not None:
            out[f] = ((b.x - a.x) / 2, (b.y - a.y) / 2)
        elif b is not None:
            out[f] = (b.x - p.x, b.y - p.y)
        elif a is not None:
            out[f] = (p.x - a.x, p.y - a.y)
        else:
            out[f] = (0.0, 0.0)
    return out


def estimate_lead(points, meas, vel, W) -> float:
    """Fit k in: measured_centre ~= detection - k * velocity (TrackNet's lead along the motion)."""
    ks = []
    for p in points:
        m = meas.get(p.frame)
        if m is None or p.raw is None or m.weight < 0.5:
            continue
        vx, vy = vel[p.frame]
        v2 = vx * vx + vy * vy
        if v2 < 9.0:
            continue
        dx, dy = m.x - p.raw[0], m.y - p.raw[1]
        ks.append(-(dx * vx + dy * vy) / v2)
    if len(ks) < 8:
        return LEAD_PRIOR
    return float(np.clip(np.median(ks), 0.0, 1.0))


def _finalize(points, meas, ring_crops, cuts, W, H, max_gap, info_out=None) -> list[FinalPoint]:
    pts = sorted(points, key=lambda p: p.frame)
    frames = np.array([p.frame for p in pts])
    by_frame = {p.frame: p for p in pts}
    vel = _velocities(by_frame)
    lead = estimate_lead(pts, meas, vel, W)
    if info_out is not None:
        info_out["lead"] = round(lead, 3)
    # Centre: a good measurement, else the raw detection, else the track fit.
    # Unmeasured positions (detections and fits) carry TrackNet's lead: shift them back.
    cx = np.array([p.x - lead * vel[p.frame][0] for p in pts], float)
    cy = np.array([p.y - lead * vel[p.frame][1] for p in pts], float)
    wgt = np.full(len(pts), 0.15)
    measured = np.zeros(len(pts), bool)
    for k, p in enumerate(pts):
        m = meas.get(p.frame)
        vx, vy = vel[p.frame]
        fit_xy = np.array([p.x - lead * vx, p.y - lead * vy])
        min_w = 0.15 if p.detected else 0.6
        if m is not None and m.weight >= min_w and np.hypot(m.x - fit_xy[0], m.y - fit_xy[1]) <= 0.008 * W:
            cx[k], cy[k] = m.x, m.y
            wgt[k] = 0.5 + 0.5 * m.weight
            measured[k] = True
        elif p.raw is not None:
            raw = np.array(p.raw) - lead * np.array([vx, vy])
            if np.hypot(*(raw - fit_xy)) <= 0.006 * W:
                cx[k], cy[k] = raw
                wgt[k] = 0.4
    # Light smoothing: local weighted quadratic over +-2 frames within the same piece.
    sx, sy = cx.copy(), cy.copy()
    for k, p in enumerate(pts):
        nb = [j for j in range(max(0, k - 2), min(len(pts), k + 3))
              if pts[j].piece == p.piece and abs(pts[j].frame - p.frame) <= 2]
        if len(nb) < 4:
            continue
        t = frames[nb] - p.frame
        w = wgt[nb]
        for arr, out in ((cx, sx), (cy, sy)):
            c = np.polyfit(t, arr[nb], 2, w=np.sqrt(w))
            out[k] = np.polyval(c, 0.0)
    # Radius.
    meas_r = np.array([meas[p.frame].r if (measured[k]) else np.nan for k, p in enumerate(pts)])
    meas_w = np.array([meas[p.frame].weight if measured[k] else 0.0 for k, p in enumerate(pts)])
    shot_ids = np.array([_shot_of(p.frame, cuts) for p in pts])
    r = smooth_radii(frames, sy, meas_r, meas_w, W, H, shot_ids)
    # Ring colour, with hysteresis reset at cuts and track breaks.
    lums = []
    for k, p in enumerate(pts):
        rc = ring_crops.get(p.frame)
        if rc is None:
            lums.append(None)
            continue
        crop, x0, y0 = rc
        lums.append(background_luminance(crop, sx[k] - x0, sy[k] - y0, r[k]))
    breaks = {0}
    for k in range(1, len(pts)):
        if shot_ids[k] != shot_ids[k - 1] or frames[k] - frames[k - 1] > max_gap + 1:
            breaks.add(k)
    rings = choose_ring_colors(lums, breaks)
    out = []
    for k, p in enumerate(pts):
        conf = p.conf
        if p.detected:
            if measured[k]:
                # The full-res blob confirms a ball-like object at this spot.
                conf = 1.0 - (1.0 - conf) * (1.0 - 0.4 * meas_w[k])
            else:
                conf *= 0.85
        out.append(FinalPoint(p.frame, float(sx[k]), float(sy[k]), float(r[k]), float(np.clip(conf, 0, 1)),
                              rings[k], p.flags, p.detected, bool(measured[k])))
    return out
