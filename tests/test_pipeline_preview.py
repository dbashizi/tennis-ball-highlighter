import numpy as np
import pytest

from tbh.pipeline.preview import TrackSampler, draw_ring, ring_geometry


def _track(rows, fps=25.0):
    return {"fields": ["t", "x", "y", "r", "conf", "ring", "flags"], "video": {"fps": fps},
            "frames": [[t, x, 0.5, 0.002, c, ring, 0] for t, x, c, ring in rows]}


def test_sampler_interpolates_close_rows():
    s = TrackSampler(_track([(10.00, 0.1, 0.9, "#101010"), (10.08, 0.3, 0.9, "#f5f5f5")]))
    v = s.at(10.02)  # 2 frames apart <= 2.5 frames: interpolate
    assert v["x"] == pytest.approx(0.15)
    assert v["ring"] == "#101010"  # nearest row
    assert s.at(10.06)["ring"] == "#f5f5f5"


def test_sampler_nearest_within_one_frame_else_nothing():
    s = TrackSampler(_track([(10.00, 0.1, 0.9, "#101010"), (10.20, 0.3, 0.9, "#101010")]))
    assert s.at(10.03)["x"] == pytest.approx(0.1)  # less than 1 frame from the nearest row
    assert s.at(10.04) is None  # exactly one frame: the row belongs to the previous frame
    assert s.at(10.10) is None  # 2.5 frames from both rows
    assert s.at(9.97)["x"] == pytest.approx(0.1)
    assert s.at(9.95) is None  # 1.25 frames away
    assert s.at(9.90) is None


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
