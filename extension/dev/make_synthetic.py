"""Synthetic clip + track for the overlay harness.

    uv run python extension/dev/make_synthetic.py

Writes extension/dev/synthetic/synthetic.mp4 (H.264, 640x360, 25 fps, 12 s;
git-ignored), synthetic.track.json and synthetic.truth.json (exposure
sub-positions per frame, for exact fit checks) (schema v1, 50 fps rows). The clip's
local time 0 corresponds to YouTube time OFFSET (100 s), so load the harness
with offset=100, which is the default for the synthetic pair.

Designed to exercise the rendering rules:
  * rows every 1/50 s, video frames every 1/25 s (every other row is between frames)
  * motion blur: each frame averages 12 sub-exposures over a 0.9-frame shutter,
    and rows carry sl/sa (streak half-length and angle) measured the same way
  * 3.00 to 3.10 s: every other row missing, gap 0.04 s <= 2.5/fps, interpolated
  * 6.00 to 6.50 s: no rows at all, ring must disappear
  * 6.80 to 7.20 s: a single isolated row at 7.00 (drawn on that frame only)
  * 8.00 to 8.50 s: conf 0.3, hidden at the default 0.5 threshold
  * 9.60 to 10.40 s: the ball holds still (sharp, sl 0, so a circle)
  * the ball crosses a white band, and the ring colour flips to #101010 there
  * bounce and hit flags on a few rows

Coordinates are edge-based: pixel column i spans [i, i+1], so x = (i + 0.5) / W
for a ball centred on pixel i.
"""

import json
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
import cv2

OUT = Path(__file__).resolve().parent / "synthetic"
W, H, FPS, DUR = 640, 360, 25, 12.0
ROW_FPS = 50
OFFSET = 100.0
BALL_R = 5.0  # px at 640 wide
SHUTTER = 0.9 / FPS  # exposure per frame, seconds
SUBSAMPLES = 12
HOLD = (9.6, 10.4)  # ball stands still


def ball_pos(t: float) -> tuple[float, float]:
    """Ball centre in edge-based pixels, with a pause during HOLD."""
    if t >= HOLD[1]:
        t -= HOLD[1] - HOLD[0]
    elif t >= HOLD[0]:
        t = HOLD[0]
    return _path(t)


def _path(t: float) -> tuple[float, float]:
    """A zig-zag across the court with bounces."""
    period = 3.0
    u = (t % period) / period
    x = 60 + (W - 120) * (u if int(t // period) % 2 == 0 else 1 - u)
    # bouncing height: |sin| arcs, 3 bounces per period
    y = H * 0.78 - abs(math.sin(u * 3 * math.pi)) * H * 0.55
    return x, y


def background() -> np.ndarray:
    img = np.zeros((H, W, 3), np.uint8)
    img[:] = (70, 120, 60)  # BGR, grass-ish
    # darker lower court
    img[H // 2 :] = (50, 95, 45)
    # white band the ball crosses (ring must turn dark here)
    img[:, 290:350] = (240, 240, 240)
    # court lines
    cv2.line(img, (40, 300), (600, 300), (235, 235, 235), 2)
    cv2.line(img, (40, 60), (600, 60), (235, 235, 235), 2)
    cv2.line(img, (320, 60), (320, 300), (200, 200, 200), 1)
    # dark crowd strip at the top
    img[:40] = (30, 30, 35)
    return img


def exposure(t: float) -> list[tuple[float, float]]:
    """Ball positions sampled across the shutter interval centred on t."""
    return [ball_pos(t + SHUTTER * ((k + 0.5) / SUBSAMPLES - 0.5)) for k in range(SUBSAMPLES)]


def streak(t: float) -> tuple[float, float, float, float]:
    """(cx, cy, sl, sa) of the blurred ball: mean position, chord half-length and angle."""
    pts = exposure(t)
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    (x0, y0), (x1, y1) = ball_pos(t - SHUTTER / 2), ball_pos(t + SHUTTER / 2)
    dx, dy = x1 - x0, y1 - y0
    return cx, cy, math.hypot(dx, dy) / 2, math.atan2(dy, dx) if (dx or dy) else 0.0


def ring_for(x: float) -> str:
    return "#101010" if 280 <= x <= 360 else "#f5f5f5"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg not found; install it to build the synthetic clip")
    bg = background()
    video = OUT / "synthetic.mp4"
    proc = subprocess.Popen(
        [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
         "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "fast",
         "-crf", "20", "-g", "25", "-movflags", "+faststart", str(video)],
        stdin=subprocess.PIPE,
    )
    n_frames = int(DUR * FPS)
    for k in range(n_frames):
        t = k / FPS
        acc = np.zeros((H, W, 3), np.float32)
        for x, y in exposure(t):
            f = bg.copy()
            # cv2 centres pixel i at i, so shift the edge-based position by -0.5.
            cv2.circle(f, (int(round((x - 0.5) * 16)), int(round((y - 0.5) * 16))), int(BALL_R * 16), (60, 235, 220), -1, cv2.LINE_AA, shift=4)
            acc += f
        frame = (acc / SUBSAMPLES + 0.5).astype(np.uint8)
        cv2.putText(frame, f"{t + OFFSET:7.2f}", (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 1, cv2.LINE_AA)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit("ffmpeg failed")

    rows = []
    for i in range(int(DUR * ROW_FPS)):
        t = i / ROW_FPS
        if 6.0 <= t < 6.5:
            continue
        if 3.0 < t < 3.1 and i % 2 == 1:
            continue
        if 6.8 <= t <= 7.2 and i != 350:  # keep only the row at 7.00
            continue
        x, y, sl, sa = streak(t)
        conf = 0.3 if 8.0 <= t < 8.5 else 0.92
        flags = 0
        if abs(y - H * 0.78) < 2:
            flags |= 2
        if i % 150 == 0:
            flags |= 4
        rows.append([round(t + OFFSET, 3), round(x / W, 6), round(y / H, 6), round(BALL_R / W, 6), conf, ring_for(x), flags,
                     round(sl / W, 6), round(sa, 4)])

    track = {
        "schema_version": 1,
        "video_id": "SYNTHETIC00",
        "source_url": "synthetic",
        "created_at": "2026-09-17T00:00:00Z",
        "generator": {"name": "make_synthetic.py", "version": "0.1.0", "detector": "none"},
        "video": {"width": W, "height": H, "fps": float(ROW_FPS)},
        "segments": [{"start": OFFSET, "end": OFFSET + DUR}],
        "fields": ["t", "x", "y", "r", "conf", "ring", "flags", "sl", "sa"],
        "frames": rows,
    }
    (OUT / "synthetic.track.json").write_text(json.dumps(track, separators=(",", ":")))
    # Ground truth for the harness: exact ball centres over each frame's exposure.
    truth = {
        "fps": FPS, "offset": OFFSET, "width": W, "height": H, "r": BALL_R,
        # Sub-exposure centres plus the continuous shutter endpoints.
        "frames": [
            [[round(x, 3), round(y, 3)] for x, y in exposure(k / FPS) + [ball_pos(k / FPS - SHUTTER / 2), ball_pos(k / FPS + SHUTTER / 2)]]
            for k in range(n_frames)
        ],
    }
    (OUT / "synthetic.truth.json").write_text(json.dumps(truth, separators=(",", ":")))
    print(video, OUT / "synthetic.track.json", len(rows), "rows")


if __name__ == "__main__":
    main()
