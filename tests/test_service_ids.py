import hashlib

import pytest

from tbh.service.ids import InvalidVideoURL, normalize

VID = "YTkyRTsiIaY"
CANON = f"https://www.youtube.com/watch?v={VID}"


@pytest.mark.parametrize("url", [
    f"https://www.youtube.com/watch?v={VID}",
    f"http://youtube.com/watch?v={VID}",
    f"www.youtube.com/watch?v={VID}",
    f"https://m.youtube.com/watch?v={VID}&feature=share",
    f"https://www.youtube.com/watch?feature=youtu.be&v={VID}&t=359s",
    f"https://www.youtube.com/watch?v={VID}&list=PL123&index=4#t=10",
    f"https://youtu.be/{VID}",
    f"https://youtu.be/{VID}?t=359&si=abc",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://youtube.com/shorts/{VID}?feature=share",
    f"https://www.youtube.com/live/{VID}?si=x",
    f"https://www.youtube.com/embed/{VID}?start=10",
    f"https://www.youtube-nocookie.com/embed/{VID}",
    f"  https://WWW.YouTube.com/watch?v={VID}  ",
])
def test_youtube_urls(url):
    assert normalize(url) == (VID, CANON)


@pytest.mark.parametrize("url", [
    "",
    "https://vimeo.com/123456",
    "https://example.com/watch?v=YTkyRTsiIaY",
    "https://youtube.com.evil.example/watch?v=YTkyRTsiIaY",
    "https://www.youtube.com/watch?v=tooshort",
    "https://www.youtube.com/watch?v=YTkyRTsiIaY<script>",
    "https://www.youtube.com/channel/UC1234567890",
    "https://www.youtube.com/",
    "ftp://youtube.com/watch?v=YTkyRTsiIaY",
    "javascript:alert(1)",
    "/tmp/some/file.mp4",
])
def test_rejected(url, monkeypatch):
    monkeypatch.delenv("TBH_ALLOW_LOCAL_FILES", raising=False)
    with pytest.raises(InvalidVideoURL):
        normalize(url)


def test_local_files_gated(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"")
    monkeypatch.delenv("TBH_ALLOW_LOCAL_FILES", raising=False)
    with pytest.raises(InvalidVideoURL):
        normalize(str(clip))

    monkeypatch.setenv("TBH_ALLOW_LOCAL_FILES", "1")
    path = str(clip.resolve())
    expected = ("local-" + hashlib.sha1(path.encode()).hexdigest()[:11], path)
    assert normalize(str(clip)) == expected
    assert normalize("file://" + str(clip)) == expected
    with pytest.raises(InvalidVideoURL):
        normalize(str(tmp_path / "missing.mp4"))
    # YouTube URLs still work with the flag on
    assert normalize(f"https://youtu.be/{VID}")[0] == VID
