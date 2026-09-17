import numpy as np
import pytest

from tbh.pipeline.preview import TrackSampler, draw_ring, draw_sample, ring_geometry, ring_shape


def _track(rows, fps=25.0):
    return {"fields": ["t", "x", "y", "r", "conf", "ring", "flags"], "video": {"fps": fps},
            "frames": [[t, x, 0.5, 0.002, c, ring, 0] for t, x, c, ring in rows]}


def _streak_track(rows, fps=25.0):
    """rows: (t, x, sl, sa); columns deliberately reordered."""
    return {"fields": ["sa", "t", "sl", "x", "y", "r", "conf", "ring", "flags"], "video": {"fps": fps},
            "frames": [[sa, t, sl, x, 0.5, 0.002, 0.9, "#101010", 0] for t, x, sl, sa in rows]}


def test_sampler_interpolates_close_rows():
    s = TrackSampler(_track([(10.00, 0.1, 0.9, "#101010"), (10.08, 0.3, 0.9, "#f5f5f5")]))
    v = s.at(10.02)  # 2 frames apart <= 2.5 frames: interpolate
    assert v["x"] == pytest.approx(0.15)
    assert v["ring"] == "#101010"  # nearest row
    assert s.at(10.06)["ring"] == "#f5f5f5"


def test_sampler_nearest_within_one_frame_else_nothing():
    s = TrackSampler(_track([(10.00, 0.1, 0.9, "#101010"), (10.20, 0.3, 0.9, "#101010")]))
    assert s.at(10.00)["x"] == pytest.approx(0.1)
    assert s.at(10.02)["x"] == pytest.approx(0.1)  # half a frame: still the same frame
    assert s.at(10.03) is None  # more than half a frame from the nearest row
    assert s.at(10.04) is None  # the next frame has no row: draw nothing
    assert s.at(10.10) is None  # 2.5 frames from both rows
    assert s.at(9.98)["x"] == pytest.approx(0.1)
    assert s.at(9.97) is None


def test_sampler_hides_low_conf():
    s = TrackSampler(_track([(10.00, 0.1, 0.3, "#101010")]))
    assert s.at(10.0) is None
    assert TrackSampler(_track([(10.00, 0.1, 0.3, "#101010")]), conf_threshold=0.0).at(10.0) is not None


def test_ring_geometry_min_stroke_keeps_inner_edge():
    inner, outer = ring_geometry(20.0, css_to_px=1.0)  # 0.2*20 = 4 px > 1.5
    assert (inner, outer) == pytest.approx((20.0, 24.0))
    inner, outer = ring_geometry(3.0, css_to_px=1.5)  # 0.6 px < 1.5 css = 2.25 px
    assert inner == pytest.approx(3.0)
    assert outer == pytest.approx(5.25)


def test_draw_ring_does_not_cover_the_ball():
    img = np.zeros((40, 40, 3), np.uint8)
    draw_ring(img, 20.0, 20.0, 6.0, 8.0, (255, 255, 255))
    yy, xx = np.mgrid[0:40, 0:40]
    d = np.hypot(xx + 0.5 - 20, yy + 0.5 - 20)
    assert img[d < 5.3].max() == 0  # the ball area stays untouched
    assert img[(d > 6.6) & (d < 7.4)].min() > 200  # the stroke is solid
    assert img[d > 9.0].max() == 0


def test_sampler_streak_columns_by_name():
    s = TrackSampler(_streak_track([(10.00, 0.1, 0.004, 0.5), (10.04, 0.2, 0.008, 2.0)]))
    v = s.at(10.01)
    assert v["x"] == pytest.approx(0.125)
    assert v["sl"] == pytest.approx(0.005)  # interpolated
    assert v["sa"] == 0.5  # from the nearest row
    assert s.at(10.03)["sa"] == 2.0
    # Files without sl/sa: circles.
    v = TrackSampler(_track([(10.0, 0.1, 0.9, "#101010")])).at(10.0)
    assert v["sl"] == 0.0 and v["sa"] == 0.0


def test_ring_shape_threshold_and_circle_only():
    assert ring_shape(4.0, 1.9) == 0.0  # < 0.5 r: circle
    assert ring_shape(4.0, 2.0) == 2.0
    assert ring_shape(4.0, 12.0, circle_only=True) == 0.0


def _dist_to_segment(xx, yy, cx, cy, sl, sa):
    ux, uy = np.cos(sa), np.sin(sa)
    t = np.clip((xx - cx) * ux + (yy - cy) * uy, -sl, sl)
    return np.hypot(xx - cx - t * ux, yy - cy - t * uy)


@pytest.mark.parametrize("sa", [0.0, 0.7, np.pi / 2, -2.5])
def test_stadium_ring_surrounds_the_streak_without_covering_it(sa):
    img = np.zeros((80, 80, 3), np.uint8)
    r, sl = 4.0, 12.0
    inner, outer = ring_geometry(r, css_to_px=1.5)  # stroke clamped to 2.25 px
    draw_ring(img, 40.0, 40.0, inner, outer, (255, 255, 255), sl, sa)
    yy, xx = np.mgrid[0:80, 0:80] + 0.5
    d = _dist_to_segment(xx, yy, 40, 40, sl, sa)
    lit = img[..., 0] > 0
    assert not lit[d < r - 0.8].any()  # the whole streak stays uncovered
    assert (img[..., 0][(d > r + 0.8) & (d < outer - 0.8)] > 200).all()  # a closed, solid pill
    assert not lit[d > outer + 0.8].any()  # thin: nothing beyond the outer edge


def test_draw_sample_uses_stadium_only_for_real_streaks():
    W = H = 100
    base = {"x": 0.5, "y": 0.5, "r": 0.04, "sa": 0.0, "ring": "#f5f5f5"}
    a = np.zeros((H, W, 3), np.uint8)
    draw_sample(a, dict(base, sl=0.01), W, H, css_to_px=1.0)  # sl 1px < 2px: circle
    b = np.zeros((H, W, 3), np.uint8)
    draw_ring(b, 50, 50, 4.0, 5.5, (245, 245, 245))
    assert np.array_equal(a, b)
    c = np.zeros((H, W, 3), np.uint8)
    draw_sample(c, dict(base, sl=0.1), W, H, css_to_px=1.0)  # 10 px streak: stadium
    assert c[50, 62:67, 0].max() > 200  # the ring passes beyond the cap at x = 50 + 10 + 4
    d = np.zeros((H, W, 3), np.uint8)
    draw_sample(d, dict(base, sl=0.1), W, H, css_to_px=1.0, circle_only=True)
    assert np.array_equal(d, b)
