import numpy as np
import pytest

from tbh.pipeline.trackfile import FLAG_BOUNCE, FLAG_HIT, FLAG_INTERPOLATED
from tbh.pipeline.track import TrackParams, remove_static, track

W, H, FPS = 1920, 1080, 25.0


def _flight(n=40, x0=400.0, y0=300.0, vx=12.0, vy=-6.0, g=0.8):
    f = np.arange(n)
    return np.stack([x0 + vx * f, y0 + vy * f + 0.5 * g * f * f], 1)


def _cands(xy, drop=(), score=0.95, n_total=None):
    n_total = n_total or len(xy)
    c = [[] for _ in range(n_total)]
    for i, (x, y) in enumerate(xy):
        if i not in drop:
            c[i].append((float(x), float(y), score))
    return c


def _pts(res):
    return {p.frame: p for p in res.points}


def test_clean_flight_is_tracked_exactly():
    xy = _flight()
    res = track(_cands(xy), [], W, H, FPS)
    pts = _pts(res)
    assert len(pts) == len(xy)
    for f, p in pts.items():
        assert p.detected and p.flags & FLAG_INTERPOLATED == 0
        assert np.hypot(p.x - xy[f, 0], p.y - xy[f, 1]) < 1.0
        assert p.conf > 0.5


@pytest.mark.parametrize("gap", [1, 3, 5, 8])
def test_gap_fill_inside_smooth_flight(gap):
    xy = _flight(n=50)
    drop = set(range(20, 20 + gap))
    pts = _pts(track(_cands(xy, drop), [], W, H, FPS))
    for f in drop:
        assert f in pts, f"frame {f} not filled"
        assert pts[f].flags & FLAG_INTERPOLATED
        assert not pts[f].detected
        assert np.hypot(pts[f].x - xy[f, 0], pts[f].y - xy[f, 1]) < 4.0
    # Confidence decays towards the middle of the gap.
    if gap >= 3:
        mid = 20 + gap // 2
        assert pts[mid].conf < pts[19].conf


def test_long_gap_not_filled():
    xy = _flight(n=50)
    drop = set(range(20, 32))  # 12 frames > max_gap
    pts = _pts(track(_cands(xy, drop), [], W, H, FPS))
    assert not any(f in pts for f in drop)


def _v_flight(kind):
    f = np.arange(40)
    if kind == "bounce":
        x = 500 + 10 * f
        y = np.where(f < 20, 300 + 15 * f, 600 - 12 * (f - 20))
    else:  # far-player hit: the ball comes up the screen and goes back down, reversed
        x = np.where(f < 20, 500 + 10 * f, 700 - 14 * (f - 20))
        y = np.where(f < 20, 700 - 12 * f, 460 + 16 * (f - 20))
    return np.stack([x, y], 1).astype(float)


@pytest.mark.parametrize("kind", ["bounce", "hit"])
def test_short_gap_with_a_kink_is_filled_with_a_corner(kind):
    xy = _v_flight(kind)
    drop = set(range(17, 23))  # 6 frames, the kink at frame 20
    pts = _pts(track(_cands(xy, drop), [], W, H, FPS))
    for k in drop:
        assert k in pts and pts[k].flags & FLAG_INTERPOLATED
        assert np.hypot(pts[k].x - xy[k, 0], pts[k].y - xy[k, 1]) < 3.0  # the corner is not rounded off
    labelled = [k for k in drop if pts[k].flags & (FLAG_BOUNCE | FLAG_HIT)]
    assert labelled and all(abs(k - 20) <= 1 for k in labelled)
    if kind == "hit":
        assert pts[labelled[0]].flags & FLAG_HIT


@pytest.mark.parametrize("kind", ["bounce", "hit"])
def test_long_gap_with_a_kink_is_not_guessed(kind):
    xy = _v_flight(kind)
    drop = set(range(16, 24))  # 8 frames
    pts = _pts(track(_cands(xy, drop), [], W, H, FPS))
    assert not any(k in pts for k in drop)
    assert 15 in pts and 24 in pts


def test_bounce_is_labelled_and_kink_kept():
    f = np.arange(40)
    y = np.where(f <= 20, 300 + 15 * f, 600 - 10 * (f - 20)).astype(float)
    xy = np.stack([500 + 6 * f, y], 1).astype(float)
    pts = _pts(track(_cands(xy), [], W, H, FPS))
    assert np.hypot(pts[20].x - xy[20, 0], pts[20].y - xy[20, 1]) < 3.0  # corner not smoothed away
    flagged = [k for k, p in pts.items() if p.flags & FLAG_BOUNCE]
    assert flagged and all(abs(k - 20) <= 1 for k in flagged)


def test_implausible_jump_is_rejected():
    xy = _flight(n=40)
    c = _cands(xy)
    c[15] = [(1700.0, 900.0, 0.99)]  # teleport
    pts = _pts(track(c, [], W, H, FPS))
    assert 15 in pts  # filled by interpolation instead
    assert pts[15].flags & FLAG_INTERPOLATED
    assert np.hypot(pts[15].x - xy[15, 0], pts[15].y - xy[15, 1]) < 4.0


def test_distractor_candidate_is_ignored():
    xy = _flight(n=40)
    c = _cands(xy)
    for k in range(0, 40, 3):  # a second, weaker, moving thing elsewhere
        c[k].append((1500.0 - 5 * k, 800.0, 0.5))
    pts = _pts(track(c, [], W, H, FPS))
    assert all(p.x < 1000 for p in pts.values())


def test_static_false_positive_removed():
    xy = _flight(n=60)
    c = _cands(xy)
    for k in (3, 4, 20, 21, 22, 40, 55):  # a logo that flickers in and out
        c[k].append((1710.0, 150.0, 0.8))
    cleaned, n = remove_static(c, W, TrackParams())
    assert n == 7
    assert all(all(abs(x - 1710) > 1 for x, _, _ in cs) for cs in cleaned)
    pts = _pts(track(c, [], W, H, FPS))
    assert all(abs(p.x - 1710) > 50 for p in pts.values())


def test_slow_ball_is_not_mistaken_for_static():
    # The ball near the apex of a lob barely moves for several frames.
    f = np.arange(30)
    xy = np.stack([800 + 1.5 * f, 400 + 0.3 * (f - 15) ** 2], 1)
    res = track(_cands(xy), [], W, H, FPS)
    assert res.static_removed == 0
    assert len(res.points) == 30


def test_isolated_detection_dropped():
    c = [[] for _ in range(30)]
    c[10] = [(900.0, 500.0, 0.99)]
    assert track(c, [], W, H, FPS).points == []


def test_cut_resets_tracking():
    a = _flight(n=20)
    b = _flight(n=20, x0=1500, y0=800, vx=-10, vy=-4)
    xy = np.concatenate([a, b])
    res = track(_cands(xy), [20], W, H, FPS)
    pts = _pts(res)
    assert len(pts) == 40
    assert all(not (p.flags & FLAG_INTERPOLATED) for p in pts.values())
    assert any(r[0] == 20 for r in res.runs)


def test_fps_scaling_50fps():
    xy = _flight(n=60, vx=6.0, vy=-3.0, g=0.2)  # the same flight at double rate
    drop = set(range(30, 36))
    pts = _pts(track(_cands(xy, drop), [], W, H, 50.0))
    assert all(k in pts for k in drop)
