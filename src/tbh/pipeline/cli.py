"""``tbh`` command line.

tbh process <url-or-file> --start 5:59 --end 6:12 --out track.json [--keep-media] [--work-dir DIR]
            [--time-offset SECONDS] [--video-id ID] [--detector auto|tracknet|classical]
            [--device mps|cpu] [--debug-out candidates.json]
tbh preview <media> <track.json> --out preview.mp4 [--side-by-side] [--circle-only] [--debug candidates.json]
            [--time-offset SECONDS] [--display-width 1280]
tbh contact <media> <track.json> --out sheet.png [-n 36]
tbh validate <track.json>
tbh merge <old.json> <new.json> --out merged.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from . import trackfile
from .timeutil import format_time, parse_time


def _progress_printer(quiet: bool):
    last = {"stage": None, "t": 0.0}

    def progress(stage: str, fraction: float, message: str = "") -> None:
        if quiet:
            return
        now = time.time()
        if stage == last["stage"] and now - last["t"] < 0.5 and fraction < 1.0:
            return
        last.update(stage=stage, t=now)
        print(f"[{stage:8s}] {fraction * 100:5.1f}%  {message}", file=sys.stderr, flush=True)

    return progress


def cmd_process(a) -> int:
    from .api import process_video

    start = parse_time(a.start)
    end = parse_time(a.end)
    if start is not None and end is not None and end <= start:
        print("error: --end must be after --start", file=sys.stderr)
        return 2
    debug: dict = {}
    reuse = json.loads(Path(a.reuse_candidates).read_text()) if a.reuse_candidates else None
    kwargs = dict(time_offset=parse_time(a.time_offset), video_id=a.video_id, source_url=a.source_url,
                  detector=a.detector, device=a.device, debug=debug, reuse_candidates=reuse)
    if a.work_dir:
        wd = Path(a.work_dir)
        track = process_video(a.input, start, end, wd, _progress_printer(a.quiet), a.keep_media, **kwargs)
    else:
        if a.keep_media:
            print("note: --keep-media without --work-dir keeps media in a temp dir; pass --work-dir",
                  file=sys.stderr)
        with tempfile.TemporaryDirectory(prefix="tbh-") as td:
            track = process_video(a.input, start, end, Path(td), _progress_printer(a.quiet), False, **kwargs)
    trackfile.dump(track, a.out)
    st = track["generator"]["stats"]
    print(json.dumps({"out": str(a.out), "rows": st["rows"], "rows_pct": st["rows_pct"],
                      "interpolated_pct": st["interpolated_pct"], "detector_fps": st["detector_fps"],
                      "media": str(debug.get("media")),
                      "time_offset": track["generator"]["source"]["time_offset"]}, indent=1))
    if a.debug_out:
        i0 = debug["i0"]

        def enc(lst):
            return {str(i + i0): [[round(float(v), 2) for v in c] for c in cs] for i, cs in enumerate(lst) if cs}

        Path(a.debug_out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.debug_out).write_text(json.dumps({
            "candidates": enc(debug["candidates"]), "raw_candidates": enc(debug["raw_candidates"]),
            "cuts": debug["cuts"], "detector": debug["detector"]}))
    return 0


def cmd_preview(a) -> int:
    from .preview import render_preview

    track = trackfile.load(a.track)
    debug = json.loads(Path(a.debug).read_text()) if a.debug else None
    t0 = time.time()
    st = render_preview(a.media, track, a.out, side_by_side=a.side_by_side, debug=debug,
                        display_width=a.display_width, time_offset=parse_time(a.time_offset),
                        conf_threshold=a.conf, circle_only=a.circle_only)
    print(json.dumps({**st, "out": str(a.out), "seconds": round(time.time() - t0, 1)}))
    return 0


def cmd_contact(a) -> int:
    from .preview import render_contact_sheet

    st = render_contact_sheet(a.media, trackfile.load(a.track), a.out, n=a.n, cols=a.cols,
                              time_offset=parse_time(a.time_offset))
    print(json.dumps(st))
    return 0


def cmd_validate(a) -> int:
    track = trackfile.load(a.track)
    trackfile.validate(track)
    segs = ", ".join(f"{format_time(s['start'])}-{format_time(s['end'])}" for s in track["segments"])
    print(f"ok: {track['video_id']} {len(track['frames'])} rows, segments {segs}")
    return 0


def cmd_merge(a) -> int:
    merged = trackfile.merge(trackfile.load(a.old), trackfile.load(a.new))
    trackfile.dump(merged, a.out)
    print(f"ok: {len(merged['frames'])} rows")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tbh", description="Tennis ball highlighter pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("process", help="track the ball and write track.json")
    pr.add_argument("input", help="YouTube URL or local video file")
    pr.add_argument("--start", help="start time (seconds or m:ss)")
    pr.add_argument("--end", help="end time (seconds or m:ss)")
    pr.add_argument("--out", required=True, type=Path)
    pr.add_argument("--keep-media", action="store_true")
    pr.add_argument("--work-dir", type=Path)
    pr.add_argument("--time-offset", help="local files: source time of the file's first frame")
    pr.add_argument("--video-id")
    pr.add_argument("--source-url")
    pr.add_argument("--detector", default="auto", choices=["auto", "tracknet", "classical"])
    pr.add_argument("--device", default=None, help="torch device (default: mps, else cpu)")
    pr.add_argument("--debug-out", type=Path, help="write raw per-frame candidates (for preview --debug)")
    pr.add_argument("--reuse-candidates", type=Path,
                    help="development: reuse candidates from a --debug-out file instead of running the detector")
    pr.add_argument("-q", "--quiet", action="store_true")
    pr.set_defaults(func=cmd_process)

    pv = sub.add_parser("preview", help="render a local preview video with the ring burned in")
    pv.add_argument("media")
    pv.add_argument("track", type=Path)
    pv.add_argument("--out", required=True, type=Path)
    pv.add_argument("--side-by-side", action="store_true")
    pv.add_argument("--debug", type=Path, help="candidates json from process --debug-out")
    pv.add_argument("--time-offset", help="source time of the media's first frame (default: from track)")
    pv.add_argument("--display-width", type=float, default=1280.0,
                    help="assumed on-screen player width in CSS px (for the 1.5px minimum stroke)")
    pv.add_argument("--conf", type=float, default=0.5, help="hide rows below this confidence")
    pv.add_argument("--circle-only", action="store_true",
                    help="always draw a circle at the streak centre (the user setting), never a stadium")
    pv.set_defaults(func=cmd_preview)

    cs = sub.add_parser("contact", help="write a contact sheet PNG (zoomed crops around the ball)")
    cs.add_argument("media")
    cs.add_argument("track", type=Path)
    cs.add_argument("--out", required=True, type=Path)
    cs.add_argument("-n", type=int, default=36)
    cs.add_argument("--cols", type=int, default=4)
    cs.add_argument("--time-offset")
    cs.set_defaults(func=cmd_contact)

    va = sub.add_parser("validate", help="validate a track.json")
    va.add_argument("track", type=Path)
    va.set_defaults(func=cmd_validate)

    mg = sub.add_parser("merge", help="merge two track files of the same video")
    mg.add_argument("old", type=Path)
    mg.add_argument("new", type=Path)
    mg.add_argument("--out", required=True, type=Path)
    mg.set_defaults(func=cmd_merge)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
