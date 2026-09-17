"""Validation and merging of schema-v1 tracking files (see docs/track-format.md)."""

from __future__ import annotations

import copy
from numbers import Real

REQUIRED_FIELDS = ("t", "x", "y", "r", "conf", "ring", "flags")


class InvalidTrack(ValueError):
    pass


def _is_num(v) -> bool:
    return isinstance(v, Real) and not isinstance(v, bool)


def validate_track(track) -> None:
    """Basic structural checks for a schema v1 track; raises InvalidTrack."""
    if not isinstance(track, dict):
        raise InvalidTrack("track must be an object")
    if track.get("schema_version") != 1:
        raise InvalidTrack("schema_version must be 1")
    if not isinstance(track.get("video_id"), str):
        raise InvalidTrack("video_id must be a string")
    video = track.get("video")
    if not isinstance(video, dict) or not _is_num(video.get("fps")) or video["fps"] <= 0:
        raise InvalidTrack("video.fps must be a positive number")

    segments = track.get("segments")
    if not isinstance(segments, list):
        raise InvalidTrack("segments must be a list")
    for s in segments:
        if not (isinstance(s, dict) and _is_num(s.get("start")) and _is_num(s.get("end"))
                and s["end"] >= s["start"]):
            raise InvalidTrack(f"bad segment: {s!r}")

    fields = track.get("fields")
    if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
        raise InvalidTrack("fields must be a list of strings")
    missing = [f for f in REQUIRED_FIELDS if f not in fields]
    if missing:
        raise InvalidTrack(f"fields missing: {', '.join(missing)}")

    frames = track.get("frames")
    if not isinstance(frames, list):
        raise InvalidTrack("frames must be a list")
    ti = fields.index("t")
    for i, row in enumerate(frames):
        if not isinstance(row, list) or len(row) != len(fields) or not _is_num(row[ti]):
            raise InvalidTrack(f"bad frame row {i}")


def merge_segments(segments: list[dict]) -> list[dict]:
    """Union of time ranges, sorted, with overlapping/touching ranges joined."""
    out: list[dict] = []
    for s in sorted(segments, key=lambda s: (s["start"], s["end"])):
        if out and s["start"] <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], s["end"])
        else:
            out.append({"start": s["start"], "end": s["end"]})
    return out


def merge_tracks(old: dict, new: dict) -> dict:
    """Merge ``new`` into ``old``.

    Segments become the union of both. Old rows that fall inside any of the new
    track's segments are dropped and replaced by the new rows. Metadata
    (generator, video, created_at, fields order) comes from ``new``.
    """
    merged = copy.deepcopy(new)
    fields = new["fields"]
    new_segs = new["segments"]

    def covered(t: float) -> bool:
        return any(s["start"] <= t <= s["end"] for s in new_segs)

    old_fields = old["fields"]
    remap = old_fields != fields
    ti = old_fields.index("t")
    kept = []
    for row in old["frames"]:
        if covered(row[ti]):
            continue
        if remap:
            by_name = dict(zip(old_fields, row))
            row = [by_name.get(f) for f in fields]
        kept.append(row)

    t_new = fields.index("t")
    merged["frames"] = sorted(kept + list(new["frames"]), key=lambda r: r[t_new])
    merged["segments"] = merge_segments(list(old["segments"]) + list(new_segs))
    merged["video_id"] = old.get("video_id", new["video_id"])
    return merged
