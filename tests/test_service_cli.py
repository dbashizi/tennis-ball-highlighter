import json

import pytest
from fastapi.testclient import TestClient

from tbh.service.app import create_app, main
from tbh.service.store import Store

VID = "YTkyRTsiIaY"


def write_track(path, start, end, video_id=VID):
    path.write_text(json.dumps({
        "schema_version": 1,
        "video_id": video_id,
        "source_url": f"https://www.youtube.com/watch?v={video_id}",
        "created_at": "2026-09-17T09:00:00Z",
        "generator": {"name": "tbh-pipeline", "version": "0.1.0", "detector": "tracknet-v2"},
        "video": {"width": 1280, "height": 720, "fps": 50.0},
        "segments": [{"start": start, "end": end}],
        "fields": ["t", "x", "y", "r", "conf", "ring", "flags"],
        "frames": [[start + 0.02, 0.5, 0.4, 0.004, 0.9, "#101010", 0]],
    }))
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("TBH_HOME", str(h))
    return h


def test_import_list_rm(home, tmp_path, capsys):
    src = write_track(tmp_path / "t1.json", 359.0, 372.0)
    assert main(["import", str(src)]) == 0
    assert "imported YTkyRTsiIaY" in capsys.readouterr().out
    assert (home / "tracks" / f"{VID}.json").exists()

    # merge a second range
    assert main(["import", str(write_track(tmp_path / "t2.json", 400.0, 410.0))]) == 0
    video = Store().get_video(VID)
    assert video["status"] == "ready"
    assert video["segments"] == [{"start": 359.0, "end": 372.0}, {"start": 400.0, "end": 410.0}]

    # served by the app
    with TestClient(create_app(), base_url="http://127.0.0.1") as c:
        assert c.get(f"/v1/videos/{VID}").json()["status"] == "ready"
        assert len(c.get(f"/v1/tracks/{VID}").json()["frames"]) == 2

    # --replace overwrites
    assert main(["import", str(src), "--replace"]) == 0
    assert Store().get_video(VID)["segments"] == [{"start": 359.0, "end": 372.0}]

    capsys.readouterr()
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert VID in out and "ready" in out and "359-372" in out

    assert main(["rm", VID]) == 0
    assert Store().get_video(VID) is None
    assert not (home / "tracks" / f"{VID}.json").exists()
    assert main(["rm", VID]) == 1


def test_import_with_url_overrides_video_id(home, tmp_path):
    src = write_track(tmp_path / "t.json", 0.0, 1.0, video_id="whatever")
    assert main(["import", str(src)]) == 1  # no valid id in file
    assert main(["import", str(src), "--url", "https://youtu.be/abcdefghijk"]) == 0
    data = json.loads((home / "tracks" / "abcdefghijk.json").read_text())
    assert data["video_id"] == "abcdefghijk"
    assert Store().get_video("abcdefghijk")["url"] == "https://www.youtube.com/watch?v=abcdefghijk"


def test_import_rejects_bad_files(home, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": 2, "video_id": VID}))
    assert main(["import", str(bad)]) == 1
    assert main(["import", str(tmp_path / "missing.json")]) == 1
    (tmp_path / "junk.json").write_text("{not json")
    assert main(["import", str(tmp_path / "junk.json")]) == 1
    assert "error:" in capsys.readouterr().err
    assert Store().list_videos() == []


def test_migrations_are_idempotent(home):
    Store().close()
    s = Store()
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert s.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
