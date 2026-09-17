"""Build, validate and merge schema-v1 track dicts (see docs/track-format.md)."""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tbh import __version__

SCHEMA_VERSION = 1
FIELDS = ["t", "x", "y", "r", "conf", "ring", "flags"]
FLAG_INTERPOLATED = 1
FLAG_BOUNCE = 2
FLAG_HIT = 4
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class TrackFileError(ValueError):
    pass


def extract_video_id(url_or_path: str) -> str | None:
    """YouTube id from a watch/short/embed URL, a bare id, or a file name like ``<id>_359-372.mp4``."""
    s = str(url_or_path).strip()
    if _ID.match(s):
        return s
    if "://" in s:
        u = urlparse(s)
        host = (u.hostname or "").lower()
        if host.endswith("youtu.be"):
            cand = u.path.lstrip("/").split("/")[0]
            return cand if _ID.match(cand) else None
        if "youtube" in host:
            q = parse_qs(u.query).get("v")
            if q and _ID.match(q[0]):
                return q[0]
            parts = [p for p in u.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v") and _ID.match(parts[1]):
                return parts[1]
        return None
    stem = Path(s).name.split(".")[0]
    m = re.match(r"^([A-Za-z0-9_-]{11})(?:$|[_.])", stem)
    return m.group(1) if m else None


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_row(t: float, x: float, y: float, r: float, conf: float, ring: str, flags: int) -> list:
    return [round(float(t), 3), round(float(x), 5), round(float(y), 5), round(float(r), 5),
            round(float(conf), 2), ring, int(flags)]


def build_track(*, video_id: str, source_url: str | None, width: int, height: int, fps: float,
                segments: list[dict], rows: list[list], detector: str,
                extra_generator: dict | None = None) -> dict:
    gen = {"name": "tbh-pipeline", "version": __version__, "detector": detector}
    if extra_generator:
        gen.update(extra_generator)
    track = {
        "schema_version": SCHEMA_VERSION,
        "video_id": video_id,
        "source_url": source_url,
        "created_at": now_iso(),
        "generator": gen,
        "video": {"width": int(width), "height": int(height), "fps": round(float(fps), 6)},
        "segments": merge_segments(segments),
        "fields": list(FIELDS),
        "frames": sorted(rows, key=lambda r: r[0]),
    }
    validate(track)
    return track


def validate(track: dict) -> None:
    """Strict validation of a schema-v1 track; raises TrackFileError."""
    def fail(msg):
        raise TrackFileError(msg)

    if not isinstance(track, dict):
        fail("track must be an object")
    if track.get("schema_version") != SCHEMA_VERSION:
        fail("schema_version must be 1")
    if not isinstance(track.get("video_id"), str) or not track["video_id"]:
        fail("video_id must be a non-empty string")
    v = track.get("video")
    if not isinstance(v, dict):
        fail("video must be an object")
    for k in ("width", "height", "fps"):
        if not isinstance(v.get(k), (int, float)) or isinstance(v.get(k), bool) or v[k] <= 0:
            fail(f"video.{k} must be a positive number")
    segs = track.get("segments")
    if not isinstance(segs, list):
        fail("segments must be a list")
    for s in segs:
        if not (isinstance(s, dict) and isinstance(s.get("start"), (int, float))
                and isinstance(s.get("end"), (int, float)) and s["end"] >= s["start"] >= 0):
            fail(f"bad segment {s!r}")
    if track.get("fields") != FIELDS:
        fail(f"fields must be {FIELDS}")
    frames = track.get("frames")
    if not isinstance(frames, list):
        fail("frames must be a list")
    prev_t = -1.0
    for i, row in enumerate(frames):
        if not isinstance(row, list) or len(row) != len(FIELDS):
            fail(f"row {i}: wrong length")
        t, x, y, r, conf, ring, flags = row
        for name, val in (("t", t), ("x", x), ("y", y), ("r", r), ("conf", conf)):
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                fail(f"row {i}: {name} must be a number")
        if t < prev_t:
            fail(f"row {i}: rows must be sorted by t")
        prev_t = t
        if not (0 <= x <= 1 and 0 <= y <= 1):
            fail(f"row {i}: x/y out of 0..1")
        if not (0 < r < 0.5):
            fail(f"row {i}: r out of range")
        if not (0 <= conf <= 1):
            fail(f"row {i}: conf out of 0..1")
        if not (isinstance(ring, str) and _HEX.match(ring)):
            fail(f"row {i}: ring must be #rrggbb")
        if not isinstance(flags, int) or isinstance(flags, bool) or flags < 0 or flags > 7:
            fail(f"row {i}: flags must be an int bitmask 0..7")
        if segs and not any(s["start"] - 1e-6 <= t <= s["end"] + 1e-6 for s in segs):
            fail(f"row {i}: t={t} outside all segments")


def merge_segments(segments: list[dict]) -> list[dict]:
    out: list[dict] = []
    for s in sorted(segments, key=lambda s: (s["start"], s["end"])):
        if out and s["start"] <= out[-1]["end"] + 1e-6:
            out[-1]["end"] = max(out[-1]["end"], s["end"])
        else:
            out.append({"start": float(s["start"]), "end": float(s["end"])})
    return out


def merge(old: dict, new: dict) -> dict:
    """Merge ``new`` into ``old``: segments are unioned, and old rows inside new segments are replaced."""
    if old.get("video_id") != new.get("video_id"):
        raise TrackFileError("cannot merge tracks of different videos")
    new_segs = new["segments"]

    def covered(t):
        return any(s["start"] - 1e-6 <= t <= s["end"] + 1e-6 for s in new_segs)

    kept = [r for r in old["frames"] if not covered(r[0])]
    merged = dict(new)
    merged["segments"] = merge_segments([dict(s) for s in old["segments"]] + [dict(s) for s in new_segs])
    merged["frames"] = sorted(kept + list(new["frames"]), key=lambda r: r[0])
    validate(merged)
    return merged


def dump(track: dict, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # One row per line: compact but diff-friendly.
    head = {k: v for k, v in track.items() if k != "frames"}
    s = json.dumps(head, indent=2)[:-2]
    rows = ",\n".join("    " + json.dumps(r, separators=(",", ":")) for r in track["frames"])
    s += ',\n  "frames": [\n' + rows + ("\n  ]\n}\n" if rows else "]\n}\n")
    json.loads(s)  # sanity
    path.write_text(s)


def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())
