"""End-to-end on synthetic videos generated at test time (no network)."""

import json
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest

from tbh.pipeline import trackfile
from tbh.pipeline.api import process_video
from tbh.pipeline.detect import ensure_tracknet_weights
from tbh.pipeline.video import ffmpeg_bin, open_video

W, H, FPS, N = 640, 360, 25, 50
R = 4.0
OFFSET = 100.0


def ball_xy(i):
    """Parabolic flight, bouncing at frame 30."""
    x = 80 + 9.0 * i
    if i <= 30:
        y = 60 + 8.0 * i
    else:
        y = 300 - 6.0 * (i - 30)
    return x, y


def _write(path: Path, frames, fps=FPS, extra=()):
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{W}x{H}", "-r", str(fps), "-i", "-", *extra, "-c:v", "libx264", "-crf", "12",
           "-pix_fmt", "yuv420p", str(path)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for f in frames:
        p.stdin.write(f.tobytes())
    p.stdin.close()
    assert p.wait() == 0


def _frame(i, t_draw=None):
    img = np.empty((H, W, 3), np.uint8)
    img[:] = (70, 130, 90)  # green court (BGR)
    cv2.line(img, (0, 330), (W, 330), (230, 230, 230), 3)  # a court line
    cv2.circle(img, (600, 30), 6, (255, 255, 255), -1)  # a static white logo
    x, y = ball_xy(i if t_draw is None else t_draw)
    ss = 8
    cv2.circle(img, (int(x * ss), int(y * ss)), int(R * ss), (40, 235, 215), -1, cv2.LINE_AA, 3)
    return img


@pytest.fixture(scope="module")
def synthetic_video(tmp_path_factory):
    d = tmp_path_factory.mktemp("synth")
    path = d / "synthetic.mp4"
    _write(path, [_frame(i) for i in range(N)])
    return path


def _check_track(track, path):
    trackfile.validate(track)
    assert track["segments"] == [{"start": OFFSET, "end": OFFSET + N / FPS}]
    rows = [trackfile.row_dict(track, r) for r in track["frames"]]
    assert len(rows) >= 0.8 * N
    for d in rows:
        t, x, y, r, ring = d["t"], d["x"], d["y"], d["r"], d["ring"]
        i = int(round((t - OFFSET) * FPS))
        assert abs((t - OFFSET) * FPS - i) < 1e-6  # t is on the source frame grid
        tx, ty = ball_xy(i)
        assert abs(x * W - tx) < 3.0, (i, x * W, tx)
        assert abs(y * H - ty) < 3.0, (i, y * H, ty)
        assert r * W == pytest.approx(R, rel=0.35)
        assert ring in ("#101010", "#f5f5f5")
        assert abs(x * W - 600) > 20 or abs(y * H - 30) > 20  # never on the logo
        # The synthetic ball is sharp: any streak estimate stays short.
        assert d["sl"] * W < R
    assert track["fields"] == trackfile.FIELDS
    assert path.exists()  # a local input file is never deleted


def test_e2e_classical(synthetic_video, tmp_path):
    stages = []
    track = process_video(str(synthetic_video), OFFSET, OFFSET + N / FPS, tmp_path / "work",
                          lambda s, f, m: stages.append(s), keep_media=False,
                          time_offset=OFFSET, detector="classical", video_id="synthetic01")
    _check_track(track, synthetic_video)
    assert track["generator"]["detector"] == "classical-diff"
    assert track["video_id"] == "synthetic01"
    order = [s for i, s in enumerate(stages) if i == 0 or stages[i - 1] != s]
    assert order == ["download", "decode", "detect", "track", "finalize", "done"]
    json.dumps(track)


def test_e2e_subrange_uses_source_timeline(synthetic_video, tmp_path):
    track = process_video(str(synthetic_video), OFFSET + 0.4, OFFSET + 1.2, tmp_path, None,
                          time_offset=OFFSET, detector="classical")
    ts = [trackfile.row_dict(track, r)["t"] for r in track["frames"]]
    assert ts and min(ts) >= OFFSET + 0.4 - 1e-9 and max(ts) < OFFSET + 1.2
    assert track["segments"] == [{"start": OFFSET + 0.4, "end": OFFSET + 1.2}]


def test_e2e_out_of_range(synthetic_video, tmp_path):
    with pytest.raises(ValueError):
        process_video(str(synthetic_video), 5.0, 6.0, tmp_path, None, time_offset=OFFSET, detector="classical")


@pytest.mark.skipif(ensure_tracknet_weights(download=False) is None, reason="TrackNet weights not present")
def test_e2e_tracknet_smoke(synthetic_video, tmp_path):
    # TrackNet was trained on broadcast tennis, not on synthetic dots. We only
    # check that the path runs and yields a valid file.
    track = process_video(str(synthetic_video), None, None, tmp_path, None, time_offset=OFFSET,
                          detector="tracknet", device="cpu")
    trackfile.validate(track)
    assert track["generator"]["detector"] == "tracknet-v2"


def test_e2e_motion_blurred_ball_gets_streak_fields(tmp_path):
    # A fast ball rendered with motion blur: 18 px per frame, shutter open half the frame.
    n = 40

    def xy(t):
        return 60 + 18.0 * t, 80 + 5.0 * t

    frames = []
    for i in range(n):
        img = np.empty((H, W, 3), np.float32)
        img[:] = (70, 130, 90)
        acc = np.zeros((H, W), np.float32)
        subs = 12
        for k in range(subs):
            x, y = xy(i - 0.25 + 0.5 * k / (subs - 1))
            m = np.zeros((H, W), np.float32)
            cv2.circle(m, (int(x * 8), int(y * 8)), int(R * 8), 1.0, -1, cv2.LINE_AA, 3)
            acc += m / subs
        a = np.clip(acc * 2.0, 0, 1)[..., None]  # a streak is about as bright as the ball
        img = img * (1 - a) + np.array((40, 235, 215), np.float32) * a
        frames.append(np.clip(img, 0, 255).astype(np.uint8))
    path = tmp_path / "blur.mp4"
    _write(path, frames)
    track = process_video(str(path), None, None, tmp_path / "w", None, time_offset=0.0, detector="classical")
    rows = [trackfile.row_dict(track, r) for r in track["frames"]]
    assert len(rows) >= 0.7 * n
    # Half of the distance travelled while the shutter is open. The caps are
    # covered for only part of the exposure, so the *visible* streak is shorter.
    geo_sl = 0.25 * np.hypot(18.0, 5.0)
    true_sa = np.arctan2(5.0, 18.0)
    sls = np.array([d["sl"] * W for d in rows])
    assert 0.5 * geo_sl <= np.median(sls) <= 1.1 * geo_sl
    bg = np.array((70, 130, 90), np.float32)
    for d in rows:
        # Oriented along the motion (not flipped by pi).
        assert abs((d["sa"] - true_sa + np.pi) % (2 * np.pi) - np.pi) < np.radians(12)
        assert d["r"] * W == pytest.approx(R, rel=0.4)
        # Every clearly visible streak pixel lies inside the ring's inner edge.
        i = int(round(d["t"] * FPS))
        diff = np.abs(frames[i].astype(np.float32) - bg).max(axis=2)
        ys, xs = np.nonzero(diff > 0.5 * diff.max())
        ux, uy = np.cos(d["sa"]), np.sin(d["sa"])
        cx, cy, sl, r = d["x"] * W, d["y"] * H, d["sl"] * W, d["r"] * W
        px, py = xs + 0.5 - cx, ys + 0.5 - cy
        tt = np.clip(px * ux + py * uy, -sl, sl)
        dist = np.hypot(px - tt * ux, py - tt * uy)
        assert dist.max() <= r + 1.5, (i, dist.max(), r)


def test_interlaced_source_is_field_doubled(tmp_path):
    # Build a 25i clip: each frame carries the ball at t and t+half a frame in
    # alternate fields.
    n = 20
    frames = []
    for i in range(n):
        a = _frame(i)          # top field: time i
        b = _frame(i, i + 0.5)  # bottom field: time i + 0.5
        for img, tt in ((a, i), (b, i + 0.5)):  # a fast-moving texture band shows combing
            xs = (np.arange(W) + int(12 * tt)) % 16 < 8
            img[4:20, :560][:, xs[:560]] = 20
        f = a.copy()
        f[1::2] = b[1::2]
        frames.append(f)
    path = tmp_path / "interlaced.mp4"
    _write(path, frames, extra=("-flags", "+ilme+ildct", "-x264-params", "tff=1", "-field_order", "tt"))
    info = open_video(path)
    assert info.interlaced, info.idet
    assert info.analysis_fps == pytest.approx(2 * FPS)
    assert len(info.pts) == 2 * n
    assert info.pts[1] - info.pts[0] == pytest.approx(0.5 / FPS)
    track = process_video(str(path), None, None, tmp_path / "w", None, time_offset=OFFSET, detector="classical")
    assert track["video"]["fps"] == pytest.approx(2 * FPS)
    rows = [trackfile.row_dict(track, r) for r in track["frames"]]
    assert len(rows) >= n  # roughly one row per field
    for d in rows:
        t, x, y = d["t"], d["x"], d["y"]
        k = (t - OFFSET) * FPS  # in source frames; fields sit on half frames
        assert abs(k * 2 - round(k * 2)) < 1e-6
        tx, ty = ball_xy(k)
        assert abs(x * W - tx) < 4.0 and abs(y * H - ty) < 4.0, (k, x * W, tx, y * H, ty)
