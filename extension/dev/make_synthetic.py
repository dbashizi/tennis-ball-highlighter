"""Synthetic clip + track for the overlay harness.

    uv run python extension/dev/make_synthetic.py

Writes extension/dev/synthetic/synthetic.mp4 (H.264, 640x360, 25 fps, 12 s;
git-ignored) and synthetic.track.json (schema v1, 50 fps rows). The clip's
local time 0 corresponds to YouTube time OFFSET (100 s), so load the harness
with offset=100, which is the default for the synthetic pair.

Designed to exercise the rendering rules:
  * rows every 1/50 s, video frames every 1/25 s (every other row is between frames)
  * 3.00 to 3.10 s: every other row missing, gap 0.04 s <= 2.5/fps, interpolated
  * 6.00 to 6.50 s: no rows at all, ring must disappear
  * 8.00 to 8.50 s: conf 0.3, hidden at the default 0.5 threshold
  * the ball crosses a white band, and the ring colour flips to #101010 there
  * bounce and hit flags on a few rows
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


def ball_pos(t: float) -> tuple[float, float]:
    """Ball centre in pixels: a zig-zag across the court with bounces."""
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
        frame = bg.copy()
        x, y = ball_pos(t)
        cv2.circle(frame, (int(round(x * 16)), int(round(y * 16))), int(BALL_R * 16), (60, 235, 220), -1, cv2.LINE_AA, shift=4)
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
        x, y = ball_pos(t)
        conf = 0.3 if 8.0 <= t < 8.5 else 0.92
        flags = 0
        if abs(y - H * 0.78) < 2:
            flags |= 2
        if i % 150 == 0:
            flags |= 4
        rows.append([round(t + OFFSET, 3), round(x / W, 5), round(y / H, 5), round(BALL_R / W, 5), conf, ring_for(x), flags])

    track = {
        "schema_version": 1,
        "video_id": "SYNTHETIC00",
        "source_url": "synthetic",
        "created_at": "2026-09-17T00:00:00Z",
        "generator": {"name": "make_synthetic.py", "version": "0.1.0", "detector": "none"},
        "video": {"width": W, "height": H, "fps": float(ROW_FPS)},
        "segments": [{"start": OFFSET, "end": OFFSET + DUR}],
        "fields": ["t", "x", "y", "r", "conf", "ring", "flags"],
        "frames": rows,
    }
    (OUT / "synthetic.track.json").write_text(json.dumps(track, separators=(",", ":")))
    print(video, OUT / "synthetic.track.json", len(rows), "rows")


if __name__ == "__main__":
    main()
