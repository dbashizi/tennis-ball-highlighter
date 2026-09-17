import numpy as np
import pytest

from tbh.pipeline import ringcolor as rc


def test_contrast_is_symmetric_and_bounded():
    assert rc.contrast(1.0, 0.0) == pytest.approx(21.0)
    assert rc.contrast(0.2, 0.5) == rc.contrast(0.5, 0.2)
    assert rc.contrast(0.3, 0.3) == pytest.approx(1.0)


@pytest.mark.parametrize(
    "L, expected",
    [(0.0, rc.WHITE), (0.02, rc.WHITE), (0.9, rc.BLACK), (0.6, rc.BLACK)],
)
def test_best_candidate_black_or_white(L, expected):
    assert rc.best_candidate(L)[0] == expected


def test_magenta_needs_1_2x_advantage(monkeypatch):
    # With the real candidate luminances magenta can never win (it sits between
    # black and white), so fake a very dark "black" and a very light "white".
    lum = dict(rc._CAND_L)
    lum[rc.BLACK] = 0.30
    lum[rc.WHITE] = 0.32
    lum[rc.MAGENTA] = 0.95
    monkeypatch.setattr(rc, "_CAND_L", lum)
    cand, c = rc.best_candidate(0.0)
    assert cand == rc.MAGENTA
    # vs L=0: white gives 0.37/0.05 = 7.4, so magenta needs >= 8.88 (L >= 0.394).
    lum[rc.MAGENTA] = 0.38  # 8.6: not enough
    assert rc.best_candidate(0.0)[0] == rc.WHITE
    lum[rc.MAGENTA] = 0.40  # 9.0: enough
    assert rc.best_candidate(0.0)[0] == rc.MAGENTA


def test_magenta_never_wins_with_real_candidates():
    for L in np.linspace(0, 1, 101):
        assert rc.best_candidate(float(L))[0] in (rc.BLACK, rc.WHITE)


def test_hysteresis_requires_three_frames_and_15_percent():
    dark, light = 0.01, 0.9  # -> white, -> black
    seq = [dark, dark, light, light, light, light]
    out = rc.choose_ring_colors(seq)
    assert out[:4] == [rc.WHITE] * 4  # two light frames are not enough
    assert out[4] == rc.BLACK  # third consecutive frame switches
    assert out[5] == rc.BLACK


def test_hysteresis_ignores_flicker_and_small_advantages():
    dark, light = 0.01, 0.9
    flicker = [dark] + [light, dark] * 5
    assert set(rc.choose_ring_colors(flicker)) == {rc.WHITE}
    # Mid-grey (L ~ 0.18, where black and white contrast are equal): the new
    # candidate is better, but by less than 15%, so it never switches.
    seq = [0.17] + [0.19] * 10
    assert rc.best_candidate(0.17)[0] == rc.WHITE
    assert rc.best_candidate(0.19)[0] == rc.BLACK
    assert set(rc.choose_ring_colors(seq)) == {rc.WHITE}


def test_hysteresis_reset_at_breaks():
    dark, light = 0.01, 0.9
    out = rc.choose_ring_colors([dark, dark, light], breaks={2})
    assert out == [rc.WHITE, rc.WHITE, rc.BLACK]


def test_annulus_excludes_the_ball():
    # A white ball (r=5) on a black background: the ring must be white.
    img = np.zeros((60, 60, 3), np.uint8)
    yy, xx = np.mgrid[0:60, 0:60]
    img[np.hypot(xx + 0.5 - 30, yy + 0.5 - 30) <= 5.5] = 255
    L = rc.background_luminance(img, 30, 30, 5.0)
    assert L < 0.01
    assert rc.best_candidate(L)[0] == rc.WHITE
    # A dark ball on a white court: ring black.
    img2 = np.full((60, 60, 3), 255, np.uint8)
    img2[np.hypot(xx + 0.5 - 30, yy + 0.5 - 30) <= 5.5] = 0
    assert rc.best_candidate(rc.background_luminance(img2, 30, 30, 5.0))[0] == rc.BLACK


def test_median_is_taken_in_linear_rgb():
    # Half pure black, half pure white annulus: the linear median is black or white,
    # never the sRGB mid-grey. Use 3 colours so the median is well defined.
    px = np.array([[0, 0, 0], [128, 128, 128], [255, 255, 255]], float) / 255
    med = np.median(rc.srgb_to_linear(px), axis=0)
    assert rc.luminance_linear(med) == pytest.approx(rc.srgb_to_linear(np.array([128 / 255]))[0], rel=1e-6)
    assert rc.hex_luminance("#ffffff") == pytest.approx(1.0)
    assert rc.hex_luminance("#000000") == pytest.approx(0.0)
