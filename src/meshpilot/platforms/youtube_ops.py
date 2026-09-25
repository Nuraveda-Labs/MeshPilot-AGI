"""Every YouTube channel operation, driven through the brand's MCP server.

What this is
------------
`platforms/youtube.py` uploads a file. This is the rest of the channel: video metadata,
playlists, comments, thumbnails, analytics, deletion. They are split because they are
different problems — an upload needs the file's BINARY over a resumable session, while
everything here is text and ids, which is exactly what an MCP bridge carries well.

Transport
---------
The brand's MCP server (`<PREFIX>_MCP_SERVERS`), reached through MeshPilot's existing
MCP client — the same path the agent's tool-use loop uses, so a capability and the brain
cannot drift onto different credentials. Nothing global: a brand without that server
configured simply has no YouTube ops.

The remote tool's interface is three text fields (`thread_id`, `action_name`,
`instructions`), so each method here composes ONE unambiguous instruction. That
vagueness is the risk worth designing against: "update the video" invites the remote
agent to guess, so every method states the id and the exact fields to change.

⚠️ Upload is NOT here. The same server exposes an upload action, but YouTube wants the
file's binary in the request body and this tool can only carry text — verified by asking
it. Use `platforms/youtube.py` / `scripts/youtube_upload.py`, which does a real
resumable upload from local disk.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import structlog

log = structlog.get_logger(__name__)

SERVER = "viasocket"
TOOL_YT = "YouTube"
TOOL_STUDIO = "Youtube_Studio"

# Verified against the live server's tool descriptions, not guessed.
ACTIONS: dict[str, tuple[str, str]] = {
    "list_videos":       (TOOL_YT, "rowi7rfdqo96"),
    "video_details":     (TOOL_YT, "rowqqr8k7upf"),
    "update_details":    (TOOL_YT, "rowu3qctzvvy"),
    "set_thumbnail":     (TOOL_YT, "rowllex9dja4"),
    "create_playlist":   (TOOL_YT, "row1ctouh055"),
    "list_playlists":    (TOOL_YT, "rowji0erw2ts"),
    "update_playlist":   (TOOL_YT, "rowlfi41rnp7"),
    "add_to_playlist":   (TOOL_YT, "row3dem94p5s"),
    "post_comment":      (TOOL_YT, "row0hpgdlc1b"),
    "get_comments":      (TOOL_YT, "row88jlsw1gp"),
    "delete_comment":    (TOOL_YT, "rownydn8um53"),
    "delete_video":      (TOOL_YT, "row84s0mr6x4"),
    "update_banner":     (TOOL_YT, "rown1ndb1vqy"),
    "channel_analytics": (TOOL_STUDIO, "rowdjj8mwar2"),
    "find_channels":     (TOOL_STUDIO, "row3a6xpsjw5"),
}

# These cannot be undone from here. They take confirm=True so that a typo, or a model
# improvising a call, cannot quietly remove a video or a viewer's comment.
DESTRUCTIVE = {"delete_video", "delete_comment"}

# Upload is deliberately absent from ACTIONS; name it so the error can be specific.
_UPLOAD_ALIAS = {"upload", "upload_video", "upload_video_or_shorts"}

Caller = Callable[[str, dict], Awaitable[str]]


class YouTubeOpsError(RuntimeError):
    pass


@dataclass(slots=True)
class OpResult:
    action: str
    ok: bool
    text: str

    def json(self) -> Any:
        """Best-effort structured view; these tools answer with JSON more often than not."""
        try:
            return json.loads(self.text)
        except Exception:  # noqa: BLE001
            return None


class YouTubeOps:
    """Channel operations for one brand. `call` is injected so this is testable offline."""

    def __init__(self, brand_id: str, call: Caller, *, server: str = SERVER) -> None:
        self.brand_id = brand_id
        self._call = call
        self._server = server

    async def run(self, action: str, instructions: str, *, confirm: bool = False,
                  thread_id: str | None = None) -> OpResult:
        if action in _UPLOAD_ALIAS:
            raise YouTubeOpsError(
                "upload is not available over MCP — YouTube needs the file's binary in the "
                "request body and this tool carries text only. Use scripts/youtube_upload.py."
            )
        if action not in ACTIONS:
            raise YouTubeOpsError(f"unknown action {action!r}; known: {sorted(ACTIONS)}")
        if action in DESTRUCTIVE and not confirm:
            raise YouTubeOpsError(f"{action} is destructive and needs confirm=True")

        tool, row = ACTIONS[action]
        ns = f"mcp__{self._server}__{tool}"
        args = {
            "thread_id": thread_id or f"{self.brand_id}-{action}",
            "action_name": row,
            "instructions": instructions,
        }
        text = await self._call(ns, args)
        ok = not text.startswith("ERROR:")
        (log.info if ok else log.warning)(
            "youtube.ops", brand_id=self.brand_id, action=action, ok=ok)
        return OpResult(action, ok, text)

    # --- metadata -------------------------------------------------------------
    async def update_details(self, video_id: str, *, title: str | None = None,
                             description: str | None = None, tags: list[str] | None = None,
                             privacy: str | None = None) -> OpResult:
        parts = [f"For the YouTube video with id '{video_id}', update ONLY these fields:"]
        if title is not None:
            parts.append(f"- title: {title!r}")
        if description is not None:
            parts.append(f"- description: {description!r}")
        if tags is not None:
            parts.append(f"- tags: {', '.join(tags)}")
        if privacy is not None:
            if privacy not in {"public", "unlisted", "private"}:
                raise ValueError(f"bad privacy {privacy!r}")
            parts.append(f"- privacyStatus: {privacy}")
        if len(parts) == 1:
            raise ValueError("update_details called with nothing to change")
        # Stated explicitly: the remote side is an agent, and an unlisted field is an
        # invitation for it to 'helpfully' rewrite something nobody asked about.
        parts.append("Do not change any other field.")
        return await self.run("update_details", "\n".join(parts))

    async def list_videos(self, limit: int = 25) -> OpResult:
        return await self.run("list_videos", f"List the {limit} most recent published videos "
                                             "on the connected channel with id, title and privacy.")

    async def video_details(self, video_id: str) -> OpResult:
        return await self.run("video_details", f"Get full details for the video id '{video_id}'.")

    # --- playlists ------------------------------------------------------------
    async def create_playlist(self, title: str, *, description: str = "",
                              privacy: str = "public") -> OpResult:
        return await self.run("create_playlist",
                              f"Create a playlist titled {title!r} with description "
                              f"{description!r} and privacy {privacy}.")

    async def add_to_playlist(self, playlist_id: str, video_id: str) -> OpResult:
        return await self.run("add_to_playlist",
                              f"Add video id '{video_id}' to playlist id '{playlist_id}'.")

    async def list_playlists(self) -> OpResult:
        return await self.run("list_playlists", "List all playlists with their ids and titles.")

    # --- community ------------------------------------------------------------
    async def post_comment(self, video_id: str, text: str) -> OpResult:
        return await self.run("post_comment",
                              f"Post this comment on video id '{video_id}': {text!r}")

    async def get_comments(self, video_id: str, limit: int = 20) -> OpResult:
        return await self.run("get_comments",
                              f"Get the {limit} most recent comments on video id '{video_id}'.")

    # --- analytics ------------------------------------------------------------
    async def channel_analytics(self, days: int = 28) -> OpResult:
        return await self.run("channel_analytics",
                              f"Get channel analytics for the last {days} days: views, watch "
                              "time, subscribers gained, and top videos.")

    # --- destructive ----------------------------------------------------------
    async def delete_video(self, video_id: str, *, confirm: bool = False) -> OpResult:
        return await self.run("delete_video", f"Delete the video with id '{video_id}'.",
                              confirm=confirm)


async def ops_for_brand(brand_id: str):
    """Async context manager yielding YouTubeOps bound to the brand's MCP servers.

        async with ops_for_brand("acme") as yt:
            await yt.update_details(vid, title="...")
    """
    from contextlib import asynccontextmanager

    from meshpilot.agent.mcp import manager_for_brand

    @asynccontextmanager
    async def _cm():
        mgr = await manager_for_brand(brand_id)
        async with mgr as m:
            yield YouTubeOps(brand_id, m.call)

    return _cm()
