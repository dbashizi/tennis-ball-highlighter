"""Offline tracking: pick one consistent ball track from per-frame candidates.

Steps (all offline, future frames allowed):

1. Static false positives: a candidate location that recurs in several
   non-consecutive frames (logo, white spot, line crossing) is dropped.
2. Tracklets: candidates are linked frame to frame (gaps of up to 4 frames)
   under a constant-velocity prediction and a physical speed limit.
3. Selection: tracklets are ranked by score and accepted when they don't
   overlap an already accepted one. Short tracklets must be consistent with
   their accepted neighbours. Camera cuts split everything.
4. Piecewise polynomial smoothing: the accepted detections are split into
   pieces at kinks (bounces and hits). Each piece gets a robust quadratic fit
   per coordinate, and outliers are removed and refitted.
5. Gap fill of up to ``max_gap`` frames (flag 1). Linear interpolation is used
   across a piece boundary, and the polynomial inside a piece.
6. Events: kinks where the vertical image velocity flips are labelled. A hit
   (4) reverses the long-term depth direction; otherwise it is a bounce (2).
   This is a heuristic.
7. Confidence: detection score, smoothed along the track, times a fit-quality
   term. It decays with distance from the nearest real detection.

All distances are in analysis-frame pixels; thresholds scale with frame width
and frame rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.spatial import cKDTree

from .trackfile import FLAG_BOUNCE, FLAG_HIT, FLAG_INTERPOLATED


@dataclass
class TrackParams:
    min_score: float = 0.15
    max_speed: float = 0.07  # fraction of width per frame at 25 fps
    link_gap: int = 4
    max_gap: int = 8  # frames filled by interpolation
    static_radius: float = 0.0035  # fraction of width
    static_min_frames: int = 4
    static_min_span: int = 12
    static_min_visits: int = 3
    min_tracklet: int = 3
    fit_tol: float = 0.0045  # piece fit residual tolerance, fraction of width
    min_piece_dets: int = 3
    piece_inner_gap: int = 3  # a longer detection gap always ends a piece
    min_conf: float = 0.35


@dataclass
class TrackPoint:
    frame: int
    x: float
    y: float
    conf: float
    flags: int
    detected: bool
    raw: tuple[float, float] | None = None
    piece: int = -1  # id of the smooth piece (between kinks) this point belongs to


@dataclass
class TrackResult:
    points: list[TrackPoint]
    runs: list[tuple[int, int]]  # (first_frame, last_frame) of continuous runs
    raw: dict[int, tuple[float, float, float]] = field(default_factory=dict)  # selected detections
    static_removed: int = 0
    outliers_removed: int = 0


# --------------------------------------------------------------------------- 1. static FPs

def remove_static(cands: list[list[tuple[float, float, float]]], W: float, p: TrackParams):
    pts, owner = [], []
    for f, cs in enumerate(cands):
        for k, c in enumerate(cs):
            pts.append(c[:2])
            owner.append((f, k))
    if not pts:
        return cands, 0
    tree = cKDTree(np.array(pts))
    rad = p.static_radius * W
    bad = set()
    for i, nb in enumerate(tree.query_ball_point(np.array(pts), rad)):
        frames = sorted({owner[j][0] for j in nb})
        if len(frames) < p.static_min_frames or frames[-1] - frames[0] < p.static_min_span:
            continue
        # A slow ball can sit in a few consecutive frames, and during a rally
        # the ball also revisits a spot now and then (players hit from similar
        # places). A static artefact keeps coming back: require at least 3
        # separate visits. Isolated one-off statics are removed later by the
        # outlier test anyway.
        visits = 1 + sum(1 for a, b in zip(frames, frames[1:]) if b - a > 2)
        if visits >= p.static_min_visits:
            bad.add(owner[i])
    out = [[c for k, c in enumerate(cs) if (f, k) not in bad] for f, cs in enumerate(cands)]
    return out, len(bad)


# --------------------------------------------------------------------------- 2. tracklets

@dataclass
class Tracklet:
    frames: list[int] = field(default_factory=list)
    xy: list[tuple[float, float]] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)

    def predict(self, f: int) -> tuple[float, float, float]:
        """Predicted position at frame f and speed estimate (px/frame)."""
        x, y = self.xy[-1]
        if len(self.frames) >= 2:
            df = self.frames[-1] - self.frames[-2]
            vx = (self.xy[-1][0] - self.xy[-2][0]) / df
            vy = (self.xy[-1][1] - self.xy[-2][1]) / df
            g = f - self.frames[-1]
            return x + vx * g, y + vy * g, float(np.hypot(vx, vy))
        return x, y, 0.0

    @property
    def score(self) -> float:
        return float(np.sum(self.scores))

    def static_like(self, W: float) -> bool:
        if len(self.frames) < 6:
            return False
        a = np.array(self.xy)
        return float(np.ptp(a[:, 0]) + np.ptp(a[:, 1])) < 0.004 * W


def build_tracklets(cands, lo: int, hi: int, W: float, fps: float, p: TrackParams) -> list[Tracklet]:
    vmax = p.max_speed * W * 25.0 / fps
    active: list[Tracklet] = []
    done: list[Tracklet] = []
    for f in range(lo, hi):
        still = []
        for t in active:
            (still if f - t.frames[-1] <= p.link_gap else done).append(t)
        active = still
        cs = [c for c in cands[f] if c[2] >= p.min_score]
        pairs = []
        for ti, t in enumerate(active):
            g = f - t.frames[-1]
            px, py, v = t.predict(f)
            lx, ly = t.xy[-1]
            for ci, c in enumerate(cs):
                d_last = np.hypot(c[0] - lx, c[1] - ly)
                if d_last > vmax * g:
                    continue
                d_pred = np.hypot(c[0] - px, c[1] - py)
                tol = 0.012 * W + 0.5 * v * g if len(t.frames) >= 2 else vmax * g
                if d_pred <= tol:
                    pairs.append((d_pred / max(tol, 1e-6) - 0.3 * c[2], ti, ci))
        pairs.sort()
        used_t, used_c = set(), set()
        for _, ti, ci in pairs:
            if ti in used_t or ci in used_c:
                continue
            used_t.add(ti)
            used_c.add(ci)
            t = active[ti]
            t.frames.append(f)
            t.xy.append(cs[ci][:2])
            t.scores.append(cs[ci][2])
        for ci, c in enumerate(cs):
            if ci not in used_c:
                active.append(Tracklet([f], [c[:2]], [c[2]]))
    return done + active


def select_tracklets(tracklets: list[Tracklet], W: float, fps: float, p: TrackParams) -> dict[int, tuple]:
    """Greedy choice of one candidate per frame; overlapping tracklets are trimmed, not discarded."""
    tracklets = [t for t in tracklets if not t.static_like(W)]
    chosen: dict[int, tuple] = {}
    for t in sorted(tracklets, key=lambda t: (-(len(t.frames) >= p.min_tracklet), -t.score)):
        keep = [(f, xy, s) for f, xy, s in zip(t.frames, t.xy, t.scores) if f not in chosen]
        if len(t.frames) >= p.min_tracklet:
            if len(keep) < 2:
                continue
        else:
            keep = [k for k in keep if k[2] >= 0.5]
        for f, xy, s in keep:
            chosen[f] = (xy[0], xy[1], s)
    return chosen


def reject_outliers(frs: np.ndarray, xy: np.ndarray, sc: np.ndarray, W: float, fps: float,
                    p: TrackParams) -> np.ndarray:
    """Two-sided consistency test. Returns a boolean inlier mask.

    A detection is kept if a short polynomial through its neighbours on the
    left, or on the right, predicts it. A detection that only has a close
    neighbour pair (no side fit available) is kept if the pair is confident and
    moves at a plausible speed. The worst offender is removed first, then the
    test repeats.
    """
    n = len(frs)
    keep = np.ones(n, bool)
    vmax = p.max_speed * W * 25.0 / fps
    win = p.max_gap

    def side_err(i, idx):
        if len(idx) < 2:
            return None
        f = frs[idx].astype(float)
        deg = 1 if len(idx) < 4 else 2
        e = []
        g = min(abs(frs[i] - frs[idx[0]]), abs(frs[i] - frs[idx[-1]]))
        for d in (0, 1):
            c = np.polyfit(f - frs[i], xy[idx, d], deg)
            e.append(np.polyval(c, 0.0) - xy[i, d])
        span_v = np.hypot(*(xy[idx[-1]] - xy[idx[0]])) / max(1, abs(frs[idx[-1]] - frs[idx[0]]))
        tol = 0.008 * W + 0.35 * span_v * g
        return float(np.hypot(*e)) / (tol * (1.0 + 0.6 * sc[i]))

    def score(i):
        live = np.nonzero(keep)[0]
        left = [j for j in live if frs[i] - win <= frs[j] < frs[i]][-4:]
        right = [j for j in live if frs[i] < frs[j] <= frs[i] + win][:4]
        errs = [e for side in (left, right[::-1]) for k in (2, 3, 4) if len(side) >= k
                for e in [side_err(i, sorted(side[-k:]))] if e is not None]
        best = min(errs) if errs else 10.0
        if best <= 1.0:
            return best
        # Fallback: a confident, physically plausible close pair (e.g. a ball
        # visible for only two frames next to an occlusion).
        near = [j for j in left[-1:] + right[:1] if abs(frs[j] - frs[i]) <= 2]
        for j in near:
            d = np.hypot(*(xy[j] - xy[i]))
            if d <= vmax * abs(frs[j] - frs[i]) and sc[i] + sc[j] >= 1.35:
                return 0.9
        return best

    while True:
        live = np.nonzero(keep)[0]
        if len(live) == 0:
            break
        errs = np.array([score(i) for i in live])
        worst = int(np.argmax(errs))
        if errs[worst] <= 1.0:
            break
        keep[live[worst]] = False
    return keep


# --------------------------------------------------------------------------- 4. pieces

def _polyfit(fr: np.ndarray, v: np.ndarray, deg: int) -> np.ndarray:
    deg = min(deg, len(fr) - 1)
    c = np.polyfit(fr - fr[0], v, deg)
    return c


def _eval(c: np.ndarray, fr0: int, fr) -> np.ndarray:
    return np.polyval(c, np.asarray(fr, dtype=float) - fr0)


def fit_piece(fr: np.ndarray, xy: np.ndarray, deg: int = 2):
    cx = _polyfit(fr, xy[:, 0], deg)
    cy = _polyfit(fr, xy[:, 1], deg)
    pred = np.stack([_eval(cx, fr[0], fr), _eval(cy, fr[0], fr)], 1)
    res = np.hypot(*(pred - xy).T)
    return (cx, cy, int(fr[0])), res


def segment_pieces(fr: np.ndarray, xy: np.ndarray, tol: float, max_gap: int):
    """Greedy split of a detection sequence into smooth pieces.

    Returns (pieces, outlier_mask). Each piece is (i0, i1), inclusive indices
    into fr. Consecutive pieces share their boundary detection when they touch
    at a kink.
    """
    n = len(fr)
    outlier = np.zeros(n, bool)
    pieces = []
    i0 = 0
    while i0 < n:
        members = [i0]
        j = i0 + 1
        while j < n:
            if fr[j] - fr[members[-1]] > max_gap + 1:
                break
            trial = members + [j]
            if len(trial) <= 3:
                members = trial
                j += 1
                continue
            _, res = fit_piece(fr[trial], xy[trial])
            if res.max() <= tol:
                members = trial
                j += 1
                continue
            # Is j a lone outlier? Test by skipping it.
            if j + 1 < n and fr[j + 1] - fr[members[-1]] <= max_gap + 1:
                _, res2 = fit_piece(fr[members + [j + 1]], xy[members + [j + 1]])
                if res2.max() <= tol:
                    k = j + 2
                    ok = True
                    if k < n and fr[k] - fr[j + 1] <= max_gap + 1:
                        _, res3 = fit_piece(fr[members + [j + 1, k]], xy[members + [j + 1, k]])
                        ok = res3.max() <= tol
                    if ok:
                        outlier[j] = True
                        members = members + [j + 1]
                        j += 2
                        continue
            break
        pieces.append((members[0], members[-1], members))
        if j >= n:
            break
        if fr[j] - fr[members[-1]] > max_gap + 1:
            i0 = j  # real gap: new run
        else:
            # Kink: the next piece starts at the last member (shared boundary),
            # unless that makes the previous piece degenerate.
            i0 = members[-1] if len(members) >= 3 else j
            if i0 == members[0]:
                i0 = j
    return pieces, outlier


# --------------------------------------------------------------------------- driver

def track(cands: list[list[tuple[float, float, float]]], cuts: list[int], W: float, H: float,
          fps: float, params: TrackParams | None = None) -> TrackResult:
    p = params or TrackParams()
    n = len(cands)
    cands, n_static = remove_static(cands, W, p)
    bounds = [0] + sorted(c for c in cuts if 0 < c < n) + [n]
    chosen: dict[int, tuple] = {}
    for lo, hi in zip(bounds, bounds[1:]):
        tl = build_tracklets(cands, lo, hi, W, fps, p)
        chosen.update(select_tracklets(tl, W, fps, p))

    points: list[TrackPoint] = []
    runs: list[tuple[int, int]] = []
    n_out = 0
    tol = p.fit_tol * W
    for lo, hi in zip(bounds, bounds[1:]):
        frs = np.array(sorted(f for f in chosen if lo <= f < hi), dtype=int)
        if len(frs) == 0:
            continue
        xy = np.array([chosen[f][:2] for f in frs], dtype=float)
        sc = np.array([chosen[f][2] for f in frs], dtype=float)
        inl = reject_outliers(frs, xy, sc, W, fps, p)
        n_out += int((~inl).sum())
        frs, xy, sc = frs[inl], xy[inl], sc[inl]
        if len(frs) == 0:
            continue
        pieces, outl = segment_pieces(frs, xy, tol, p.piece_inner_gap)
        n_out += int(outl.sum())
        pts, rr, n_o2 = _render_pieces(frs, xy, sc, pieces, outl, p, W, fps, H)
        n_out += n_o2
        points += pts
        runs += rr
    points.sort(key=lambda q: q.frame)
    _snap_to_weak(points, cands, W, p)
    return TrackResult(points=points, runs=runs, raw=chosen, static_removed=n_static,
                       outliers_removed=n_out)


def _snap_to_weak(points: list[TrackPoint], cands, W: float, p: TrackParams) -> None:
    """Replace an interpolated position with a weak raw candidate when one lies close to it.

    TrackNet often still fires weakly (score 0.1-0.5) on the ball near players.
    Those detections are too weak to start a track, but they are more accurate
    than an interpolation, which can cut corners at hits.
    """
    rad = 0.015 * W
    for q in points:
        if q.detected or not (0 <= q.frame < len(cands)):
            continue
        best = None
        for c in cands[q.frame]:
            if c[2] < 0.5 * p.min_score:
                continue
            d = float(np.hypot(c[0] - q.x, c[1] - q.y))
            if d <= rad and (best is None or d < best[0]):
                best = (d, c)
        if best is not None:
            c = best[1]
            q.x, q.y = float(c[0]), float(c[1])
            q.raw = (float(c[0]), float(c[1]))
            q.detected = True
            q.flags &= ~FLAG_INTERPOLATED


def _render_pieces(frs, xy, sc, pieces, outl, p: TrackParams, W, fps, H):
    tol = p.fit_tol * W
    fits = []
    n_out = 0
    for i0, i1, members in pieces:
        m = np.array([k for k in members if not outl[k]])
        pair_ok = (len(m) == 2 and sc[m].sum() >= 1.2 and frs[m[1]] - frs[m[0]] <= 2)
        if len(m) < p.min_piece_dets and not pair_ok:
            fits.append(None)
            continue
        fit, res = fit_piece(frs[m], xy[m])
        # One robust pass: drop residuals far above tol and refit.
        bad = res > 1.5 * tol
        if bad.any() and len(m) - int(bad.sum()) >= p.min_piece_dets:
            n_out += int(bad.sum())
            outl[m[bad]] = True
            m = m[~bad]
            fit, res = fit_piece(frs[m], xy[m])
        rms = float(np.sqrt(np.mean(res ** 2))) if len(res) else 0.0
        if len(m) < p.min_piece_dets:
            rms = max(rms, 0.5 * tol)  # a 2-point piece is less certain
        fits.append((fit, m, rms))

    # Per-frame values from each valid piece.
    by_frame: dict[int, TrackPoint] = {}
    piece_spans = []
    for pid, ((i0, i1, members), fz) in enumerate(zip(pieces, fits)):
        if fz is None:
            continue
        (cx, cy, f0), m, rms = fz
        fa, fb = int(frs[m[0]]), int(frs[m[-1]])
        det_frames = {int(frs[k]): k for k in m}
        quality = float(np.exp(-(rms / (0.5 * tol)) ** 2))
        # Smoothed detection score along the piece.
        s_det = np.array([sc[k] for k in m])
        ker = np.ones(5) / 5
        s_sm = np.convolve(np.pad(s_det, 2, mode="edge"), ker, mode="valid")
        s_of = {int(frs[k]): float(s) for k, s in zip(m, s_sm)}
        for f in range(fa, fb + 1):
            x = float(_eval(cx, f0, [f])[0])
            y = float(_eval(cy, f0, [f])[0])
            detected = f in det_frames
            if detected:
                conf = s_of[f] * (0.75 + 0.25 * quality)
            else:
                prev = max(g for g in det_frames if g < f)
                nxt = min(g for g in det_frames if g > f)
                if nxt - prev - 1 > p.max_gap:
                    continue
                dist = min(f - prev, nxt - f)
                decay = 0.93 if nxt - prev - 1 <= 4 else 0.85
                conf = min(s_of[prev], s_of[nxt]) * (0.75 + 0.25 * quality) * (decay ** dist)
            raw = tuple(xy[det_frames[f]]) if detected else None
            old = by_frame.get(f)
            if old is None or (detected and not old.detected) or conf > old.conf:
                by_frame[f] = TrackPoint(f, x, y, float(conf), 0 if detected else FLAG_INTERPOLATED,
                                         detected, raw, pid)
        piece_spans.append((fa, fb, cx, cy, f0, len(m)))

    # Gaps between consecutive pieces. Short gaps (<= 2) are filled linearly.
    # Longer gaps (<= max_gap) are filled with a cubic Hermite curve, but only if
    # the two pieces' extrapolations agree in the middle of the gap, i.e. no
    # bounce or hit is hidden in it.
    vmax = p.max_speed * W * 25.0 / fps
    piece_spans.sort()
    for (a0, a1, acx, acy, af0, an), (b0, b1, bcx, bcy, bf0, bn) in zip(piece_spans, piece_spans[1:]):
        gap = b0 - a1 - 1
        if gap <= 0 or gap > p.max_gap or a1 not in by_frame or b0 not in by_frame:
            continue
        A, B = by_frame[a1], by_frame[b0]
        if np.hypot(B.x - A.x, B.y - A.y) > vmax * (gap + 1):
            continue
        va = np.array([np.polyval(np.polyder(acx), a1 - af0), np.polyval(np.polyder(acy), a1 - af0)])
        vb = np.array([np.polyval(np.polyder(bcx), b0 - bf0), np.polyval(np.polyder(bcy), b0 - bf0)])
        L = b0 - a1
        PA, PB = np.array([A.x, A.y]), np.array([B.x, B.y])
        mode = "linear"
        if gap > 2:
            sp = 0.5 * (np.hypot(*va) + np.hypot(*vb))
            tol_gap = 0.008 * W + 0.25 * sp * gap
            mid = (a1 + b0) / 2
            # A well-supported piece is extrapolated with its own quadratic (gravity).
            poly_b = bn >= 6 and b1 - b0 >= 8
            poly_a = an >= 6 and a1 - a0 >= 8

            def pred_b(f):
                if poly_b:
                    return np.array([np.polyval(bcx, f - bf0), np.polyval(bcy, f - bf0)])
                return PB + vb * (f - b0)

            def pred_a(f):
                if poly_a:
                    return np.array([np.polyval(acx, f - af0), np.polyval(acy, f - af0)])
                return PA + va * (f - a1)

            # Where do the two flight lines meet? (least squares over the kink time)
            d = PA - PB - va * a1 + vb * b0
            e = va - vb
            tk = float(-(d @ e) / (e @ e)) if (e @ e) > 1e-9 else mid
            K1, K2 = PA + va * (tk - a1), PB + vb * (tk - b0)
            cosang = float(va @ vb / max(1e-9, np.hypot(*va) * np.hypot(*vb)))
            if cosang > np.cos(np.radians(20)) and \
                    np.hypot(*((PA + va * (mid - a1)) - (PB - vb * (b0 - mid)))) <= tol_gap:
                mode = "hermite"  # smooth through the gap
            elif gap <= 6 and a1 < tk < b0 and np.hypot(*(K1 - K2)) <= 0.008 * W + 0.1 * sp * gap:
                mode = "kink"  # a bounce or hit inside the gap, at time tk
                K = 0.5 * (K1 + K2)
                ev = _classify_kink(va, vb, fps, H)
                kf = int(round(tk))
            elif np.hypot(*(pred_b(a1) - PA)) <= tol_gap:
                mode = "right"  # the kink is at A (e.g. a hit): follow B's flight back to A
            elif np.hypot(*(pred_a(b0) - PB)) <= tol_gap:
                mode = "left"  # the kink is at B
            else:
                continue  # a bounce or hit hidden inside the gap: don't guess
        for f in range(a1 + 1, b0):
            u = (f - a1) / L
            if mode == "hermite":
                h00, h10 = 2 * u**3 - 3 * u**2 + 1, u**3 - 2 * u**2 + u
                h01, h11 = -2 * u**3 + 3 * u**2, u**3 - u**2
                x, y = h00 * PA + h10 * L * va + h01 * PB + h11 * L * vb
            elif mode == "kink":
                if f <= tk:
                    x, y = PA + (K - PA) * (f - a1) / (tk - a1)
                else:
                    x, y = K + (PB - K) * (f - tk) / (b0 - tk)
            elif mode == "right":
                x, y = pred_b(f) + (PA - pred_b(a1)) * (1 - u)
            elif mode == "left":
                x, y = pred_a(f) + (PB - pred_a(b0)) * u
            else:
                x, y = PA + u * (PB - PA)
            dist = min(f - a1, b0 - f)
            decay = 0.93 if gap <= 4 else 0.88
            conf = min(A.conf, B.conf) * (decay ** dist)
            if mode in ("right", "left", "kink"):
                conf *= 0.8  # the flight model changes inside the gap: less certain
            flags = FLAG_INTERPOLATED
            if mode == "kink" and f == kf:
                flags |= ev
            by_frame[f] = TrackPoint(f, float(x), float(y), conf, flags, False)

    # Events at shared piece boundaries.
    _label_events(by_frame, piece_spans, fps, H)

    pts = [q for q in by_frame.values() if q.conf >= p.min_conf]
    pts.sort(key=lambda q: q.frame)
    runs = []
    for q in pts:
        if runs and q.frame == runs[-1][1] + 1:
            runs[-1][1] = q.frame
        else:
            runs.append([q.frame, q.frame])
    return pts, [tuple(r) for r in runs], n_out


def _classify_kink(v_in, v_out, fps, H: float = 1080.0) -> int:
    """Conservative bounce/hit label for a velocity change (px/frame). Returns 0 if unsure.

    Image y grows downward, and the camera is behind the near baseline.
    * A down -> up flip with a strong speed-up is a near-player hit (4).
      A down -> up flip without one is a bounce (2).
    * A near-reversal (>= 150 deg) whose incoming ball was moving up the
      screen is a far-player hit (4).
    Everything else stays unlabelled, so misses are preferred over wrong labels.
    Tuned on one broadcast clip; treat the flags as experimental.
    """
    k_s = (1080.0 / H) * (fps / 25.0)
    vxi, vyi = float(v_in[0]) * k_s, float(v_in[1]) * k_s
    vxo, vyo = float(v_out[0]) * k_s, float(v_out[1]) * k_s
    ang = np.degrees(abs(np.arctan2(vxi * vyo - vyi * vxo, vxi * vxo + vyi * vyo)))
    sp_in, sp_out = np.hypot(vxi, vyi), np.hypot(vxo, vyo)
    if ang < 45:
        return 0
    if vyi > 3 and vyo < -3:
        return FLAG_HIT if (sp_out > 1.3 * sp_in and sp_out > 30) else FLAG_BOUNCE
    if ang >= 150 and vyi < 0:
        return FLAG_HIT
    return 0


def _label_events(by_frame, spans, fps, H: float = 1080.0):
    """Label bounces and hits at piece boundaries (kinks); see ``_classify_kink``."""
    for (a0, a1, acx, acy, af0, _an), (b0, b1, bcx, bcy, bf0, _bn) in zip(spans, spans[1:]):
        if b0 - a1 > 2 or a1 - a0 < 2 or b1 - b0 < 2:
            continue
        f = a1 if b0 <= a1 else (a1 + b0) // 2
        if f not in by_frame:
            continue
        v_in = (np.polyval(np.polyder(acx), a1 - af0), np.polyval(np.polyder(acy), a1 - af0))
        v_out = (np.polyval(np.polyder(bcx), b0 - bf0), np.polyval(np.polyder(bcy), b0 - bf0))
        by_frame[f].flags |= _classify_kink(v_in, v_out, fps, H)
