import pytest

from tbh.pipeline.timeutil import format_time, parse_time


@pytest.mark.parametrize(
    "value, expected",
    [
        ("359", 359.0),
        ("359.5", 359.5),
        (359, 359.0),
        (12.25, 12.25),
        ("5:59", 359.0),
        ("6:12", 372.0),
        ("05:59.5", 359.5),
        ("1:00:00", 3600.0),
        ("0:05:59", 359.0),
        (" 5:59 ", 359.0),
        (None, None),
        ("", None),
    ],
)
def test_parse_time(value, expected):
    assert parse_time(value) == expected


@pytest.mark.parametrize("value", ["5:60", "abc", "1:2:3:4", "-3", -1, "1:75:00", "5:"])
def test_parse_time_rejects(value):
    with pytest.raises(ValueError):
        parse_time(value)


def test_format_roundtrip():
    for v in (0.0, 59.5, 359.04, 3725.25):
        assert parse_time(format_time(v)) == pytest.approx(v, abs=1e-3)
