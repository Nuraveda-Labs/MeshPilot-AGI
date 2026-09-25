"""Streaming upload of a local file to the brand's bucket."""
from __future__ import annotations

import httpx
import pytest

from meshpilot.media.generation import storage as st


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_it_streams_the_file_and_returns_a_public_url(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "k")
    monkeypatch.setattr(st, "bucket_for", lambda b: "zv-media")
    body = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/bucket"):
            return httpx.Response(200)
        body["len"] = len(req.content)
        body["ct"] = req.headers["Content-Type"]
        return httpx.Response(200)

    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\x01" * 2_500_000)          # spans multiple 1MiB chunks
    url = await st.upload_file(f, "example", content_type="video/mp4",
                               prefix="longform", dest_name="x.mp4",
                               client=_client(handler))
    assert url == "https://proj.supabase.co/storage/v1/object/public/zv-media/longform/x.mp4"
    assert body["len"] == 2_500_000          # every chunk arrived, nothing truncated
    assert body["ct"] == "video/mp4"


async def test_a_size_rejection_surfaces_rather_than_returning_a_dead_url(tmp_path, monkeypatch):
    """The real failure mode here: the host caps object size, and a returned URL that
    404s later is far worse than an exception now."""
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "k")
    monkeypatch.setattr(st, "bucket_for", lambda b: "zv-media")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/bucket"):
            return httpx.Response(200)
        return httpx.Response(400, json={"statusCode": "413", "code": "EntityTooLarge"})

    f = tmp_path / "big.mp4"
    f.write_bytes(b"\x00" * 1000)
    with pytest.raises(Exception, match="EntityTooLarge"):
        await st.upload_file(f, "example", client=_client(handler))


async def test_a_missing_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "k")
    with pytest.raises(Exception, match="no such file"):
        await st.upload_file(tmp_path / "nope.mp4", "example")
