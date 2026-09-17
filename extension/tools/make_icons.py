"""Generate the extension icons: an optic-yellow tennis ball inside a ring.

Usage (from the repo root):  uv run python extension/tools/make_icons.py
Draws at 8x and downsamples with INTER_AREA for clean anti-aliasing.
"""

from pathlib import Path

import cv2
import numpy as np

OUT = Path(__file__).resolve().parent.parent / "icons"
SIZES = (16, 32, 48, 128)
SS = 8  # supersampling factor

BALL = (58, 242, 214)  # BGR of #d6f23a
SEAM = (230, 251, 247)  # BGR of #f7fbe6
RING = (255, 255, 255)
BACK = (15, 15, 15)  # #0f0f0f disc behind the ring, so it reads on light toolbars


def _layer(n: int) -> np.ndarray:
    return np.zeros((n, n, 4), np.float32)


def _over(dst: np.ndarray, src: np.ndarray) -> np.ndarray:
    a = src[..., 3:4] / 255.0
    out = dst.copy()
    out[..., :3] = src[..., :3] * a + dst[..., :3] * (1 - a)
    out[..., 3:4] = src[..., 3:4] + dst[..., 3:4] * (1 - a)
    return out


def draw(size: int) -> np.ndarray:
    n = size * SS
    c = (n // 2, n // 2)
    s = n / 24.0  # 24-unit design grid, same as the in-page SVG
    small = size <= 16  # thicker ring, no seams, so it stays legible

    def disc(r, color):
        m = np.zeros((n, n), np.uint8)
        cv2.circle(m, c, int(r * s), 255, -1, cv2.LINE_AA)
        layer = _layer(n)
        layer[..., :3] = color
        layer[..., 3] = m
        return layer

    ring_r, ring_w = (10.0, 2.8) if small else (10.2, 1.9)
    ball_r = 6.8 if small else 7.0

    img = disc(12, BACK)
    ring = np.zeros((n, n), np.uint8)
    cv2.circle(ring, c, int(ring_r * s), 255, int(ring_w * s), cv2.LINE_AA)
    lay = _layer(n)
    lay[..., :3] = RING
    lay[..., 3] = ring
    img = _over(img, lay)

    ball = np.zeros((n, n, 3), np.uint8)
    ball[:] = BALL
    if not small:
        for sign in (-1, 1):
            centre = (int(c[0] + sign * 9.4 * s), c[1])
            cv2.ellipse(ball, centre, (int(5.0 * s), int(6.0 * s)), 0, 0, 360, SEAM, max(1, int(1.0 * s)), cv2.LINE_AA)
    mask = np.zeros((n, n), np.uint8)
    cv2.circle(mask, c, int(ball_r * s), 255, -1, cv2.LINE_AA)
    lay = _layer(n)
    lay[..., :3] = ball
    lay[..., 3] = mask
    img = _over(img, lay)

    out = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
    return np.clip(out, 0, 255).astype(np.uint8)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        path = OUT / f"icon{size}.png"
        cv2.imwrite(str(path), draw(size))
        print(path)


if __name__ == "__main__":
    main()
