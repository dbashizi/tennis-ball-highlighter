"""Normalise YouTube URLs (and, for testing, local file paths) to video IDs."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

_YT_ID = re.compile(r"[A-Za-z0-9_-]{11}")
VIDEO_ID_RE = re.compile(r"^(?:[A-Za-z0-9_-]{11}|local-[0-9a-f]{11})$")

_YT_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
_PATH_PREFIXES = ("shorts", "live", "embed", "v", "e")


class InvalidVideoURL(ValueError):
    pass


def local_files_allowed() -> bool:
    return os.environ.get("TBH_ALLOW_LOCAL_FILES") == "1"


def is_valid_video_id(video_id: str) -> bool:
    return bool(VIDEO_ID_RE.match(video_id))


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def normalize(url: str) -> tuple[str, str]:
    """Return ``(video_id, canonical_url)`` or raise :class:`InvalidVideoURL`.

    The canonical URL of a YouTube video is its plain ``watch?v=`` URL (no
    timestamp or playlist parameters). Local files keep their resolved path.
    """
    url = (url or "").strip()
    if not url:
        raise InvalidVideoURL("empty url")

    local = _local_path(url)
    if local is not None:
        path = str(local)
        return "local-" + hashlib.sha1(path.encode()).hexdigest()[:11], path

    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https"):
        raise InvalidVideoURL("not a YouTube URL")

    candidate = None
    segments = [s for s in parts.path.split("/") if s]
    if host in ("youtu.be", "www.youtu.be"):
        candidate = segments[0] if segments else None
    elif host in _YT_HOSTS:
        if segments == ["watch"]:
            candidate = (parse_qs(parts.query).get("v") or [None])[0]
        elif len(segments) >= 2 and segments[0] in _PATH_PREFIXES:
            candidate = segments[1]
    else:
        raise InvalidVideoURL("not a YouTube URL")

    if not candidate or not _YT_ID.fullmatch(candidate):
        raise InvalidVideoURL("could not find a YouTube video id in the URL")
    return candidate, watch_url(candidate)


def _local_path(url: str) -> Path | None:
    if not local_files_allowed():
        return None
    raw = url[len("file://"):] if url.startswith("file://") else url
    if not raw.startswith(("/", "~")):
        return None
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise InvalidVideoURL(f"local file not found: {path}")
    return path
