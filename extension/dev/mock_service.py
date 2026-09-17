"""Mock of the local helper service (docs/api.md), stdlib only.

Enough to exercise the extension's prompt -> job -> progress -> ready flow
without the real pipeline.

    python3 extension/dev/mock_service.py                 # :8765, empty
    python3 extension/dev/mock_service.py --preload SYNTHETIC00=extension/dev/synthetic/synthetic.track.json
    python3 extension/dev/mock_service.py --job-seconds 20 --fail-video FAILFAILFAI

Jobs advance with wall-clock time through queued, download, decode, detect,
track, finalize and done. On completion the track is built from a "source"
track for that video (rows inside the requested range), or from a synthetic
circular path when there is none. Pass --source ID=PATH to add sources; the
synthetic clip and samples/YTkyRTsiIaY.track.json are registered by default.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SOURCES = {
    "SYNTHETIC00": REPO / "extension/dev/synthetic/synthetic.track.json",
    "YTkyRTsiIaY": REPO / "samples/YTkyRTsiIaY.track.json",
}
STAGES = [("queued", 0.05), ("download", 0.15), ("decode", 0.15), ("detect", 0.45), ("track", 0.12), ("finalize", 0.08)]
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
FIELDS = ["t", "x", "y", "r", "conf", "ring", "flags"]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def video_id_from_url(url: str) -> str | None:
    u = urlparse(url)
    v = parse_qs(u.query).get("v", [None])[0]
    if v and ID_RE.match(v):
        return v
    tail = u.path.rstrip("/").rsplit("/", 1)[-1]
    return tail if ID_RE.match(tail) else None


def merge_segments(segs: list[dict]) -> list[dict]:
    out: list[dict] = []
    for s in sorted(segs, key=lambda s: s["start"]):
        if out and s["start"] <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], s["end"])
        else:
            out.append(dict(s))
    return out


class State:
    def __init__(self, job_seconds: float, fail_videos: set[str], sources: dict[str, Path]):
        self.lock = threading.Lock()
        self.videos: dict[str, dict] = {}
        self.tracks: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.job_seconds = job_seconds
        self.fail_videos = fail_videos
        self.sources = sources

    # -- tracks --
    def load_source(self, vid: str) -> dict | None:
        p = self.sources.get(vid)
        if p and p.is_file():
            return json.loads(p.read_text())
        return None

    def build_track(self, vid: str, start: float | None, end: float | None) -> dict:
        src = self.load_source(vid)
        if src is not None:
            lo = start if start is not None else -math.inf
            hi = end if end is not None else math.inf
            idx = {f: src["fields"].index(f) for f in FIELDS}
            frames = [[r[idx[f]] for f in FIELDS] for r in src["frames"] if lo <= r[idx["t"]] <= hi]
            segs = []
            for s in src["segments"]:
                a, b = max(s["start"], lo), min(s["end"], hi)
                if b > a:
                    segs.append({"start": a, "end": b})
            fps = src["video"]["fps"]
            video = src["video"]
        else:
            a = start if start is not None else 0.0
            b = end if end is not None else 60.0
            fps = 50.0
            frames = []
            n = int((b - a) * fps)
            for i in range(n):
                t = a + i / fps
                if int(t) % 10 == 9:  # a gap every 10 s
                    continue
                frames.append([round(t, 3), round(0.5 + 0.3 * math.cos(t * 2), 5), round(0.5 + 0.3 * math.sin(t * 2), 5), 0.006, 0.9, "#f5f5f5", 0])
            segs = [{"start": a, "end": b}]
            video = {"width": 1280, "height": 720, "fps": fps}
        return {
            "schema_version": 1,
            "video_id": vid,
            "source_url": f"https://www.youtube.com/watch?v={vid}",
            "created_at": now_iso(),
            "generator": {"name": "mock_service", "version": "0.0.0", "detector": "mock"},
            "video": video,
            "segments": segs,
            "fields": FIELDS,
            "frames": frames,
        }

    def merge_track(self, vid: str, new: dict) -> None:
        old = self.tracks.get(vid)
        if not old:
            self.tracks[vid] = new
            return
        spans = new["segments"]
        kept = [r for r in old["frames"] if not any(s["start"] <= r[0] <= s["end"] for s in spans)]
        old["frames"] = sorted(kept + new["frames"], key=lambda r: r[0])
        old["segments"] = merge_segments(old["segments"] + new["segments"])
        old["created_at"] = new["created_at"]

    def set_ready(self, vid: str, url: str) -> None:
        tr = self.tracks[vid]
        self.videos[vid] = {
            "video_id": vid,
            "url": url,
            "status": "ready",
            "segments": tr["segments"],
            "track_url": f"/v1/tracks/{vid}",
            "updated_at": now_iso(),
            "job": None,
        }

    # -- jobs --
    def advance(self, job: dict) -> dict:
        """Compute job progress from elapsed time; finish or fail it when due."""
        if job["status"] in ("ready", "failed"):
            return job
        frac = min(1.0, (time.monotonic() - job["_t0"]) / self.job_seconds)
        vid = job["video_id"]
        if vid in self.fail_videos and frac >= 0.5:
            job.update(status="failed", error="mock failure: couldn't download this video", message="download failed", updated_at=now_iso())
            rec = self.videos.get(vid)
            if rec:
                rec.update(status="ready" if vid in self.tracks else "failed", job=None if vid in self.tracks else self.public(job))
            return job
        if frac >= 1.0:
            self.merge_track(vid, self.build_track(vid, job["_start"], job["_end"]))
            job.update(status="ready", stage="done", progress=1.0, message="done", updated_at=now_iso())
            self.set_ready(vid, job["_url"])
            return job
        acc = 0.0
        stage = STAGES[-1][0]
        for name, w in STAGES:
            if frac < acc + w:
                stage = name
                sub = (frac - acc) / w
                break
            acc += w
        else:
            sub = 1.0
        job["stage"] = stage
        job["status"] = "queued" if stage == "queued" else "processing"
        job["progress"] = round(frac, 3)
        job["message"] = f"frame {int(sub * 650)}/650" if stage == "detect" else f"{stage}…"
        job["updated_at"] = now_iso()
        rec = self.videos.get(vid)
        if rec:
            rec["status"] = job["status"]
            rec["job"] = self.public(job)
        return job

    @staticmethod
    def public(job: dict) -> dict:
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def create_job(self, url: str, start, end) -> dict:
        vid = video_id_from_url(url)
        if not vid:
            raise ValueError("not a YouTube watch URL")
        for j in self.jobs.values():
            self.advance(j)
            if j["video_id"] == vid and j["_start"] == start and j["_end"] == end and j["status"] in ("queued", "processing"):
                return j
        jid = uuid.uuid4().hex
        job = {
            "job_id": jid, "video_id": vid, "status": "queued", "stage": "queued", "progress": 0.0,
            "message": "waiting", "error": None, "created_at": now_iso(), "updated_at": now_iso(),
            "_t0": time.monotonic(), "_start": start, "_end": end, "_url": url,
        }
        self.jobs[jid] = job
        rec = self.videos.get(vid)
        if rec:
            rec.update(status="queued", job=self.public(job))
        else:
            self.videos[vid] = {"video_id": vid, "url": url, "status": "queued", "segments": [], "track_url": None, "updated_at": now_iso(), "job": self.public(job)}
        return job


def make_handler(state: State, latency: float):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tbh-mock/0.1"

        def log_message(self, fmt, *args):  # quieter log
            print(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

        def cors(self):
            origin = self.headers.get("Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
                # Private Network Access preflight, for pages that ask.
                self.send_header("Access-Control-Allow-Private-Network", "true")

        def send_json(self, status: int, body) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.cors()
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self.send_response(HTTPStatus.NO_CONTENT)
            self.cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def route(self, method: str):
            if latency:
                time.sleep(latency)
            path = urlparse(self.path).path.rstrip("/")
            parts = path.split("/")[1:]
            with state.lock:
                if method == "GET" and path == "/v1/health":
                    return self.send_json(200, {"ok": True, "version": "0.1.0-mock", "detector": "mock"})
                if method == "GET" and path == "/v1/videos":
                    for j in state.jobs.values():
                        state.advance(j)
                    vids = sorted(state.videos.values(), key=lambda v: v["updated_at"], reverse=True)
                    return self.send_json(200, vids)
                if len(parts) == 3 and parts[:2] == ["v1", "videos"]:
                    vid = parts[2]
                    for j in state.jobs.values():
                        if j["video_id"] == vid:
                            state.advance(j)
                    if method == "GET":
                        rec = state.videos.get(vid)
                        return self.send_json(200, rec) if rec else self.send_json(404, {"detail": "unknown video"})
                    if method == "DELETE":
                        existed = state.videos.pop(vid, None)
                        state.tracks.pop(vid, None)
                        return self.send_json(200 if existed else 404, {"ok": True} if existed else {"detail": "unknown video"})
                if method == "GET" and len(parts) == 3 and parts[:2] == ["v1", "tracks"]:
                    tr = state.tracks.get(parts[2])
                    return self.send_json(200, tr) if tr else self.send_json(404, {"detail": "no track"})
                if method == "GET" and len(parts) == 3 and parts[:2] == ["v1", "jobs"]:
                    job = state.jobs.get(parts[2])
                    if not job:
                        return self.send_json(404, {"detail": "unknown job"})
                    return self.send_json(200, state.public(state.advance(job)))
                if method == "POST" and path == "/v1/jobs":
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                        body = json.loads(self.rfile.read(n) or b"{}")
                        start, end = body.get("start"), body.get("end")
                        if (start is not None and end is not None) and end <= start:
                            return self.send_json(422, {"detail": "end must be after start"})
                        job = state.create_job(body["url"], start, end)
                    except (KeyError, ValueError, json.JSONDecodeError) as e:
                        return self.send_json(422, {"detail": str(e)})
                    return self.send_json(202, state.public(job))
            return self.send_json(404, {"detail": "not found"})

        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

        def do_DELETE(self):
            self.route("DELETE")

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--job-seconds", type=float, default=8.0)
    ap.add_argument("--latency", type=float, default=0.0, help="seconds added to every response")
    ap.add_argument("--preload", action="append", default=[], metavar="ID=PATH", help="serve PATH as a ready track")
    ap.add_argument("--source", action="append", default=[], metavar="ID=PATH", help="rows used when a job for ID finishes")
    ap.add_argument("--fail-video", action="append", default=["FAILFAILFAI"], metavar="ID", help="jobs for ID fail halfway")
    args = ap.parse_args()

    sources = dict(DEFAULT_SOURCES)
    for item in args.source:
        k, _, v = item.partition("=")
        sources[k] = Path(v).resolve()
    state = State(args.job_seconds, set(args.fail_video), sources)
    for item in args.preload:
        k, _, v = item.partition("=")
        doc = json.loads(Path(v).read_text())
        state.tracks[k] = doc
        state.set_ready(k, f"https://www.youtube.com/watch?v={k}")
        print(f"preloaded {k} ({len(doc['frames'])} rows)")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state, args.latency))
    print(f"mock tbh service on http://127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
