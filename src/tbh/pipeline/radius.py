"""Ball radius (and centre refinement) from full-resolution pixels.

TrackNet gives only a centre. The ball blob is segmented in a small crop by
its difference from a *background* crop: the per-pixel median of the same
window in neighbouring frames, where the ball has usually moved away.

* The blob's second moments give an ellipse. For a round blob,
  ``r = 2*sqrt(lambda_min)`` (uniform disk). For a motion-blurred streak we use
  the half-width *across* the streak, ``r = sqrt(3*lambda_min)`` (uniform band),
  and blend the two by elongation. The streak length is never used.
* The diff-weighted centroid refines the centre, when the blob is clean.

Radii are then smoothed heavily. A robust linear model ``r = a + b*y`` (depth
follows the image row for a fixed broadcast camera) is fitted per camera shot.
Each measurement's ratio to the model is median-filtered and Gaussian-smoothed
over time and clamped, and the result is clamped to an absolute range
relative to frame width.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from scipy.ndimage import gaussian_filter1d, median_filter
from scipy.optimize import least_squares

# Absolute clamp for r as a fraction of frame width. Broadcast HD footage is
# typically 0.2-0.6% (a diameter of 0.4-1.2%). Upscaled SD footage like our test clip
# measures lower (r ~ 0.15%), so the floor is below that.
R_MIN_FRAC = 0.0012
R_MAX_FRAC = 0.0065


@dataclass
class BallMeasurement:
    x: float  # refined centre (analysis-frame px)
    y: float
    r: float  # radius px (minor-axis based)
    major: float  # half-length along the streak, px
    contrast: float  # peak background difference (0..255 scale)
    weight: float  # 0..1 reliability
    sl: float = 0.0  # streak half-length: centre to cap centre, px (0 = round)
    angle: float = 0.0  # streak direction, radians in [0, pi) (ambiguous by pi)
    elong: float = 1.0  # along/across extent ratio of the loose blob


def crop_window(img: np.ndarray, cx: float, cy: float, half: int):
    """Crop [cx-half, cx+half) with edge clamping; return (crop, x0, y0)."""
    h, w = img.shape[:2]
    x0 = int(round(cx)) - half
    y0 = int(round(cy)) - half
    x0c, y0c = max(0, x0), max(0, y0)
    x1c, y1c = min(w, x0 + 2 * half), min(h, y0 + 2 * half)
    if x1c - x0c < half or y1c - y0c < half:
        return None, x0c, y0c
    return img[y0c:y1c, x0c:x1c], x0c, y0c


def measure_ball(frame: np.ndarray, background: np.ndarray, x: float, y: float, W: float,
                 search_frac: float = 0.007) -> BallMeasurement | None:
    """Segment the ball near (x, y). ``frame`` and ``background`` are same-size crops.

    (x, y) are coordinates *within the crop*.
    """
    if frame is None or background is None or frame.shape != background.shape:
        return None
    f = frame.astype(np.float32)
    b = background.astype(np.float32)
    diff = np.sqrt(((f - b) ** 2).sum(axis=2))
    diff = cv2.GaussianBlur(diff, (0, 0), 0.8)
    h, w = diff.shape
    yy, xx = np.mgrid[0:h, 0:w]
    near = np.hypot(xx + 0.5 - x, yy + 0.5 - y) <= max(4.0, search_frac * W)
    if not near.any():
        return None
    peak_idx = np.argmax(np.where(near, diff, -1))
    py, px = divmod(int(peak_idx), w)
    peak = float(diff[py, px])
    noise = float(np.median(np.abs(diff - np.median(diff)))) * 1.4826 + 1.0
    if peak < 8.0 or peak < 4.0 * noise:
        return None
    thr = max(0.5 * peak, 3.0 * noise, 5.0)
    mask = (diff >= thr).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    k = lab[py, px]
    if k == 0:
        return None
    bx, by, bw, bh, area = stats[k]
    if bx == 0 or by == 0 or bx + bw >= w or by + bh >= h:
        return None  # blob touches the crop border: a player or line, not a ball
    rmax = R_MAX_FRAC * W
    if area > 6 * np.pi * rmax * rmax or area < 3:
        return None
    m = lab == k
    wts = diff * m
    s = float(wts.sum())
    cxm = float((xx * wts).sum() / s) + 0.5
    cym = float((yy * wts).sum() / s) + 0.5
    # Binary moments for the shape (the extent at half maximum).
    ys_, xs_ = np.nonzero(m)
    xs_ = xs_ + 0.5
    ys_ = ys_ + 0.5
    cov = np.cov(np.stack([xs_, ys_])) if len(xs_) > 2 else np.eye(2) * 0.25
    cov = cov + np.eye(2) / 12.0  # pixel-area correction
    lam = np.sort(np.linalg.eigvalsh(cov))
    lam_min, lam_max = max(lam[0], 1e-3), max(lam[1], 1e-3)
    elong = float(np.sqrt(lam_max / lam_min))
    t = float(np.clip((elong - 1.2) / 1.3, 0.0, 1.0))  # 0 = round, 1 = streak
    r = (1 - t) * 2.0 * np.sqrt(lam_min) + t * np.sqrt(3.0 * lam_min)
    major = (1 - t) * 2.0 * np.sqrt(lam_max) + t * np.sqrt(3.0 * lam_max)
    if not (0.5 * R_MIN_FRAC * W <= r <= 1.5 * rmax):
        return None
    if np.hypot(cxm - x, cym - y) > search_frac * W * 1.2:
        return None
    # Reliability: contrast over noise, and a compact blob.
    fill = area / max(1.0, np.pi * r * max(major, r))
    weight = float(np.clip((peak / noise - 4.0) / 8.0, 0.0, 1.0) * np.clip(fill, 0.3, 1.0))
    meas = BallMeasurement(cxm, cym, float(r), float(major), peak, weight)
    _measure_streak(meas, diff, lab == k, py, px, peak, noise, xx, yy)
    return meas


def _measure_streak(meas: BallMeasurement, diff, tight, py, px, peak, noise, xx, yy,
                    loose_frac: float = 0.3) -> None:
    """Streak half-length and direction from a looser mask that includes the faint tail.

    The half-extents along and across the principal axis come from percentiles
    of the pixel projections. ``sl = along - across``: the blur widens both by
    the same amount, so the difference is the centre-to-cap-centre distance of a
    stadium. The centre moves to the streak's midpoint along the axis.
    """
    h, w = diff.shape
    thr = max(loose_frac * peak, 3.0 * noise, 4.0)
    loose = (diff >= thr).astype(np.uint8)
    _, llab = cv2.connectedComponents(loose, connectivity=8)
    k = llab[py, px]
    m = llab == k
    ys, xs = np.nonzero(m)
    if k == 0 or len(xs) < 4 or xs.min() == 0 or ys.min() == 0 or xs.max() == w - 1 or ys.max() == h - 1 \
            or m.sum() > 12 * max(1, tight.sum()):
        m = tight  # loose blob merged into something else: fall back to the tight blob
        ys, xs = np.nonzero(m)
    xs = xs + 0.5
    ys = ys + 0.5
    wts = diff[m]
    cx = float((xs * wts).sum() / wts.sum())
    cy = float((ys * wts).sum() / wts.sum())
    cov = np.cov(np.stack([xs - cx, ys - cy]), aweights=wts) if len(xs) > 2 else np.eye(2)
    evals, evecs = np.linalg.eigh(cov)
    ax = evecs[:, 1]  # principal axis
    along = (xs - cx) * ax[0] + (ys - cy) * ax[1]
    across = -(xs - cx) * ax[1] + (ys - cy) * ax[0]
    lo, hi = np.percentile(along, [1.5, 98.5])
    alo, ahi = np.percentile(across, [1.5, 98.5])
    half_along = 0.5 * (hi - lo) + 0.5  # + half a pixel for the pixel footprint
    half_across = 0.5 * (ahi - alo) + 0.5
    meas.elong = float(half_along / max(half_across, 0.5))
    meas.sl = float(max(0.0, half_along - half_across))
    meas.angle = float(np.arctan2(ax[1], ax[0]) % np.pi)
    if meas.sl > 0:
        mid = 0.5 * (lo + hi)
        meas.x = float(cx + mid * ax[0])
        meas.y = float(cy + mid * ax[1])


def median_background(crops: list[np.ndarray]) -> np.ndarray | None:
    crops = [c for c in crops if c is not None]
    if len(crops) < 2:
        return None
    shape = crops[0].shape
    crops = [c for c in crops if c.shape == shape]
    if len(crops) < 2:
        return None
    return np.median(np.stack(crops), axis=0).astype(np.uint8)


# --------------------------------------------------------------------------- smoothing

def fit_depth_model(ys: np.ndarray, rs: np.ndarray, ws: np.ndarray, H: float):
    """Robust r = a + b*(y/H). Returns a callable, or a constant model if the data are thin."""
    ok = ws > 0
    ys, rs, ws = ys[ok], rs[ok], ws[ok]
    if len(rs) == 0:
        return None
    med = float(np.median(rs))
    if len(rs) < 8 or np.ptp(ys) < 0.1 * H:
        return lambda y: np.full_like(np.asarray(y, dtype=float), med)

    def resid(p):
        return np.sqrt(ws) * (p[0] + p[1] * ys / H - rs)

    sol = least_squares(resid, x0=[med, 0.0], loss="soft_l1", f_scale=0.3 * med)
    a, b = sol.x
    # The ball can't get smaller towards the camera (larger y): keep b >= 0.
    if b < 0:
        return lambda y: np.full_like(np.asarray(y, dtype=float), med)
    return lambda y: a + b * np.asarray(y, dtype=float) / H


def smooth_radii(frames: np.ndarray, ys: np.ndarray, meas_r: np.ndarray, meas_w: np.ndarray,
                 W: float, H: float, shot_ids: np.ndarray, sigma_frames: float = 6.0) -> np.ndarray:
    """Heavily smoothed radius (px) for every track point.

    ``meas_r`` is NaN where there is no measurement. All arrays are aligned and sorted by frame.
    """
    out = np.zeros(len(frames), dtype=float)
    rmin, rmax = R_MIN_FRAC * W, R_MAX_FRAC * W
    global_ok = np.isfinite(meas_r) & (meas_w > 0)
    global_med = float(np.median(meas_r[global_ok])) if global_ok.any() else 0.0025 * W
    for sid in np.unique(shot_ids):
        idx = np.nonzero(shot_ids == sid)[0]
        r = meas_r[idx]
        w = meas_w[idx]
        ok = np.isfinite(r) & (w > 0)
        model = fit_depth_model(ys[idx][ok], r[ok], w[ok], H) if ok.sum() else None
        if model is None:
            model = lambda y, m=global_med: np.full_like(np.asarray(y, dtype=float), m)  # noqa: E731
        base = np.clip(model(ys[idx]), rmin, rmax)
        # Ratio to the model, on a dense frame grid so gaps count as time.
        f = frames[idx]
        grid = np.arange(f.min(), f.max() + 1)
        q = np.full(len(grid), np.nan)
        qw = np.zeros(len(grid))
        q[f[ok] - f.min()] = r[ok] / base[ok]
        qw[f[ok] - f.min()] = w[ok]
        filled = _fill_nan(q)
        if np.isnan(filled).all():
            qs = np.ones(len(grid))
        else:
            win = int(2 * sigma_frames + 1) | 1
            qm = median_filter(filled, size=win, mode="nearest")
            # Weighted Gaussian: sum(w*q)/sum(w), which ignores unmeasured frames.
            wq = gaussian_filter1d(np.nan_to_num(qm) * (qw + 0.05), sigma_frames, mode="nearest")
            ww = gaussian_filter1d(qw + 0.05, sigma_frames, mode="nearest")
            qs = wq / ww
        qs = np.clip(qs, 0.75, 1.3)
        out[idx] = np.clip(base * qs[f - f.min()], rmin, rmax)
    return out


def _fill_nan(a: np.ndarray) -> np.ndarray:
    a = a.copy()
    ok = np.isfinite(a)
    if not ok.any():
        return a
    idx = np.arange(len(a))
    a[~ok] = np.interp(idx[~ok], idx[ok], a[ok])
    return a
