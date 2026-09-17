import numpy as np
import pytest

from tbh.pipeline.refine import estimate_exposure, smooth_streaks

W = 1920


def _run(n=30, vx=20.0, vy=-10.0, sl_true=None, meas_every=1, noise=0.0, flip=True, seed=0):
    rng = np.random.default_rng(seed)
    frames = np.arange(n)
    pieces = [0] * n
    v = np.tile([vx, vy], (n, 1)).astype(float)
    speed = np.hypot(vx, vy)
    sl_true = 0.3 * speed if sl_true is None else sl_true
    ang = np.arctan2(vy, vx)
    sl_meas = np.full(n, np.nan)
    ang_meas = np.full(n, np.nan)
    w = np.zeros(n)
    for i in range(0, n, meas_every):
        sl_meas[i] = sl_true * (1 + noise * rng.normal())
        a = ang + np.radians(3) * rng.normal() * (noise > 0)
        ang_meas[i] = (a + (np.pi if flip and i % 2 else 0.0)) % np.pi  # ambiguous by pi
        w[i] = 1.0
    r = np.full(n, 3.0)
    return smooth_streaks(frames, pieces, r, v, sl_meas, w, ang_meas, w.copy(), W), sl_true, ang


def test_angle_follows_motion_despite_pi_ambiguity():
    for vx, vy in [(20, -10), (-15, 25), (-30, -2), (5, 30)]:
        (sl, sa, k), sl_true, ang = _run(vx=vx, vy=vy)
        err = np.abs((sa - ang + np.pi) % (2 * np.pi) - np.pi)
        assert err.max() < 1e-6


def test_sl_smoothed_and_close_to_measurements():
    (sl, sa, k), sl_true, _ = _run(noise=0.25)
    assert np.median(np.abs(sl - sl_true)) < 0.12 * sl_true
    assert np.std(np.diff(sl)) < 0.2 * sl_true


def test_unmeasured_rows_use_exposure_model():
    (sl, sa, k), sl_true, ang = _run(meas_every=3)
    assert k == pytest.approx(0.3, abs=0.02)
    assert np.allclose(sl, sl_true, rtol=0.05)


def test_physical_cap_and_slow_ball():
    (sl, sa, k), *_ = _run(vx=2.0, vy=0.0, sl_true=20.0)  # impossible for a slow ball
    assert sl.max() <= 0.75 * 2.0 + 0.5 * 3.0 + 1e-9
    (sl, sa, k), *_ = _run(vx=0.0, vy=0.0, sl_true=0.0)
    assert np.all(sl == 0)


def test_estimate_exposure_needs_enough_data():
    assert estimate_exposure(np.array([np.nan]), np.array([0.0]), np.array([10.0])) == 0.25
    sp = np.full(20, 10.0)
    assert estimate_exposure(np.full(20, 4.0), np.ones(20), sp) == pytest.approx(0.4)
