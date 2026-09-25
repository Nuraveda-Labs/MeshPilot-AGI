"""YouTube publisher — resumable upload of a local video file.

Why this exists
---------------
MeshPilot could authenticate to YouTube but could not PUBLISH to it: `oauth/youtube.py`
managed tokens and `/internal/youtube/whoami` proved identity, while the registered
publishers were Buffer, Facebook and Instagram. A 17-minute landscape upload therefore
had no path — Buffer carries YouTube *Shorts only* (vertical, <=3 min), and an MCP
bridge cannot help because YouTube wants the file's BINARY in the request body, not a
URL or a Drive id.

Where this runs
---------------
On the operator's machine, against the file on local disk. That is deliberate: the
alternative is shipping ~1GB into the cloud first, which buys nothing. Credentials are
still MeshPilot's — the refresh token lives in Postgres (written by the cloud OAuth
callback) and resolves per brand, so this is the pipeline reaching a local file, not a
side channel around it.

Credentials, per brand, no globals:
    <PREFIX>_YOUTUBE_CLIENT_ID / <PREFIX>_YOUTUBE_CLIENT_SECRET   (token refresh)
    + a stored PlatformAuth row for (brand, "youtube")

⚠️ Uploads are QUOTA-EXPENSIVE. A videos.insert costs ~1600 units of the default
10,000/day, so roughly six uploads a day per project before YouTube refuses.
"""
from __future__ import annotations

import json
import mimetypes
import os
import pathlib
from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger(__name__)

_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
_THUMB_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"

# 8 MiB. Must be a multiple of 256 KiB — Google rejects a non-aligned chunk that is not
# the final one, and the failure is a 400 with no useful body.
CHUNK = 8 * 1024 * 1024
_RESUMABLE_INCOMPLETE = 308
_RETRY_STATUS = {500, 502, 503, 504}
MAX_CHUNK_RETRIES = 5

VALID_PRIVACY = {"public", "unlisted", "private"}


class YouTubeUploadFailed(RuntimeError):
    """Upload did not complete. Carries the last status and a trimmed body."""


@dataclass(slots=True)
class UploadResult:
    video_id: str
    url: str
    privacy: str


def _metadata(title: str, description: str, tags: list[str], privacy: str,
              category_id: str, made_for_kids: bool) -> dict:
    if privacy not in VALID_PRIVACY:
        raise ValueError(f"privacy must be one of {sorted(VALID_PRIVACY)}, got {privacy!r}")
    # YouTube truncates a >100-char title server-side rather than erroring, which would
    # silently ship a cut title. Refuse instead.
    if len(title) > 100:
        raise ValueError(f"title is {len(title)} chars; YouTube's limit is 100")
    if len(description) > 5000:
        raise ValueError(f"description is {len(description)} chars; YouTube's limit is 5000")
    return {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": made_for_kids,
        },
    }


async def _start_session(client: httpx.AsyncClient, token: str, meta: dict,
                         size: int, content_type: str) -> str:
    r = await client.post(
        _UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": content_type,
        },
        content=json.dumps(meta),
    )
    if r.status_code not in (200, 201):
        raise YouTubeUploadFailed(f"could not open upload session: {r.status_code} {r.text[:300]}")
    location = r.headers.get("location") or r.headers.get("Location")
    if not location:
        raise YouTubeUploadFailed("upload session opened but returned no Location header")
    return location


async def upload_video(
    brand_id: str,
    path: str | pathlib.Path,
    *,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    privacy: str = "unlisted",
    category_id: str = "20",          # 20 = Gaming
    made_for_kids: bool = False,
    token: str | None = None,
    client: httpx.AsyncClient | None = None,
    progress=None,
) -> UploadResult:
    """Upload a local file and return its video id.

    `privacy` defaults to **unlisted** on purpose: an upload is the one step that cannot
    be quietly undone once subscribers are notified, so going public is an explicit act.
    """
    p = pathlib.Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"no such video file: {p}")
    size = p.stat().st_size
    if size == 0:
        raise ValueError(f"{p} is empty")
    content_type = mimetypes.guess_type(p.name)[0] or "video/*"
    meta = _metadata(title, description, tags or [], privacy, category_id, made_for_kids)

    if token is None:
        from meshpilot.oauth.youtube import get_fresh_access_token
        token = await get_fresh_access_token(brand_id)

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=600.0))
    try:
        session_url = await _start_session(client, token, meta, size, content_type)
        log.info("youtube.upload.started", brand_id=brand_id, bytes=size, privacy=privacy)

        offset = 0
        with p.open("rb") as fh:
            while offset < size:
                fh.seek(offset)
                chunk = fh.read(CHUNK)
                end = offset + len(chunk) - 1
                headers = {
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                }
                for attempt in range(MAX_CHUNK_RETRIES):
                    r = await client.put(session_url, headers=headers, content=chunk)
                    if r.status_code in _RETRY_STATUS and attempt < MAX_CHUNK_RETRIES - 1:
                        log.warning("youtube.upload.chunk_retry", status=r.status_code,
                                    offset=offset, attempt=attempt + 1)
                        continue
                    break

                if r.status_code == _RESUMABLE_INCOMPLETE:
                    # Trust the server's Range over our own arithmetic: after a retried
                    # chunk it may hold fewer bytes than we think we sent, and assuming
                    # otherwise corrupts the file with a silent gap.
                    rng = r.headers.get("range") or r.headers.get("Range")
                    offset = int(rng.split("-")[1]) + 1 if rng else offset + len(chunk)
                elif r.status_code in (200, 201):
                    body = r.json()
                    vid = body.get("id", "")
                    if not vid:
                        raise YouTubeUploadFailed(f"upload finished without an id: {str(body)[:300]}")
                    log.info("youtube.upload.done", brand_id=brand_id, video_id=vid, privacy=privacy)
                    return UploadResult(vid, f"https://youtu.be/{vid}", privacy)
                else:
                    raise YouTubeUploadFailed(
                        f"chunk at {offset} failed: {r.status_code} {r.text[:300]}")

                if progress:
                    progress(offset, size)

        raise YouTubeUploadFailed("stream ended before YouTube acknowledged the upload")
    finally:
        if owns_client:
            await client.aclose()


async def set_thumbnail(brand_id: str, video_id: str, path: str | pathlib.Path, *,
                        token: str | None = None,
                        client: httpx.AsyncClient | None = None) -> bool:
    """Attach a custom thumbnail. Requires a channel verified for custom thumbnails."""
    p = pathlib.Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"no such thumbnail: {p}")
    if p.stat().st_size > 2 * 1024 * 1024:
        raise ValueError(f"{p} is {p.stat().st_size} bytes; YouTube's thumbnail limit is 2MB")
    if token is None:
        from meshpilot.oauth.youtube import get_fresh_access_token
        token = await get_fresh_access_token(brand_id)

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=120)
    try:
        r = await client.post(
            _THUMB_URL,
            params={"videoId": video_id},
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": mimetypes.guess_type(p.name)[0] or "image/png"},
            content=p.read_bytes(),
        )
        if r.status_code not in (200, 201):
            raise YouTubeUploadFailed(f"thumbnail failed: {r.status_code} {r.text[:300]}")
        log.info("youtube.thumbnail.set", brand_id=brand_id, video_id=video_id)
        return True
    finally:
        if owns_client:
            await client.aclose()
