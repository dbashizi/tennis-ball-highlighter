"""Single background worker that runs pipeline jobs one at a time."""

from __future__ import annotations

import logging
import queue
import tempfile
import threading
import time
from pathlib import Path

from .store import Store

log = logging.getLogger("tbh.service")

PROGRESS_INTERVAL = 0.25  # seconds between progress writes within one stage


def load_process_video():
    # Imported lazily so the service starts even if the pipeline is broken.
    from tbh.pipeline.api import process_video

    return process_video


class Worker:
    def __init__(self, store: Store):
        self.store = store
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        for job_id in self.store.recover():
            self._queue.put(job_id)
        self._thread = threading.Thread(target=self._loop, name="tbh-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread:
            self._queue.put(None)
            self._thread.join(timeout)
            self._thread = None

    def submit(self, job_id: str) -> None:
        self._queue.put(job_id)

    def _loop(self) -> None:
        while (job_id := self._queue.get()) is not None:
            try:
                self.run_job(job_id)
            except Exception:  # never let the worker thread die
                log.exception("worker error on job %s", job_id)

    def run_job(self, job_id: str) -> None:
        job = self.store.start_job(job_id)
        if job is None:  # cancelled or already handled
            return
        last = {"stage": None, "at": 0.0}

        def progress(stage: str, fraction: float, message: str = "") -> None:
            t = time.monotonic()
            if stage == last["stage"] and t - last["at"] < PROGRESS_INTERVAL and fraction < 1:
                return
            last.update(stage=stage, at=t)
            self.store.update_progress(job_id, stage, fraction, message)

        try:
            process_video = load_process_video()
            with tempfile.TemporaryDirectory(prefix="tbh-job-") as work_dir:
                track = process_video(
                    job["url"], job["start"], job["end"], work_dir=Path(work_dir), progress=progress
                )
            if not self.store.is_processing(job_id):
                return  # cancelled while running (e.g. video deleted)
            progress("finalize", 1.0, "saving track")
            self.store.save_track(job["video_id"], job["url"], track)
            self.store.finish_job(job_id)
        except Exception as exc:
            log.exception("job %s failed", job_id)
            self.store.fail_job(job_id, f"{type(exc).__name__}: {exc}")
