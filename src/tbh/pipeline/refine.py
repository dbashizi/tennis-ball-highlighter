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

from .radius import BallMeasurement, crop_window, measure_ball, median_background, smooth_radii
from .ringcolor import background_luminance, choose_ring_colors, segment_distance
from .track import TrackPoint
from .trackfile import FLAG_BOUNCE, FLAG_HIT
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
    sl: float = 0.0  # streak half-length, px
    sa: float = 0.0  # streak direction, radians (oriented along the motion)


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
    half = max(20, int(round(0.022 * W)))
    vel = _velocities(by_frame)
    meas: dict[int, BallMeasurement] = {}
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
            progress("track", 0.2 + 0.5 * done / max(1, total), f"refine frame {done}/{total}")
    # Tail: process the frames left in the buffer.
    last_idx = buf[-1][0] if buf else lo
    for c in range(max(lo, last_idx - 11), last_idx + 1):
        while buf and buf[0][0] < c - 12:
            buf.popleft()
        process(c)

    info_out: dict = {"measurements": len(meas)}
    fin = _finalize(points, meas, cuts, W, H, info_out)
    _ring_pass(info, fin, cuts, max_gap, progress)
    return fin, info_out


def _ring_pass(info: VideoInfo, fin: list[FinalPoint], cuts: list[int], max_gap: int,
               progress=None) -> None:
    """Second full-res pass: ring colour from the annulus around the final stadium (or circle)."""
    if not fin:
        return
    by = {q.frame: k for k, q in enumerate(fin)}
    lums: list[float | None] = [None] * len(fin)
    lo, hi = fin[0].frame, fin[-1].frame + 1
    for idx, fr in iter_frames(info, start_index=lo, end_index=hi):
        k = by.get(idx)
        if k is None:
            continue
        q = fin[k]
        lums[k] = background_luminance(fr, q.x, q.y, q.r, q.sl, q.sa)
        if progress and k % 20 == 0:
            progress("track", 0.7 + 0.3 * k / len(fin), "ring colours")
    shot = [_shot_of(q.frame, cuts) for q in fin]
    breaks = {0}
    for k in range(1, len(fin)):
        if shot[k] != shot[k - 1] or fin[k].frame - fin[k - 1].frame > max_gap + 1:
            breaks.add(k)
    for q, c in zip(fin, choose_ring_colors(lums, breaks)):
        q.ring = c


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


def _local_velocity(frames, xs, ys, pieces) -> np.ndarray:
    """px/frame from the final positions: central differences within a piece and adjacent frames."""
    n = len(frames)
    v = np.zeros((n, 2))
    for k in range(n):
        a = k - 1 if k > 0 and frames[k - 1] == frames[k] - 1 and pieces[k - 1] == pieces[k] else None
        b = k + 1 if k + 1 < n and frames[k + 1] == frames[k] + 1 and pieces[k + 1] == pieces[k] else None
        if a is not None and b is not None:
            v[k] = ((xs[b] - xs[a]) / 2, (ys[b] - ys[a]) / 2)
        elif b is not None:
            v[k] = (xs[b] - xs[k], ys[b] - ys[k])
        elif a is not None:
            v[k] = (xs[k] - xs[a], ys[k] - ys[a])
    return v


def estimate_exposure(sl_meas: np.ndarray, sl_w: np.ndarray, speed: np.ndarray) -> float:
    """k in sl ~= k * |v| (half the shutter fraction). Median over reliable streaks."""
    ok = np.isfinite(sl_meas) & (sl_w >= 0.5) & (speed >= 4.0)
    if ok.sum() < 8:
        return 0.25
    return float(np.clip(np.median(sl_meas[ok] / speed[ok]), 0.0, 0.6))


def smooth_streaks(frames, pieces, r, v, sl_meas, sl_w, ang_meas, ang_w, W):
    """Per-row (sl, sa) in px/radians.

    sl: a weighted mean over +-2 frames of the same piece, of the measured
    half-lengths (weight = reliability) and the exposure model k*|v| (weight
    0.3). Rows without a measurement use k*|v|. It is capped by physics:
    sl <= 0.75*|v| + 0.5*r, and sl <= 4% of width.

    sa: angle-aware. Measured directions (ambiguous by pi) and the velocity
    direction are averaged as doubled-angle vectors, then flipped to point along
    the motion.
    """
    n = len(frames)
    speed = np.hypot(v[:, 0], v[:, 1])
    k_e = estimate_exposure(sl_meas, sl_w, speed)
    model = k_e * speed
    sl = np.zeros(n)
    sa = np.zeros(n)
    for i in range(n):
        nb = [j for j in range(max(0, i - 2), min(n, i + 3))
              if pieces[j] == pieces[i] and abs(frames[j] - frames[i]) <= 2]
        num = 0.3 * model[i]
        den = 0.3
        c2 = s2 = 0.0
        for j in nb:
            if np.isfinite(sl_meas[j]) and sl_w[j] > 0:
                wj = sl_w[j] * (1.0 if j == i else 0.5)
                # The neighbour's value, rescaled to this row's speed if both move.
                val = sl_meas[j] * (speed[i] / speed[j]) if speed[j] > 2 and speed[i] > 2 else sl_meas[j]
                num += wj * val
                den += wj
            if np.isfinite(ang_meas[j]) and ang_w[j] > 0:
                wa = ang_w[j] * (1.0 if j == i else 0.5)
                c2 += wa * np.cos(2 * ang_meas[j])
                s2 += wa * np.sin(2 * ang_meas[j])
        if not any(np.isfinite(sl_meas[j]) and sl_w[j] > 0 for j in nb):
            num, den = model[i], 1.0
        sl[i] = num / den
        va = np.arctan2(v[i, 1], v[i, 0])
        if speed[i] > 1.0:
            wv = 0.3 * min(1.0, speed[i] / 8.0)
            c2 += wv * np.cos(2 * va)
            s2 += wv * np.sin(2 * va)
        a = 0.5 * np.arctan2(s2, c2) if (c2 or s2) else va
        if speed[i] > 1.0 and np.cos(a - va) < 0:
            a += np.pi
        sa[i] = (a + np.pi) % (2 * np.pi) - np.pi
    cap = np.minimum(0.75 * speed + 0.5 * np.asarray(r), 0.04 * W)
    sl = np.clip(sl, 0.0, cap)
    return sl, sa, k_e


def _finalize(points, meas, cuts, W, H, info_out=None) -> list[FinalPoint]:
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
        near = False
        if m is not None and m.weight >= min_w:
            near = np.hypot(m.x - fit_xy[0], m.y - fit_xy[1]) <= 0.008 * W
            if not near and m.sl > 0:
                # A long streak whose midpoint is away from the track point still
                # matches if the track point lies on the streak.
                d_seg = float(segment_distance(fit_xy[0], fit_xy[1], m.x, m.y, m.sl, m.angle))
                near = d_seg <= m.r + 0.004 * W and m.sl <= 0.04 * W
        if near:
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
    # Streak half-length and direction.
    pieces = [p.piece for p in pts]
    v = _local_velocity(frames, sx, sy, pieces)
    sl_meas = np.array([meas[p.frame].sl if measured[k] else np.nan for k, p in enumerate(pts)])
    ang_meas = np.array([meas[p.frame].angle if measured[k] else np.nan for k, p in enumerate(pts)])
    ang_w = np.array([meas[p.frame].weight * float(np.clip(meas[p.frame].elong - 1.2, 0, 1))
                      if measured[k] else 0.0 for k, p in enumerate(pts)])
    sl, sa, k_e = smooth_streaks(frames, pieces, r, v, sl_meas, meas_w, ang_meas, ang_w, W)
    for k, p in enumerate(pts):
        # At a bounce or hit the exposure straddles the contact: the streak is a V
        # and the central-difference velocity is meaningless. Without a direct
        # measurement a circle is less misleading than a wrongly oriented pill.
        if (p.flags & (FLAG_BOUNCE | FLAG_HIT)) and not measured[k]:
            sl[k] = 0.0
    if info_out is not None:
        info_out["exposure_k"] = round(k_e, 3)
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
                              "#101010", p.flags, p.detected, bool(measured[k]), float(sl[k]), float(sa[k])))
    return out
