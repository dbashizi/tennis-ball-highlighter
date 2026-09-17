"""Ring colour selection (docs/track-format.md, "Ring colour rule").

1. Take the median colour, in linear RGB, of the annulus 1.3r..2.5r around the ball.
   Compute its relative luminance L.
2. Pick the candidate with the highest WCAG contrast against L. Black and white
   are preferred; magenta wins only if its contrast is at least 1.2x theirs.
3. Hysteresis: switch only if the new candidate's contrast beats the current
   colour's by at least 15% on 3 consecutive frames.

Note: #101010 and #f5f5f5 bracket magenta's luminance (0.285), so under a
pure-luminance contrast measure one of them always beats magenta by far more
than 1.2x. Magenta is therefore never chosen in practice. It is implemented
anyway, to follow the spec.
"""

from __future__ import annotations

import numpy as np

BLACK = "#101010"
WHITE = "#f5f5f5"
MAGENTA = "#ff00ff"
CANDIDATES = (BLACK, WHITE, MAGENTA)
MAGENTA_ADVANTAGE = 1.2
SWITCH_RATIO = 1.15
SWITCH_FRAMES = 3


def srgb_to_linear(c: np.ndarray) -> np.ndarray:
    c = np.asarray(c, dtype=np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def hex_to_rgb01(h: str) -> np.ndarray:
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float64) / 255.0


def luminance_linear(rgb_lin: np.ndarray) -> float:
    r, g, b = rgb_lin
    return float(0.2126 * r + 0.7152 * g + 0.0722 * b)


def hex_luminance(h: str) -> float:
    return luminance_linear(srgb_to_linear(hex_to_rgb01(h)))


_CAND_L = {c: hex_luminance(c) for c in CANDIDATES}


def contrast(l1: float, l2: float) -> float:
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def annulus_mask(shape: tuple[int, int], cx: float, cy: float, r: float,
                 inner: float = 1.3, outer: float = 2.5) -> np.ndarray:
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w]
    d = np.hypot(xx + 0.5 - cx, yy + 0.5 - cy)
    ri, ro = inner * r, outer * r
    # Keep at least a 1px-wide band so tiny radii still sample something.
    if ro - ri < 1.0:
        ro = ri + 1.0
    return (d >= ri) & (d <= ro)


def background_luminance(img_bgr: np.ndarray, cx: float, cy: float, r: float) -> float | None:
    """Median linear-RGB colour of the annulus, as luminance. (cx, cy) are pixel coordinates in img."""
    ro = max(2.5 * r, 1.3 * r + 1.0)
    x0, x1 = int(max(0, np.floor(cx - ro - 1))), int(min(img_bgr.shape[1], np.ceil(cx + ro + 1)))
    y0, y1 = int(max(0, np.floor(cy - ro - 1))), int(min(img_bgr.shape[0], np.ceil(cy + ro + 1)))
    if x1 <= x0 or y1 <= y0:
        return None
    crop = img_bgr[y0:y1, x0:x1]
    m = annulus_mask(crop.shape[:2], cx - x0, cy - y0, r)
    if m.sum() < 4:
        return None
    px = crop[m][:, ::-1].astype(np.float64) / 255.0  # BGR -> RGB
    med = np.median(srgb_to_linear(px), axis=0)
    return luminance_linear(med)


def best_candidate(L: float) -> tuple[str, float]:
    """Max-contrast candidate for background luminance L, with the black/white preference."""
    cb = contrast(_CAND_L[BLACK], L)
    cw = contrast(_CAND_L[WHITE], L)
    cm = contrast(_CAND_L[MAGENTA], L)
    bw, cbw = (BLACK, cb) if cb >= cw else (WHITE, cw)
    if cm >= MAGENTA_ADVANTAGE * cbw:
        return MAGENTA, cm
    return bw, cbw


class RingHysteresis:
    """Stateful colour chooser. Call ``reset()`` at camera cuts and track breaks."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.current: str | None = None
        self.pending: str | None = None
        self.count = 0

    def update(self, L: float | None) -> str:
        if L is None:
            return self.current or WHITE
        cand, c_new = best_candidate(L)
        if self.current is None:
            self.current = cand
            self.pending, self.count = None, 0
            return self.current
        if cand == self.current:
            self.pending, self.count = None, 0
            return self.current
        c_cur = contrast(_CAND_L[self.current], L)
        if c_new >= SWITCH_RATIO * c_cur:
            if cand == self.pending:
                self.count += 1
            else:
                self.pending, self.count = cand, 1
            if self.count >= SWITCH_FRAMES:
                self.current = cand
                self.pending, self.count = None, 0
        else:
            self.pending, self.count = None, 0
        return self.current


def choose_ring_colors(lums: list[float | None], breaks: set[int] | None = None) -> list[str]:
    """Apply the hysteresis to a sequence of luminances. A reset happens before each index in ``breaks``."""
    hy = RingHysteresis()
    out = []
    breaks = breaks or set()
    for i, L in enumerate(lums):
        if i in breaks:
            hy.reset()
        out.append(hy.update(L))
    return out
