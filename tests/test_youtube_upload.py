"""Resumable upload to YouTube (platforms/youtube.py).

The interesting failures here are not "did it 200" — they are the ones that produce a
CORRUPT or WRONG video while every call looks successful: resuming from the wrong byte
offset, a silently truncated title, or an upload going public when the caller did not
ask for it. Those are what these tests pin.
"""
from __future__ import annotations

import json

import httpx
import pytest

from meshpilot.platforms import youtube as yt

SESSION = "https://upload.example/session/abc"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _video(tmp_path, nbytes: int):
    p = tmp_path / "v.mp4"
    p.write_bytes(b"\x00" * nbytes)
    return p


async def test_it_uploads_and_returns_the_video_id(tmp_path):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            assert req.headers["X-Upload-Content-Length"] == "1024"
            return httpx.Response(200, headers={"Location": SESSION})
        seen.append(req.headers["Content-Range"])
        return httpx.Response(200, json={"id": "VID123"})

    res = await yt.upload_video("example", _video(tmp_path, 1024), title="t",
                                token="tok", client=_client(handler))
    assert res.video_id == "VID123"
    assert res.url == "https://youtu.be/VID123"
    assert seen == ["bytes 0-1023/1024"]


async def test_resume_follows_the_servers_range_not_our_arithmetic(tmp_path):
    """The corruption case. After a 308 the server may hold FEWER bytes than we believe
    we sent; continuing from our own count leaves a gap and the video is broken while
    every response was a success."""
    ranges = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(200, headers={"Location": SESSION})
        ranges.append(req.headers["Content-Range"])
        if len(ranges) == 1:
            # We sent 0..CHUNK-1 but the server only kept the first 1000 bytes.
            return httpx.Response(308, headers={"Range": "bytes=0-999"})
        return httpx.Response(200, json={"id": "OK"})

    size = yt.CHUNK + 5000
    res = await yt.upload_video("example", _video(tmp_path, size), title="t",
                                token="tok", client=_client(handler))
    assert res.video_id == "OK"
    # Second chunk MUST start at 1000 — the server's answer — not at CHUNK.
    assert ranges[1].startswith("bytes 1000-"), ranges


async def test_a_5xx_chunk_is_retried(tmp_path):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(200, headers={"Location": SESSION})
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="backend error")
        return httpx.Response(200, json={"id": "AFTER_RETRY"})

    res = await yt.upload_video("example", _video(tmp_path, 512), title="t",
                                token="tok", client=_client(handler))
    assert res.video_id == "AFTER_RETRY"
    assert calls["n"] == 2


async def test_a_permanent_error_raises_rather_than_reporting_success(tmp_path):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(200, headers={"Location": SESSION})
        return httpx.Response(403, text="quotaExceeded")

    with pytest.raises(yt.YouTubeUploadFailed, match="403"):
        await yt.upload_video("example", _video(tmp_path, 512), title="t",
                              token="tok", client=_client(handler))


async def test_it_defaults_to_unlisted(tmp_path):
    """An upload is the one step that cannot be quietly undone once subscribers are
    notified, so PUBLIC has to be asked for."""
    body = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            body.update(json.loads(req.content))
            return httpx.Response(200, headers={"Location": SESSION})
        return httpx.Response(200, json={"id": "X"})

    await yt.upload_video("example", _video(tmp_path, 16), title="t",
                          token="tok", client=_client(handler))
    assert body["status"]["privacyStatus"] == "unlisted"


async def test_an_over_long_title_is_refused_not_truncated(tmp_path):
    """YouTube truncates server-side instead of erroring, which would ship a cut title."""
    with pytest.raises(ValueError, match="101 chars"):
        await yt.upload_video("example", _video(tmp_path, 16), title="x" * 101, token="tok")


async def test_bad_privacy_and_missing_file_are_refused(tmp_path):
    with pytest.raises(ValueError, match="privacy must be"):
        await yt.upload_video("example", _video(tmp_path, 16), title="t",
                              privacy="everyone", token="tok")
    with pytest.raises(FileNotFoundError):
        await yt.upload_video("example", tmp_path / "nope.mp4", title="t", token="tok")


async def test_chunk_size_is_256kib_aligned():
    """Google rejects a non-final chunk that is not a multiple of 256 KiB, with a 400
    and no useful body."""
    assert yt.CHUNK % (256 * 1024) == 0


async def test_an_oversized_thumbnail_is_refused(tmp_path):
    big = tmp_path / "t.png"
    big.write_bytes(b"\x00" * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="2MB"):
        await yt.set_thumbnail("example", "VID", big, token="tok")


async def test_thumbnail_posts_to_the_right_video(tmp_path):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"kind": "youtube#thumbnailSetResponse"})

    png = tmp_path / "t.png"
    png.write_bytes(b"\x89PNG" + b"\x00" * 100)
    assert await yt.set_thumbnail("example", "VID42", png, token="tok",
                                  client=_client(handler)) is True
    assert "videoId=VID42" in seen["url"]
