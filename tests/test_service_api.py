import gzip
import threading
import time

import pytest
from fastapi.testclient import TestClient

from tbh.service import jobs
from tbh.service.app import create_app
from tbh.service.store import Store

VID = "YTkyRTsiIaY"
URL = f"https://www.youtube.com/watch?v={VID}"


def make_track(start, end, x=0.5, n=None, video_id=VID):
    n = n if n is not None else int(round((end - start) / 0.5)) + 1
    return {
        "schema_version": 1,
        "video_id": video_id,
        "source_url": URL,
        "created_at": "2026-09-17T09:00:00Z",
        "generator": {"name": "fake", "version": "0", "detector": "fake"},
        "video": {"width": 1280, "height": 720, "fps": 50.0},
        "segments": [{"start": start, "end": end}],
        "fields": ["t", "x", "y", "r", "conf", "ring", "flags"],
        "frames": [[start + 0.5 * i, x, 0.5, 0.004, 0.9, "#101010", 0] for i in range(n)],
    }


class FakePipeline:
    def __init__(self):
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()
        self.fail = None
        self.x = 0.5

    def __call__(self, url, start, end, work_dir, progress, keep_media=False):
        self.calls.append((url, start, end, work_dir))
        assert work_dir.is_dir()
        progress("download", 0.5, "downloading")
        self.gate.wait(5)
        for i in range(3):
            progress("detect", i / 3, f"frame {i}/3")
        if self.fail:
            raise RuntimeError(self.fail)
        return make_track(start or 0.0, end or 10.0, x=self.x)


@pytest.fixture
def fake(monkeypatch):
    f = FakePipeline()
    monkeypatch.setattr(jobs, "load_process_video", lambda: f)
    return f


@pytest.fixture
def client(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("TBH_HOME", str(tmp_path))
    with TestClient(create_app(), base_url="http://127.0.0.1:8765") as c:
        yield c


def wait_job(client, job_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/v1/jobs/{job_id}").json()
        if job["status"] in ("ready", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job did not finish: {job}")


def test_health(client):
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "version": "0.1.0", "detector": "tracknet-v2"}


def test_unknown_video_and_track(client):
    assert client.get(f"/v1/videos/{VID}").status_code == 404
    assert client.get(f"/v1/videos/{VID}").json() == {"detail": "unknown video"}
    assert client.get(f"/v1/tracks/{VID}").status_code == 404
    assert client.get("/v1/videos/../../etc").status_code == 404
    assert client.get("/v1/jobs/nope").status_code == 404
    assert client.delete(f"/v1/videos/{VID}").status_code == 404


def test_job_lifecycle_and_track_served(client, fake):
    r = client.post("/v1/jobs", json={"url": f"https://youtu.be/{VID}?t=359", "start": 359, "end": 372})
    assert r.status_code == 202
    job = r.json()
    assert job["video_id"] == VID and job["status"] in ("queued", "processing")

    job = wait_job(client, job["job_id"])
    assert job["status"] == "ready" and job["stage"] == "done" and job["progress"] == 1
    assert job["error"] is None
    assert fake.calls[0][:3] == (URL, 359.0, 372.0)
    assert not fake.calls[0][3].exists()  # temp work dir cleaned up

    video = client.get(f"/v1/videos/{VID}").json()
    assert video["status"] == "ready"
    assert video["url"] == URL
    assert video["segments"] == [{"start": 359.0, "end": 372.0}]
    assert video["track_url"] == f"/v1/tracks/{VID}"
    assert video["job"] is None

    r = client.get(f"/v1/tracks/{VID}", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    track = r.json()
    assert track["schema_version"] == 1 and track["video_id"] == VID
    assert len(track["frames"]) == 27

    listing = client.get("/v1/videos").json()
    assert [v["video_id"] for v in listing] == [VID]


def test_progress_visible_while_processing(client, fake):
    fake.gate.clear()
    job = client.post("/v1/jobs", json={"url": URL}).json()
    deadline = time.time() + 5
    while client.get(f"/v1/jobs/{job['job_id']}").json()["stage"] != "download":
        assert time.time() < deadline
        time.sleep(0.02)
    running = client.get(f"/v1/jobs/{job['job_id']}").json()
    assert running["status"] == "processing" and running["progress"] == 0.5
    video = client.get(f"/v1/videos/{VID}").json()
    assert video["status"] == "processing" and video["job"]["job_id"] == job["job_id"]
    fake.gate.set()
    assert wait_job(client, job["job_id"])["status"] == "ready"
    assert fake.calls[0][1:3] == (None, None)


def test_dedupe_in_flight(client, fake):
    fake.gate.clear()
    body = {"url": URL, "start": 10, "end": 20}
    a = client.post("/v1/jobs", json=body).json()
    b = client.post("/v1/jobs", json={**body, "url": f"https://m.youtube.com/watch?v={VID}"}).json()
    c = client.post("/v1/jobs", json={"url": URL, "start": 10, "end": 30}).json()
    assert a["job_id"] == b["job_id"]
    assert c["job_id"] != a["job_id"]
    fake.gate.set()
    wait_job(client, a["job_id"])
    wait_job(client, c["job_id"])
    assert len(fake.calls) == 2
    # finished jobs are not reused
    d = client.post("/v1/jobs", json=body).json()
    assert d["job_id"] != a["job_id"]
    wait_job(client, d["job_id"])


def test_failure_path(client, fake):
    fake.fail = "no ball found"
    job = client.post("/v1/jobs", json={"url": URL}).json()
    job = wait_job(client, job["job_id"])
    assert job["status"] == "failed"
    assert "no ball found" in job["error"]
    video = client.get(f"/v1/videos/{VID}").json()
    assert video["status"] == "failed"
    assert video["track_url"] is None
    assert video["job"]["error"] == job["error"]


def test_failure_keeps_existing_track_ready(client, fake):
    wait_job(client, client.post("/v1/jobs", json={"url": URL, "start": 0, "end": 5}).json()["job_id"])
    fake.fail = "boom"
    wait_job(client, client.post("/v1/jobs", json={"url": URL, "start": 5, "end": 9}).json()["job_id"])
    video = client.get(f"/v1/videos/{VID}").json()
    assert video["status"] == "ready" and video["segments"] == [{"start": 0.0, "end": 5.0}]


def test_merge_via_jobs(client, fake):
    fake.x = 0.1
    wait_job(client, client.post("/v1/jobs", json={"url": URL, "start": 0, "end": 10}).json()["job_id"])
    fake.x = 0.9
    wait_job(client, client.post("/v1/jobs", json={"url": URL, "start": 5, "end": 15}).json()["job_id"])
    track = client.get(f"/v1/tracks/{VID}").json()
    assert track["segments"] == [{"start": 0.0, "end": 15.0}]
    ts = [row[0] for row in track["frames"]]
    assert ts == sorted(ts) and len(ts) == len(set(ts))
    for t, x, *_ in track["frames"]:
        assert x == (0.1 if t < 5 else 0.9)
    assert client.get(f"/v1/videos/{VID}").json()["segments"] == [{"start": 0.0, "end": 15.0}]


def test_invalid_track_from_pipeline_fails_job(client, fake, monkeypatch):
    monkeypatch.setattr(jobs, "load_process_video", lambda: lambda *a, **k: {"schema_version": 2})
    job = wait_job(client, client.post("/v1/jobs", json={"url": URL}).json()["job_id"])
    assert job["status"] == "failed" and "schema_version" in job["error"]


def test_broken_pipeline_import_fails_job_not_service(client, monkeypatch):
    def broken():
        raise ImportError("torch missing")

    monkeypatch.setattr(jobs, "load_process_video", broken)
    job = wait_job(client, client.post("/v1/jobs", json={"url": URL}).json()["job_id"])
    assert job["status"] == "failed" and "torch missing" in job["error"]
    assert client.get("/v1/health").status_code == 200


@pytest.mark.parametrize("body", [
    {"url": "https://vimeo.com/12345"},
    {"url": "https://www.youtube.com/watch?v=short"},
    {"url": "/etc/passwd"},
    {"url": URL, "start": 20, "end": 10},
    {"url": URL, "start": -1},
    {},
])
def test_job_validation(client, body):
    assert client.post("/v1/jobs", json=body).status_code == 422


def test_local_files_when_enabled(client, fake, monkeypatch, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    monkeypatch.setenv("TBH_ALLOW_LOCAL_FILES", "1")
    job = client.post("/v1/jobs", json={"url": str(clip)}).json()
    assert job["video_id"].startswith("local-") and len(job["video_id"]) == 17
    assert wait_job(client, job["job_id"])["status"] == "ready"
    assert fake.calls[0][0] == str(clip.resolve())
    assert client.get(f"/v1/tracks/{job['video_id']}").json()["video_id"] == job["video_id"]


def test_delete_video(client, tmp_path):
    wait_job(client, client.post("/v1/jobs", json={"url": URL}).json()["job_id"])
    assert (tmp_path / "tracks" / f"{VID}.json").exists()
    assert client.delete(f"/v1/videos/{VID}").status_code == 200
    assert not (tmp_path / "tracks" / f"{VID}.json").exists()
    assert client.get(f"/v1/videos/{VID}").status_code == 404
    assert client.get(f"/v1/tracks/{VID}").status_code == 404


def test_cors_and_private_network_preflight(client):
    for origin in ("chrome-extension://abcdefghijklmnop", "https://www.youtube.com"):
        r = client.options("/v1/jobs", headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
            "Access-Control-Request-Private-Network": "true",
        })
        assert r.status_code == 200, r.text
        assert r.headers["access-control-allow-origin"] == origin
        assert r.headers["access-control-allow-private-network"] == "true"

    r = client.get("/v1/health", headers={"Origin": "chrome-extension://abc"})
    assert r.headers["access-control-allow-origin"] == "chrome-extension://abc"

    r = client.get("/v1/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    r = client.options("/v1/jobs", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert r.status_code == 400


def test_rejects_foreign_host_header(client):
    assert client.get("/v1/health", headers={"Host": "attacker.example"}).status_code == 400


def test_startup_marks_interrupted_and_requeues(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("TBH_HOME", str(tmp_path))
    store = Store()
    stuck, _ = store.submit_job(VID, URL, 1.0, 2.0)
    store.start_job(stuck["job_id"])
    pending, _ = store.submit_job(VID, URL, 3.0, 4.0)
    store.close()

    with TestClient(create_app(), base_url="http://127.0.0.1") as c:
        stuck = c.get(f"/v1/jobs/{stuck['job_id']}").json()
        assert stuck["status"] == "failed" and stuck["error"] == "interrupted"
        assert wait_job(c, pending["job_id"])["status"] == "ready"
