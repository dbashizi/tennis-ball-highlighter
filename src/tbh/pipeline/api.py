"""Pipeline entry point used by the service and the CLI (see docs/api.md)."""

from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path
from typing import Callable

import numpy as np

from . import trackfile
from .detect import make_detector, run_detection
from .recover import recover
from .refine import refine
from .track import TrackParams, track
from .video import open_video

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, float, str], None]


def _noop(stage: str, fraction: float, message: str = "") -> None:
    pass


def _is_local(url: str) -> bool:
    s = str(url)
    return "://" not in s and Path(s).expanduser().exists()


def process_video(
    url: str,
    start: float | None,
    end: float | None,
    work_dir: Path,
    progress: ProgressFn | None = None,
    keep_media: bool = False,
    *,
    time_offset: float | None = None,
    video_id: str | None = None,
    source_url: str | None = None,
    detector: str = "auto",
    weights: Path | None = None,
    device: str | None = None,
    verify_offset: bool = True,
    recover_gaps: bool = True,
    reuse_candidates: dict | None = None,
    debug: dict | None = None,
) -> dict:
    """Track the ball in ``url`` between ``start`` and ``end`` (source seconds). Returns a schema-v1 track dict.

    ``url`` may be a YouTube URL, which is downloaded (only the range) into
    ``work_dir``, or a local file path. For a local file, ``time_offset`` is the
    source time of the file's pts 0 (default 0). ``start`` and ``end`` are
    always in the source timeline. Media we downloaded is deleted at the end
    unless ``keep_media``. A local input file is never deleted.

    ``debug``: an optional dict that receives intermediate results (candidates, track).
    """
    progress = progress or _noop
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    timings: dict[str, float] = {}
    created: list[Path] = []
    timing_check: dict = {}
    try:
        # ---------------------------------------------------------------- download
        t0 = time.time()
        if _is_local(url):
            media = Path(url).expanduser().resolve()
            offset = float(time_offset or 0.0)
            vid = video_id or trackfile.extract_video_id(media.name) or _safe_id(media.stem)
            src_url = source_url or (f"https://www.youtube.com/watch?v={vid}"
                                     if trackfile.extract_video_id(media.name) else None)
            timing_check = {"verified": False, "method": "local file; offset supplied by caller",
                            "offset": offset}
            progress("download", 1.0, f"using local file {media.name}")
        else:
            from .download import download_section, verify_clip_offset

            vid = video_id or trackfile.extract_video_id(url)
            if not vid:
                raise ValueError(f"not a YouTube URL: {url!r}")
            src_url = source_url or f"https://www.youtube.com/watch?v={vid}"
            dl = download_section(url, start, end, work_dir, progress)
            created = list(dl.created)
            media = dl.path
            offset = dl.start
            timing_check = {"verified": False, "offset": offset, "method": "nominal (download start)"}
            if verify_offset and dl.stream_url and (start or 0) > 0:
                progress("download", 0.98, "verifying clip start time")
                fps_guess = float(dl.fps or 25.0)
                timing_check = verify_clip_offset(dl.stream_url, media, offset, fps_guess)
                if timing_check.get("verified") and abs(timing_check["delta"]) < 1.0:
                    offset = float(timing_check["offset"])
                else:
                    log.warning("clip offset not verified, using nominal start: %s", timing_check)
            progress("download", 1.0, f"downloaded {media.name}")
        timings["download"] = time.time() - t0

        # ---------------------------------------------------------------- decode
        t0 = time.time()
        progress("decode", 0.0, "probing video")
        info = open_video(media)
        src_times = offset + info.pts
        seg_start = float(start) if start is not None else float(src_times[0])
        seg_end = float(end) if end is not None else float(offset + info.duration)
        eps = 1e-6
        in_range = np.nonzero((src_times >= seg_start - eps) & (src_times < seg_end - eps))[0]
        if len(in_range) == 0:
            raise ValueError(f"no frames in [{seg_start}, {seg_end}) (file covers "
                             f"{src_times[0]:.3f}-{src_times[-1]:.3f})")
        i0, i1 = int(in_range[0]), int(in_range[-1]) + 1
        progress("decode", 1.0, f"{info.width}x{info.height} @ {info.analysis_fps:g} fps"
                 f"{' (deinterlaced)' if info.interlaced else ''}, {i1 - i0} frames")
        timings["decode"] = time.time() - t0

        # ---------------------------------------------------------------- detect
        t0 = time.time()
        progress("detect", 0.0, "loading detector")
        if reuse_candidates is not None:
            # Development aid: skip the detector and use cached raw candidates
            # ({abs_frame: [[x, y, score], ...]}, as written by `tbh process --debug-out`).
            from .detect import DetectionResult

            rc_ = reuse_candidates.get("raw_candidates", reuse_candidates.get("candidates", {}))
            dres = DetectionResult(
                candidates=[[tuple(c) for c in rc_.get(str(i), [])] for i in range(i0, i1)],
                cuts=list(reuse_candidates.get("cuts", [])), width=info.width, height=info.height,
                detector=reuse_candidates.get("detector", "tracknet-v2"), fps_processed=0.0, seconds=0.0)
            det = None
        else:
            det = make_detector(detector, weights=weights, device=device)
            dres = run_detection(info, det, progress, start_index=i0, end_index=i1)
        timings["detect"] = time.time() - t0
        # Candidates are indexed relative to i0.
        cands = dres.candidates
        raw_cands = [list(c) for c in cands]
        cuts_rel = [c - i0 for c in dres.cuts]

        # ---------------------------------------------------------------- track
        t0 = time.time()
        progress("track", 0.0, "linking detections")
        params = TrackParams()
        tr = track(cands, cuts_rel, info.width, info.height, info.analysis_fps, params)
        progress("track", 0.2, f"{len(tr.points)} track points; refining")
        # The refine pass works in absolute frame indices.
        for p in tr.points:
            p.frame += i0
        cuts_abs = [c + i0 for c in cuts_rel]
        final, rinfo = refine(info, tr.points, cuts_abs, progress, params.max_gap)
        n_recovered = 0
        if recover_gaps and final:
            # Hybrid step: classical search in track gaps, then re-track.
            progress("track", 0.6, "searching track gaps")
            fr_ = np.array([q.frame for q in final])
            rr_ = np.array([q.r for q in final])
            recovered = recover(info, tr.points, cuts_abs, lambda f: float(np.interp(f, fr_, rr_)), i0, i1)
            if recovered:
                n_recovered = len(recovered)
                cands = [list(c) for c in cands]
                for f_abs, c in recovered.items():
                    cands[f_abs - i0].append(c)
                tr = track(cands, cuts_rel, info.width, info.height, info.analysis_fps, params)
                for p in tr.points:
                    p.frame += i0
                final, rinfo = refine(info, tr.points, cuts_abs, progress, params.max_gap)
        timings["track"] = time.time() - t0

        # ---------------------------------------------------------------- finalize
        t0 = time.time()
        progress("finalize", 0.0, "building track file")
        W, H = info.width, info.height
        rows = []
        for q in final:
            if q.conf < params.min_conf:
                continue
            x, y = q.x / W, q.y / H
            if not (0 <= x <= 1 and 0 <= y <= 1):
                continue
            rows.append(trackfile.make_row(src_times[q.frame], x, y, q.r / W, q.conf, q.ring, q.flags,
                                           q.sl / W, q.sa))
        n_frames = i1 - i0
        FI = {f: i for i, f in enumerate(trackfile.FIELDS)}
        n_interp = sum(1 for r in rows if r[FI["flags"]] & trackfile.FLAG_INTERPOLATED)
        sl_px = np.array([r[FI["sl"]] * W for r in rows]) if rows else np.zeros(0)
        r_px = np.array([r[FI["r"]] * W for r in rows]) if rows else np.zeros(0)
        stats = {
            "frames_analysed": n_frames,
            "rows": len(rows),
            "rows_pct": round(100.0 * len(rows) / max(1, n_frames), 1),
            "rows_conf_ge_0_5": sum(1 for r in rows if r[FI["conf"]] >= 0.5),
            "visible_pct": round(100.0 * sum(1 for r in rows if r[FI["conf"]] >= 0.5) / max(1, n_frames), 1),
            "stadium_rows": int(np.sum(sl_px >= 0.5 * r_px)) if rows else 0,
            "streak_half_length_px_median": round(float(np.median(sl_px)), 2) if rows else None,
            "streak_ratio_median": round(float(np.median((sl_px + r_px) / r_px)), 2) if rows else None,
            "exposure_k": rinfo.get("exposure_k"),
            "interpolated_rows": n_interp,
            "interpolated_pct": round(100.0 * n_interp / max(1, n_frames), 1),
            "detected_rows": len(rows) - n_interp,
            "radius_measured_rows": sum(1 for q in final if q.measured),
            "bounces": sum(1 for r in rows if r[FI["flags"]] & trackfile.FLAG_BOUNCE),
            "hits": sum(1 for r in rows if r[FI["flags"]] & trackfile.FLAG_HIT),
            "camera_cuts": [round(float(src_times[c]), 3) for c in dres.cuts],
            "static_candidates_removed": tr.static_removed,
            "outliers_removed": tr.outliers_removed,
            "gap_candidates_recovered": n_recovered,
            "detector_fps": round(dres.fps_processed, 1),
            "tracknet_lead_frames": rinfo.get("lead"),
            "device": str(getattr(det, "device", "cpu")) if det is not None else "reused-candidates",
        }
        timings["finalize"] = time.time() - t0
        timings["total"] = time.time() - t_start
        stats["seconds"] = {k: round(v, 2) for k, v in timings.items()}
        stats["realtime_factor"] = round((n_frames / info.analysis_fps) / max(1e-6, timings["total"]), 3)
        source = {
            "file": media.name,
            "codec": info.codec,
            "width": info.width,
            "height": info.height,
            "fps": info.fps,
            "interlaced": info.interlaced,
            "idet": info.idet,
            "time_offset": round(offset, 6),
            "timing_check": timing_check,
        }
        out = trackfile.build_track(
            video_id=vid, source_url=src_url, width=W, height=H, fps=info.analysis_fps,
            segments=[{"start": round(seg_start, 3), "end": round(seg_end, 3)}], rows=rows,
            detector=dres.detector, extra_generator={"stats": stats, "source": source},
        )
        if debug is not None:
            debug.update(candidates=cands, raw_candidates=raw_cands, detector=dres.detector, cuts=dres.cuts, i0=i0, track=tr, final=final, info=info,
                         offset=offset, media=media)
        progress("finalize", 1.0, f"{len(rows)} rows")
        progress("done", 1.0, f"{len(rows)} rows, {stats['rows_pct']}% of frames")
        return out
    finally:
        if not keep_media:
            for p in created:
                try:
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink(missing_ok=True)
                except OSError:
                    pass


def _safe_id(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)[:64] or "local"
