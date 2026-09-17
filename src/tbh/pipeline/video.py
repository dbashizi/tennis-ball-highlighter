"""Decoding helpers built on ffmpeg/ffprobe: probing, interlace detection,
and streaming raw frames with exact timestamps."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import numpy as np


def ffmpeg_bin() -> str:
    return shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


def ffprobe_bin() -> str:
    return shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"


@dataclass
class VideoInfo:
    path: Path
    width: int
    height: int
    fps: float  # source frame rate (frames per second)
    duration: float
    nb_frames: int
    codec: str
    field_order: str = "unknown"
    interlaced: bool = False
    idet: dict = field(default_factory=dict)
    # Presentation timestamps (seconds, in the file's own timeline) of every
    # frame we will decode; analysis frame rate after optional deinterlacing.
    pts: np.ndarray | None = None
    analysis_fps: float = 0.0

    def as_dict(self) -> dict:
        return {
            "width": self.width, "height": self.height, "fps": self.fps,
            "duration": self.duration, "nb_frames": self.nb_frames, "codec": self.codec,
            "field_order": self.field_order, "interlaced": self.interlaced,
            "idet": self.idet, "analysis_fps": self.analysis_fps,
        }


def _parse_rate(s: str) -> float:
    if "/" in s:
        a, b = s.split("/")
        return float(a) / float(b) if float(b) else 0.0
    return float(s)


def probe(path: str | Path) -> VideoInfo:
    path = Path(path)
    out = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames,duration,field_order"
         ":format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    j = json.loads(out.stdout)
    st = j["streams"][0]
    fps = _parse_rate(st.get("avg_frame_rate") or "0") or _parse_rate(st.get("r_frame_rate") or "0")
    duration = float(st.get("duration") or j.get("format", {}).get("duration") or 0)
    nb = int(st.get("nb_frames") or 0) or int(round(duration * fps))
    return VideoInfo(
        path=path, width=int(st["width"]), height=int(st["height"]), fps=fps,
        duration=duration, nb_frames=nb, codec=st.get("codec_name", "?"),
        field_order=st.get("field_order", "unknown"),
    )


def packet_pts(path: str | Path) -> np.ndarray:
    """Sorted presentation timestamps of all video packets (no decoding)."""
    out = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "packet=pts_time", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    vals = [float(x.strip().rstrip(",")) for x in out.stdout.split() if x.strip().rstrip(",") not in ("", "N/A")]
    return np.array(sorted(vals), dtype=np.float64)


def detect_interlace(path: str | Path, max_frames: int = 400) -> tuple[bool, dict]:
    """Run ffmpeg's idet filter. Interlaced if TFF+BFF clearly beat progressive."""
    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(path), "-frames:v", str(max_frames),
         "-vf", "idet", "-an", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.findall(r"Multi frame detection: TFF:\s*(\d+) BFF:\s*(\d+) Progressive:\s*(\d+) Undetermined:\s*(\d+)",
                   proc.stderr)
    if not m:
        return False, {}
    tff, bff, prog, und = (int(v) for v in m[-1])
    stats = {"tff": tff, "bff": bff, "progressive": prog, "undetermined": und}
    interlaced = (tff + bff) > max(10, 2 * prog)
    stats["parity"] = "tff" if tff >= bff else "bff"
    return interlaced, stats


def open_video(path: str | Path, deinterlace: bool | None = None) -> VideoInfo:
    """Probe + interlace check + timestamp table for the analysis frames."""
    info = probe(path)
    if deinterlace is None:
        info.interlaced, info.idet = detect_interlace(path)
        # Trust explicit field-order metadata unless idet clearly sees progressive frames.
        if not info.interlaced and info.field_order in ("tt", "bb", "tb", "bt") and info.idet:
            d = info.idet
            if d["progressive"] < 0.5 * (d["tff"] + d["bff"] + d["undetermined"]):
                info.interlaced = True
        # Explicit field order in the stream wins for the parity; idet decides otherwise.
        if info.interlaced and info.field_order in ("tt", "bb", "tb", "bt"):
            info.idet["parity"] = "bff" if info.field_order in ("bb", "bt") else "tff"
            info.idet["parity_source"] = "stream field_order"
    else:
        info.interlaced = deinterlace
    pts = packet_pts(path)
    if len(pts) == 0:
        pts = np.arange(info.nb_frames) / info.fps
    if info.interlaced:
        # bwdif send_field emits one frame per field: pts and pts + half a frame.
        half = 0.5 / info.fps
        pts = np.stack([pts, pts + half], 1).reshape(-1)
        info.analysis_fps = 2 * info.fps
    else:
        info.analysis_fps = info.fps
    info.pts = pts
    return info


def iter_frames(info: VideoInfo, size: tuple[int, int] | None = None,
                start_index: int = 0, end_index: int | None = None) -> Iterator[tuple[int, np.ndarray]]:
    """Yield (analysis_frame_index, BGR uint8 frame).

    ``size`` = (w, h) to rescale (area filter), else native size. Frames outside
    [start_index, end_index) are decoded but not yielded (decode is sequential).
    """
    w, h = size or (info.width, info.height)
    vf = []
    if info.interlaced:
        parity = info.idet.get("parity", "auto") if info.idet else "auto"
        vf.append(f"bwdif=mode=send_field:parity={parity}:deint=all")
    if size:
        vf.append(f"scale={w}:{h}:flags=area")
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(info.path)]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-fps_mode", "passthrough", "-an", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    frame_bytes = w * h * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=frame_bytes * 2)
    idx = 0
    try:
        while True:
            if end_index is not None and idx >= end_index:
                break
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            if idx >= start_index:
                yield idx, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            idx += 1
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()
