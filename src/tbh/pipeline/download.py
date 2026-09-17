"""Download only the requested time range of a YouTube video, and verify its timing.

yt-dlp's ``--download-sections`` with ``--force-keyframes-at-cuts`` has ffmpeg
seek to ``start`` and re-encode. The resulting file starts at pts 0, and pts 0 is
the first source frame at or after ``start``. So the original timestamp of a
clip frame is ``start + pts``. ``verify_clip_offset`` checks this. It reads a
few seconds of the stream around ``start`` with ``-copyts`` (original
timestamps) and matches the clip's first frames against it by pixel MSE.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .video import ffmpeg_bin

FORMAT = ("bv*[height<=1080][vcodec^=avc1]/bv*[height<=1080]/"
          "b[height<=1080]/bv*/b")
REENCODE_ARGS = ["-crf", "14", "-preset", "veryfast"]


@dataclass
class Download:
    path: Path
    start: float  # nominal start of the clip in the source timeline
    end: float | None
    stream_url: str | None
    format_id: str | None
    width: int | None
    height: int | None
    fps: float | None
    title: str | None
    duration: float | None  # full source duration
    created: list[Path] = field(default_factory=list)


def download_section(url: str, start: float | None, end: float | None, out_dir: Path,
                     progress: Callable[[str, float, str], None] | None = None) -> Download:
    import yt_dlp
    from yt_dlp.utils import download_range_func

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    before = set(out_dir.iterdir())

    def hook(d):
        if not progress:
            return
        if d.get("status") == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate")
            frac = (d.get("downloaded_bytes", 0) / tot) if tot else 0.0
            if not tot and d.get("elapsed"):
                frac = min(0.9, d["elapsed"] / 60.0)
            progress("download", min(0.95, frac), f"{d.get('_percent_str', '').strip()} downloading")
        elif d.get("status") == "finished":
            progress("download", 0.97, "download finished, finalising")

    tag = f"{int(start or 0)}-{int(end) if end is not None else 'end'}"
    opts = {
        "format": FORMAT,
        "outtmpl": str(out_dir / f"%(id)s_{tag}.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "progress_hooks": [hook],
        "ffmpeg_location": ffmpeg_bin(),
    }
    if start is not None or end is not None:
        s = float(start or 0.0)
        e = float(end) if end is not None else float("inf")
        opts["download_ranges"] = download_range_func(None, [(s, e)])
        opts["force_keyframes_at_cuts"] = True
        opts["external_downloader_args"] = {"ffmpeg_o": REENCODE_ARGS}
    if progress:
        progress("download", 0.0, "resolving video")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = [Path(d["filepath"]) for d in info.get("requested_downloads", []) if d.get("filepath")]
    created = sorted(set(out_dir.iterdir()) - before)
    if not files:
        vids = [p for p in created if p.suffix in (".mp4", ".webm", ".mkv")]
        if not vids:
            raise RuntimeError("download produced no video file")
        files = vids
    fmt = info
    if info.get("requested_formats"):
        fmt = info["requested_formats"][0]
    return Download(
        path=files[0], start=float(start or 0.0), end=end,
        stream_url=fmt.get("url") or info.get("url"), format_id=info.get("format_id"),
        width=fmt.get("width"), height=fmt.get("height"), fps=fmt.get("fps"),
        title=info.get("title"), duration=info.get("duration"), created=created or files,
    )


def _gray_frames(args: list[str], n: int, copyts: bool, size=(160, 90)):
    w, h = size
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostats", "-nostdin"]
    if copyts:
        cmd.append("-copyts")
    cmd += args + ["-frames:v", str(n), "-vf", f"scale={w}:{h},showinfo", "-fps_mode", "passthrough",
                   "-an", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    p = subprocess.run(cmd, capture_output=True, timeout=180)
    pts = [float(v) for v in re.findall(r"pts_time:([-0-9.]+)", p.stderr.decode(errors="replace"))]
    a = np.frombuffer(p.stdout, np.uint8)
    k = len(a) // (w * h)
    a = a[: k * w * h].reshape(k, h, w).astype(np.float32)
    return a, np.array(pts[:k])


def verify_clip_offset(stream_url: str, clip_path: Path, nominal: float, fps: float,
                       n_match: int = 8, window: float = 2.0) -> dict:
    """Match the clip's first frames in the source stream (original timestamps).

    Returns {"verified": bool, "offset": float, "nominal": float, "delta": float,
    "mse_best": float, "mse_next": float, "method": str}. ``offset`` is the
    source time of clip pts 0.
    """
    out = {"verified": False, "offset": nominal, "nominal": nominal, "delta": 0.0,
           "method": "ffmpeg -copyts frame match"}
    try:
        clip, cpts = _gray_frames(["-i", str(clip_path)], n_match, copyts=False)
        ss = max(0.0, nominal - window)
        n_src = int((2 * window + 1) * fps) + n_match
        src, spts = _gray_frames(["-ss", f"{ss:.3f}", "-i", stream_url], n_src, copyts=True)
        if len(clip) < 2 or len(src) < n_match + 2:
            out["error"] = "not enough frames decoded"
            return out
        n = min(n_match, len(clip))
        errs = []
        for o in range(len(src) - n + 1):
            errs.append(float(np.mean([np.mean((src[o + k] - clip[k]) ** 2) for k in range(n)])))
        errs = np.array(errs)
        order = np.argsort(errs)
        best = int(order[0])
        nxt = float(errs[order[1]]) if len(order) > 1 else float("inf")
        offset = float(spts[best] - (cpts[0] if len(cpts) else 0.0))
        out.update(offset=round(offset, 6), delta=round(offset - nominal, 6),
                   mse_best=round(float(errs[best]), 3), mse_next=round(nxt, 3))
        # A clear match: well below the runner-up (usually an adjacent frame).
        out["verified"] = bool(errs[best] < 0.5 * nxt and errs[best] < 30.0)
    except Exception as exc:  # network or decoding issues: keep the nominal offset
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out
