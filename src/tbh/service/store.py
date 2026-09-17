"""SQLite-backed storage for video records, jobs and tracking files."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .merge import merge_tracks, validate_track

ACTIVE = ("queued", "processing")

# Each entry upgrades the schema by one version (PRAGMA user_version).
MIGRATIONS = [
    """
    CREATE TABLE videos (
        video_id   TEXT PRIMARY KEY,
        url        TEXT NOT NULL,
        status     TEXT NOT NULL,
        segments   TEXT NOT NULL DEFAULT '[]',
        track_path TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE jobs (
        job_id     TEXT PRIMARY KEY,
        video_id   TEXT NOT NULL,
        url        TEXT NOT NULL,
        start_s    REAL,
        end_s      REAL,
        status     TEXT NOT NULL,
        stage      TEXT NOT NULL,
        progress   REAL NOT NULL DEFAULT 0,
        message    TEXT NOT NULL DEFAULT '',
        error      TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX jobs_video ON jobs(video_id, status);
    """,
]


def tbh_home() -> Path:
    return Path(os.environ.get("TBH_HOME") or Path.home() / ".tbh").expanduser()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _job_dict(row: sqlite3.Row) -> dict:
    return {
        "job_id": row["job_id"],
        "video_id": row["video_id"],
        "url": row["url"],
        "status": row["status"],
        "stage": row["stage"],
        "progress": row["progress"],
        "message": row["message"],
        "error": row["error"],
        "start": row["start_s"],
        "end": row["end_s"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class Store:
    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else tbh_home()
        self.tracks_dir = self.home / "tracks"
        self.tracks_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(
            self.home / "tbh.sqlite3", check_same_thread=False, isolation_level=None, timeout=10
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def close(self) -> None:
        self.db.close()

    def _migrate(self) -> None:
        with self._lock:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
                self.db.executescript(f"BEGIN; {script}; PRAGMA user_version={i}; COMMIT;")

    def _q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    # ---- jobs -----------------------------------------------------------

    def get_job(self, job_id: str) -> dict | None:
        rows = self._q("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        return _job_dict(rows[0]) if rows else None

    def submit_job(self, video_id: str, url: str, start, end) -> tuple[dict, bool]:
        """Create a queued job, or return the identical active one. -> (job, created)."""
        with self._lock:
            rows = self._q(
                "SELECT * FROM jobs WHERE video_id=? AND start_s IS ? AND end_s IS ? "
                "AND status IN ('queued','processing') ORDER BY created_at LIMIT 1",
                (video_id, start, end),
            )
            if rows:
                return _job_dict(rows[0]), False
            ts, job_id = now(), uuid.uuid4().hex
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?,?,'queued','queued',0,'',NULL,?,?)",
                    (job_id, video_id, url, start, end, ts, ts),
                )
                self.db.execute(
                    "INSERT INTO videos (video_id, url, status, created_at, updated_at) "
                    "VALUES (?,?,'queued',?,?) ON CONFLICT(video_id) DO UPDATE SET updated_at=?",
                    (video_id, url, ts, ts, ts),
                )
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")
            return self.get_job(job_id), True

    def start_job(self, job_id: str) -> dict | None:
        """Mark a queued job as processing; None if it is no longer queued."""
        with self._lock:
            cur = self.db.execute(
                "UPDATE jobs SET status='processing', updated_at=? "
                "WHERE job_id=? AND status='queued'",
                (now(), job_id),
            )
            return self.get_job(job_id) if cur.rowcount else None

    def update_progress(self, job_id: str, stage: str, progress: float, message: str) -> None:
        self._q(
            "UPDATE jobs SET stage=?, progress=?, message=?, updated_at=? "
            "WHERE job_id=? AND status='processing'",
            (stage, max(0.0, min(1.0, float(progress))), message, now(), job_id),
        )

    def is_processing(self, job_id: str) -> bool:
        return bool(self._q("SELECT 1 FROM jobs WHERE job_id=? AND status='processing'", (job_id,)))

    def finish_job(self, job_id: str) -> None:
        self._q(
            "UPDATE jobs SET status='ready', stage='done', progress=1, message='', updated_at=? "
            "WHERE job_id=? AND status='processing'",
            (now(), job_id),
        )

    def fail_job(self, job_id: str, error: str) -> None:
        with self._lock:
            ts = now()
            self.db.execute(
                "UPDATE jobs SET status='failed', error=?, updated_at=? "
                "WHERE job_id=? AND status IN ('queued','processing')",
                (error, ts, job_id),
            )
            self.db.execute(
                "UPDATE videos SET status='failed', updated_at=? "
                "WHERE track_path IS NULL AND video_id=(SELECT video_id FROM jobs WHERE job_id=?)",
                (ts, job_id),
            )

    def recover(self) -> list[str]:
        """Fail jobs interrupted by a restart; return ids of still-queued jobs."""
        rows = self._q("SELECT job_id FROM jobs WHERE status='processing'")
        for r in rows:
            self.fail_job(r["job_id"], "interrupted")
        return [r["job_id"] for r in self._q(
            "SELECT job_id FROM jobs WHERE status='queued' ORDER BY created_at")]

    # ---- videos ---------------------------------------------------------

    def _video_dict(self, row: sqlite3.Row) -> dict:
        job_rows = self._q(
            "SELECT * FROM jobs WHERE video_id=? ORDER BY "
            "(status IN ('queued','processing')) DESC, created_at DESC LIMIT 1",
            (row["video_id"],),
        )
        job = _job_dict(job_rows[0]) if job_rows else None
        has_track = row["track_path"] is not None
        if job and job["status"] in ACTIVE:
            status = job["status"]
        elif has_track:
            status, job = "ready", None
        else:
            status = row["status"]
        return {
            "video_id": row["video_id"],
            "url": row["url"],
            "status": status,
            "segments": json.loads(row["segments"]),
            "track_url": f"/v1/tracks/{row['video_id']}" if has_track else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "job": job,
        }

    def get_video(self, video_id: str) -> dict | None:
        rows = self._q("SELECT * FROM videos WHERE video_id=?", (video_id,))
        return self._video_dict(rows[0]) if rows else None

    def list_videos(self) -> list[dict]:
        rows = self._q("SELECT * FROM videos ORDER BY updated_at DESC")
        return [self._video_dict(r) for r in rows]

    def track_path(self, video_id: str) -> Path | None:
        rows = self._q("SELECT track_path FROM videos WHERE video_id=?", (video_id,))
        if not rows or not rows[0]["track_path"]:
            return None
        path = Path(rows[0]["track_path"])
        return path if path.is_file() else None

    def save_track(self, video_id: str, url: str, track: dict, replace: bool = False) -> dict:
        """Validate, merge into any existing track, write to disk and mark ready."""
        track = dict(track, video_id=video_id)
        validate_track(track)
        with self._lock:
            existing = self.track_path(video_id)
            if existing and not replace:
                track = merge_tracks(json.loads(existing.read_text()), track)
            path = self.tracks_dir / f"{video_id}.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(track, separators=(",", ":")))
            os.replace(tmp, path)
            ts = now()
            self.db.execute(
                "INSERT INTO videos VALUES (?,?,'ready',?,?,?,?) ON CONFLICT(video_id) DO UPDATE "
                "SET status='ready', segments=excluded.segments, "
                "track_path=excluded.track_path, updated_at=excluded.updated_at",
                (video_id, url, json.dumps(track["segments"]), str(path), ts, ts),
            )
        return track

    def delete_video(self, video_id: str) -> bool:
        with self._lock:
            path = self.tracks_dir / f"{video_id}.json"
            cur = self.db.execute("DELETE FROM videos WHERE video_id=?", (video_id,))
            self.db.execute(
                "UPDATE jobs SET status='failed', error='cancelled', updated_at=? "
                "WHERE video_id=? AND status IN ('queued','processing')",
                (now(), video_id),
            )
            path.unlink(missing_ok=True)
            return cur.rowcount > 0
