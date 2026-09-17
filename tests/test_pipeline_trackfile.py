import json

import pytest

from tbh.pipeline import trackfile as tf


def _track(rows, segments, vid="YTkyRTsiIaY"):
    return tf.build_track(video_id=vid, source_url=f"https://www.youtube.com/watch?v={vid}", width=1920,
                          height=1080, fps=25.0, segments=segments, rows=rows, detector="test")


def _row(t, x=0.5, y=0.5, flags=0, sl=0.0, sa=0.0):
    return tf.make_row(t, x, y, 0.0016, 0.9, "#101010", flags, sl, sa)


def _raw(t, x=0.5, conf=0.9, ring="#101010", flags=0, n=None):
    row = [t, x, 0.5, 0.001, conf, ring, flags, 0.0, 0.0]
    return row if n is None else row[:n]


def test_build_and_validate():
    t = _track([_row(359.04), _row(359.0, flags=1)], [{"start": 359.0, "end": 372.0}])
    assert t["schema_version"] == 1
    assert t["fields"] == ["t", "x", "y", "r", "conf", "ring", "flags", "sl", "sa"]
    assert [r[0] for r in t["frames"]] == [359.0, 359.04]  # sorted
    assert t["video"] == {"width": 1920, "height": 1080, "fps": 25.0}
    json.dumps(t)


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda t: t.update(schema_version=2), "schema_version"),
        (lambda t: t["frames"].append(_raw(360.0, x=1.5)), "x/y"),
        (lambda t: t["frames"].append(_raw(360.0, ring="red")), "ring"),
        (lambda t: t["frames"].append(_raw(360.0, conf=1.9)), "conf"),
        (lambda t: t["frames"].append(_raw(360.0, flags=8)), "flags"),
        (lambda t: t["frames"].append(_raw(380.0)), "outside"),
        (lambda t: t["frames"].insert(0, _raw(365.0)), "sorted"),
        (lambda t: t["frames"].append([360.0, 0.5, 0.5]), "length"),
        (lambda t: t["frames"].append(_raw(360.0)[:7] + [-0.1, 0.0]), "sl"),
        (lambda t: t["frames"].append(_raw(360.0)[:8] + ["up"]), "sa"),
        (lambda t: t.update(fields=["t", "x", "y", "r", "conf", "ring", "sl", "sa", "flagz"]), "missing"),
        (lambda t: t["video"].update(fps=0), "fps"),
    ],
)
def test_validate_rejects(mutate, msg):
    t = _track([_row(359.0)], [{"start": 359.0, "end": 372.0}])
    mutate(t)
    with pytest.raises(tf.TrackFileError, match=msg):
        tf.validate(t)


def _legacy(rows, segments, vid="YTkyRTsiIaY"):
    """A schema-v1 file written before sl/sa existed."""
    t = _track([], segments, vid)
    t["fields"] = ["t", "x", "y", "r", "conf", "ring", "flags"]
    t["frames"] = [_raw(tt, x=x, n=7) for tt, x in rows]
    return t


def test_optional_columns_are_optional_and_looked_up_by_name():
    legacy = _legacy([(1.0, 0.2)], [{"start": 0.0, "end": 2.0}])
    tf.validate(legacy)
    assert tf.row_dict(legacy, legacy["frames"][0])["sl"] == 0.0
    # Reordered columns plus an unknown extra column are fine.
    t = _track([], [{"start": 0.0, "end": 2.0}])
    t["fields"] = ["flags", "sa", "extra", "t", "ring", "sl", "conf", "r", "y", "x"]
    t["frames"] = [[0, 1.2, "whatever", 1.0, "#f5f5f5", 0.003, 0.8, 0.002, 0.4, 0.6]]
    tf.validate(t)
    d = tf.row_dict(t, t["frames"][0])
    assert (d["x"], d["sl"], d["sa"], d["ring"]) == (0.6, 0.003, 1.2, "#f5f5f5")
    # Sorting is checked on the named t column.
    t["frames"].append([0, 0.0, "x", 0.5, "#f5f5f5", 0.0, 0.8, 0.002, 0.4, 0.6])
    with pytest.raises(tf.TrackFileError, match="sorted"):
        tf.validate(t)


def test_merge_remaps_legacy_rows_by_name():
    old = _legacy([(1.0, 0.2), (5.0, 0.3)], [{"start": 0.0, "end": 6.0}])
    new = _track([_row(3.0, x=0.9, sl=0.004, sa=-1.0)], [{"start": 2.5, "end": 3.5}])
    m = tf.merge(old, new)
    assert m["fields"] == tf.FIELDS
    rows = [tf.row_dict(m, r) for r in m["frames"]]
    assert [(r["t"], r["x"], r["sl"], r["sa"]) for r in rows] == [
        (1.0, 0.2, 0.0, 0.0), (3.0, 0.9, 0.004, -1.0), (5.0, 0.3, 0.0, 0.0)]
    # And the other way round: new rows without sl/sa, old rows with them.
    m2 = tf.merge(new, _legacy([(1.0, 0.1)], [{"start": 0.5, "end": 1.5}]))
    assert m2["fields"] == tf.REQUIRED_FIELDS
    assert [r[0] for r in m2["frames"]] == [1.0, 3.0]
    assert all(len(r) == 7 for r in m2["frames"])


def test_merge_segments():
    segs = tf.merge_segments([{"start": 5, "end": 10}, {"start": 0, "end": 2}, {"start": 9, "end": 12}])
    assert segs == [{"start": 0.0, "end": 2.0}, {"start": 5.0, "end": 12.0}]


def test_merge_replaces_overlapping_rows_and_unions_segments():
    old = _track([_row(10.0, x=0.1), _row(11.0, x=0.1), _row(14.0, x=0.1)], [{"start": 10.0, "end": 15.0}])
    new = _track([_row(12.0, x=0.9), _row(13.0, x=0.9)], [{"start": 11.0, "end": 13.5}])
    m = tf.merge(old, new)
    assert m["segments"] == [{"start": 10.0, "end": 15.0}]
    ts = [(tf.row_dict(m, r)["t"], tf.row_dict(m, r)["x"]) for r in m["frames"]]
    # 11.0 lies inside the new segment, so it's replaced (dropped); 10.0 and 14.0 are kept.
    assert ts == [(10.0, 0.1), (12.0, 0.9), (13.0, 0.9), (14.0, 0.1)]


def test_merge_disjoint_and_other_video():
    a = _track([_row(1.0)], [{"start": 0.0, "end": 2.0}])
    b = _track([_row(5.0)], [{"start": 4.0, "end": 6.0}])
    m = tf.merge(a, b)
    assert len(m["segments"]) == 2 and len(m["frames"]) == 2
    c = _track([_row(5.0)], [{"start": 4.0, "end": 6.0}], vid="AAAAAAAAAAA")
    with pytest.raises(tf.TrackFileError):
        tf.merge(a, c)


def test_dump_load_roundtrip(tmp_path):
    t = _track([_row(359.0), _row(359.04)], [{"start": 359.0, "end": 372.0}])
    p = tmp_path / "x" / "t.json"
    tf.dump(t, p)
    assert tf.load(p) == t
    # One row per line keeps diffs readable.
    assert sum(1 for line in p.read_text().splitlines() if line.strip().startswith("[359.")) == 2


def test_empty_frames_dump(tmp_path):
    t = _track([], [{"start": 0.0, "end": 1.0}])
    tf.dump(t, tmp_path / "e.json")
    assert tf.load(tmp_path / "e.json")["frames"] == []


@pytest.mark.parametrize(
    "s, vid",
    [
        ("https://www.youtube.com/watch?v=YTkyRTsiIaY", "YTkyRTsiIaY"),
        ("https://www.youtube.com/watch?v=YTkyRTsiIaY&t=359s", "YTkyRTsiIaY"),
        ("https://youtu.be/YTkyRTsiIaY?t=5", "YTkyRTsiIaY"),
        ("https://m.youtube.com/shorts/YTkyRTsiIaY", "YTkyRTsiIaY"),
        ("https://www.youtube.com/embed/YTkyRTsiIaY", "YTkyRTsiIaY"),
        ("YTkyRTsiIaY", "YTkyRTsiIaY"),
        ("data/clips/YTkyRTsiIaY_359-372.mp4", "YTkyRTsiIaY"),
        ("https://example.com/watch?v=YTkyRTsiIaY", None),
        ("synthetic.mp4", None),
    ],
)
def test_extract_video_id(s, vid):
    assert tf.extract_video_id(s) == vid
