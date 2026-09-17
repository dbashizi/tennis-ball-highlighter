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
    rows = track["frames"]
    assert len(rows) >= 0.8 * N
    for t, x, y, r, conf, ring, flags in rows:
        i = int(round((t - OFFSET) * FPS))
        assert abs((t - OFFSET) * FPS - i) < 1e-6  # t is on the source frame grid
        tx, ty = ball_xy(i)
        assert abs(x * W - tx) < 3.0, (i, x * W, tx)
        assert abs(y * H - ty) < 3.0, (i, y * H, ty)
        assert r * W == pytest.approx(R, rel=0.35)
        assert ring in ("#101010", "#f5f5f5")
        assert abs(x * W - 600) > 20 or abs(y * H - 30) > 20  # never on the logo
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
    ts = [r[0] for r in track["frames"]]
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
    rows = track["frames"]
    assert len(rows) >= n  # roughly one row per field
    for t, x, y, *_ in rows:
        k = (t - OFFSET) * FPS  # in source frames; fields sit on half frames
        assert abs(k * 2 - round(k * 2)) < 1e-6
        tx, ty = ball_xy(k)
        assert abs(x * W - tx) < 4.0 and abs(y * H - ty) < 4.0, (k, x * W, tx, y * H, ty)
