import copy

import pytest

from tbh.service.merge import InvalidTrack, merge_segments, merge_tracks, validate_track

FIELDS = ["t", "x", "y", "r", "conf", "ring", "flags"]


def track(segments, rows, fields=FIELDS):
    return {
        "schema_version": 1,
        "video_id": "YTkyRTsiIaY",
        "video": {"width": 640, "height": 360, "fps": 25.0},
        "segments": [{"start": a, "end": b} for a, b in segments],
        "fields": fields,
        "frames": rows,
    }


def row(t, x):
    return [t, x, 0.5, 0.01, 0.9, "#101010", 0]


def test_merge_segments():
    segs = [{"start": 5, "end": 8}, {"start": 0, "end": 2}, {"start": 7, "end": 9},
            {"start": 2, "end": 3}, {"start": 20, "end": 21}]
    assert merge_segments(segs) == [
        {"start": 0, "end": 3}, {"start": 5, "end": 9}, {"start": 20, "end": 21}]


def test_new_rows_replace_old_rows_inside_new_segments():
    old = track([(0, 10)], [row(t, "old") for t in (1, 4, 5, 6, 9)])
    new = track([(5, 7), (20, 22)], [row(t, "new") for t in (5.5, 21)])
    merged = merge_tracks(old, new)
    assert merged["segments"] == [{"start": 0, "end": 10}, {"start": 20, "end": 22}]
    assert [(r[0], r[1]) for r in merged["frames"]] == [
        (1, "old"), (4, "old"), (5.5, "new"), (9, "old"), (21, "new")]


def test_merge_does_not_mutate_inputs_and_remaps_fields():
    old_fields = ["x", "t", "y", "r", "conf", "ring", "flags"]
    old = track([(0, 2)], [[0.3, 1.0, 0.5, 0.01, 0.9, "#f5f5f5", 1]], fields=old_fields)
    new = track([(3, 4)], [row(3.5, 0.7)])
    snapshot = copy.deepcopy((old, new))
    merged = merge_tracks(old, new)
    assert (old, new) == snapshot
    assert merged["fields"] == FIELDS
    assert merged["frames"][0] == [1.0, 0.3, 0.5, 0.01, 0.9, "#f5f5f5", 1]
    validate_track(merged)


def test_validate_accepts_good_track():
    validate_track(track([(0, 1)], [row(0.5, 0.1)]))


@pytest.mark.parametrize("mutate", [
    lambda t: t.update(schema_version=2),
    lambda t: t.pop("video_id"),
    lambda t: t.update(video={"fps": 0}),
    lambda t: t.update(segments=[{"start": 5, "end": 1}]),
    lambda t: t.update(segments=[{"start": "a", "end": 1}]),
    lambda t: t.update(fields=["t", "x"]),
    lambda t: t.update(frames=[[0.5, 0.1]]),
    lambda t: t.update(frames=[["x", 0.1, 0.5, 0.01, 0.9, "#101010", 0]]),
    lambda t: t.update(frames={}),
])
def test_validate_rejects(mutate):
    t = track([(0, 1)], [row(0.5, 0.1)])
    mutate(t)
    with pytest.raises(InvalidTrack):
        validate_track(t)


def test_validate_rejects_non_dict():
    with pytest.raises(InvalidTrack):
        validate_track([])
