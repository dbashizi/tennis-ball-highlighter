"""FastAPI app for the local helper service (docs/api.md) and the ``tbh-service`` CLI."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from tbh import __version__

from . import ids
from .jobs import Worker
from .merge import InvalidTrack
from .store import Store

DETECTOR = "tracknet-v2"
DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8765
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "[::1]", "::1"]


class JobRequest(BaseModel):
    url: str
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _range(self):
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


def _known_video_id(video_id: str) -> str:
    if not ids.is_valid_video_id(video_id):
        raise HTTPException(404, "unknown video")
    return video_id


def create_app(home: Path | None = None, allowed_hosts: list[str] | None = None) -> FastAPI:
    """Build the app. Storage is opened (from ``home`` or ``$TBH_HOME``) at startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(home)
        worker = Worker(store)
        worker.start()
        app.state.store, app.state.worker = store, worker
        try:
            yield
        finally:
            worker.stop()
            store.close()

    app = FastAPI(title="tbh helper service", version=__version__, lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=500)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://www.youtube.com"],
        allow_origin_regex=r"chrome-extension://.*",
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        allow_private_network=True,
    )
    # Guards against DNS-rebinding: only accept requests addressed to localhost.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or ALLOWED_HOSTS)

    def store(request: Request) -> Store:
        return request.app.state.store

    @app.get("/v1/health")
    def health():
        return {"ok": True, "version": __version__, "detector": DETECTOR}

    @app.get("/v1/videos")
    def list_videos(request: Request):
        return store(request).list_videos()

    @app.get("/v1/videos/{video_id}")
    def get_video(video_id: str, request: Request):
        video = store(request).get_video(_known_video_id(video_id))
        if video is None:
            raise HTTPException(404, "unknown video")
        return video

    @app.delete("/v1/videos/{video_id}")
    def delete_video(video_id: str, request: Request):
        if not store(request).delete_video(_known_video_id(video_id)):
            raise HTTPException(404, "unknown video")
        return {"deleted": video_id}

    @app.get("/v1/tracks/{video_id}")
    def get_track(video_id: str, request: Request):
        path = store(request).track_path(_known_video_id(video_id))
        if path is None:
            raise HTTPException(404, "unknown track")
        return Response(
            path.read_bytes(), media_type="application/json",
            headers={"Cache-Control": "no-cache"},
        )

    @app.post("/v1/jobs", status_code=202)
    def create_job(body: JobRequest, request: Request):
        try:
            video_id, url = ids.normalize(body.url)
        except ids.InvalidVideoURL as exc:
            raise HTTPException(422, str(exc)) from None
        job, created = store(request).submit_job(video_id, url, body.start, body.end)
        if created:
            request.app.state.worker.submit(job["job_id"])
        return job

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        job = store(request).get_job(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        return job

    return app


app = create_app()


# ---- CLI ----------------------------------------------------------------


def _cmd_run(args) -> int:
    import uvicorn

    hosts = ALLOWED_HOSTS if args.host in ALLOWED_HOSTS else [*ALLOWED_HOSTS, args.host]
    uvicorn.run(create_app(allowed_hosts=hosts), host=args.host, port=args.port)
    return 0


def _cmd_import(args) -> int:
    try:
        track = json.loads(Path(args.track).read_text())
        if args.url:
            video_id, url = ids.normalize(args.url)
        else:
            video_id = track.get("video_id") if isinstance(track, dict) else None
            if not isinstance(video_id, str) or not ids.is_valid_video_id(video_id):
                raise ValueError("track has no valid video_id; pass --url")
            url = track.get("source_url") or ids.watch_url(video_id)
        saved = Store().save_track(video_id, url, track, replace=args.replace)
    except (OSError, ValueError, InvalidTrack) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"imported {video_id}: {len(saved['frames'])} rows, "
          f"{len(saved['segments'])} segment(s)")
    return 0


def _cmd_list(args) -> int:
    videos = Store().list_videos()
    if not videos:
        print("no videos")
    for v in videos:
        segs = ", ".join(f"{s['start']:g}-{s['end']:g}" for s in v["segments"]) or "-"
        print(f"{v['video_id']:<18} {v['status']:<11} {v['updated_at']}  {segs}  {v['url']}")
    return 0


def _cmd_rm(args) -> int:
    if not (ids.is_valid_video_id(args.video_id) and Store().delete_video(args.video_id)):
        print(f"error: unknown video {args.video_id}", file=sys.stderr)
        return 1
    print(f"removed {args.video_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tbh-service", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run the HTTP service")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("import", help="register an existing track.json as ready")
    p.add_argument("track")
    p.add_argument("--url", help="YouTube URL (default: the track's video_id)")
    p.add_argument("--replace", action="store_true", help="replace instead of merging")
    p.set_defaults(func=_cmd_import)

    sub.add_parser("list", help="list known videos").set_defaults(func=_cmd_list)

    p = sub.add_parser("rm", help="remove a video and its tracking file")
    p.add_argument("video_id")
    p.set_defaults(func=_cmd_rm)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
