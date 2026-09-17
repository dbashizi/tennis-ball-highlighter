import cv2
import numpy as np
import pytest

from tbh.pipeline.radius import R_MAX_FRAC, R_MIN_FRAC, measure_ball, smooth_radii

W = 1920


def _scene(r, cx=32.3, cy=30.7, streak=0.0, angle=30.0, seed=0, size=64):
    rng = np.random.default_rng(seed)
    bg = np.empty((size, size, 3), np.float32)
    bg[:] = (70, 130, 90)  # BGR green court
    bg += rng.normal(0, 1.5, bg.shape)
    ss = 8
    big = np.zeros((size * ss, size * ss), np.float32)
    # Motion blur: average of disks along the streak direction.
    n = max(1, int(streak) + 1)
    for k in range(n):
        off = (k / max(1, n - 1) - 0.5) * streak if n > 1 else 0.0
        x = (cx + off * np.cos(np.radians(angle))) * ss
        y = (cy + off * np.sin(np.radians(angle))) * ss
        cv2.circle(big, (int(round(x)), int(round(y))), int(round(r * ss)), 1.0 / n, -1)
    alpha = cv2.resize(big, (size, size), interpolation=cv2.INTER_AREA)
    if n > 1:
        alpha = alpha / alpha.max()  # a streak is as bright as the ball
    ball = np.array((60, 235, 215), np.float32)  # BGR yellow
    frame = bg * (1 - alpha[..., None]) + ball * alpha[..., None]
    frame = cv2.GaussianBlur(frame, (0, 0), 0.6)
    return np.clip(frame, 0, 255).astype(np.uint8), np.clip(bg, 0, 255).astype(np.uint8)


@pytest.mark.parametrize("r", [3.0, 4.5, 7.0])
def test_round_ball_radius_and_centre(r):
    frame, bg = _scene(r)
    m = measure_ball(frame, bg, 30.0, 30.0, W)
    assert m is not None
    assert m.r == pytest.approx(r, rel=0.2)
    assert m.x == pytest.approx(32.3, abs=0.6)
    assert m.y == pytest.approx(30.7, abs=0.6)


@pytest.mark.parametrize("angle", [0.0, 35.0, 90.0])
def test_streak_uses_minor_axis(angle):
    r = 3.5
    frame, bg = _scene(r, streak=20.0, angle=angle, cx=32.0, cy=32.0)
    m0 = measure_ball(frame, bg, 32.0, 32.0, W, search_frac=0.01)
    assert m0 is not None
    m = measure_ball(frame, bg, 32.0, 32.0, W, search_frac=0.01)
    assert m is not None
    assert m.r == pytest.approx(r, rel=0.3)  # not the streak length
    assert m.major > 2.5 * m.r
    assert m.x == pytest.approx(32.0, abs=1.0)
    # Half-length from the centre to the cap centre (the synthetic streak spans +-10 px).
    assert m.sl == pytest.approx(10.0, rel=0.2)
    diff = (m.angle - np.radians(angle)) % np.pi
    assert min(diff, np.pi - diff) < np.radians(5)


def test_round_ball_has_no_streak():
    frame, bg = _scene(4.0)
    m = measure_ball(frame, bg, 30.0, 30.0, W)
    assert m.sl < 1.0


def test_no_ball_no_measurement():
    frame, bg = _scene(0.0)
    assert measure_ball(bg.copy(), bg, 32, 32, W) is None


def test_border_touching_blob_rejected():
    frame, bg = _scene(3.0)
    frame[:, :10] = 255  # a player-sized change at the crop border
    frame[28:36, 5:40] = 255
    assert measure_ball(frame, bg, 30, 30, W) is None


def test_smooth_radii_is_smooth_clamped_and_depth_aware():
    n = 120
    frames = np.arange(n)
    ys = np.linspace(300, 900, n)  # far to near
    truth = 2.0 + 3.0 * ys / 1080  # grows towards the camera
    rng = np.random.default_rng(1)
    meas = truth * (1 + rng.normal(0, 0.15, n))
    meas[::7] = np.nan  # missing measurements
    meas[10] = 30.0  # a wild outlier
    w = np.where(np.isfinite(meas), 1.0, 0.0)
    out = smooth_radii(frames, ys, meas, w, W, 1080, np.zeros(n, int))
    assert np.all(out >= R_MIN_FRAC * W - 1e-9) and np.all(out <= R_MAX_FRAC * W + 1e-9)
    assert np.max(np.abs(np.diff(out))) < 0.15  # no frame-to-frame jumps
    assert np.median(np.abs(out - truth) / truth) < 0.1
    assert out[-1] > out[0]  # the near ball is bigger
