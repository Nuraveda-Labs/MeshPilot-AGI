"""Channel operations over the brand's MCP server (platforms/youtube_ops.py).

The remote tool takes free text and is itself an agent, so the failures that matter are
not transport errors — they are AMBIGUITY and BLAST RADIUS: an instruction vague enough
that the far end edits a field nobody asked about, or a destructive call that goes
through because it was easy to make.
"""
from __future__ import annotations

import pytest

from meshpilot.platforms import youtube_ops as yo


class Spy:
    """Captures the namespaced tool and args instead of calling anything."""

    def __init__(self, reply: str = '{"ok":true}') -> None:
        self.calls: list[tuple[str, dict]] = []
        self.reply = reply

    async def __call__(self, ns: str, args: dict) -> str:
        self.calls.append((ns, args))
        return self.reply


def _ops(reply: str = '{"ok":true}'):
    spy = Spy(reply)
    return yo.YouTubeOps("example", spy), spy


async def test_every_action_id_is_routed_to_the_right_tool():
    """Studio actions must not be sent to the YouTube tool and vice versa — the ids are
    opaque row keys, so a mix-up fails remotely with a useless message."""
    for action, (tool, row) in yo.ACTIONS.items():
        assert tool in (yo.TOOL_YT, yo.TOOL_STUDIO), action
        assert row.startswith("row"), (action, row)
    assert yo.ACTIONS["channel_analytics"][0] == yo.TOOL_STUDIO
    assert yo.ACTIONS["update_details"][0] == yo.TOOL_YT


async def test_update_details_names_the_id_and_forbids_collateral_edits():
    ops, spy = _ops()
    await ops.update_details("VID9", title="New Title", tags=["a", "b"])
    ns, args = spy.calls[0]
    assert ns == "mcp__viasocket__YouTube"
    assert args["action_name"] == "rowu3qctzvvy"
    ins = args["instructions"]
    assert "'VID9'" in ins
    assert "New Title" in ins and "a, b" in ins
    # The far end is an agent; without this it may rewrite fields it was not given.
    assert "Do not change any other field." in ins
    # A field not supplied must not be mentioned at all.
    assert "description" not in ins.lower()


async def test_update_details_with_nothing_to_change_is_refused():
    ops, spy = _ops()
    with pytest.raises(ValueError, match="nothing to change"):
        await ops.update_details("VID9")
    assert spy.calls == []


async def test_delete_requires_explicit_confirmation():
    """A typo or an improvising model must not be able to remove a video."""
    ops, spy = _ops()
    with pytest.raises(yo.YouTubeOpsError, match="destructive"):
        await ops.delete_video("VID9")
    assert spy.calls == []
    res = await ops.delete_video("VID9", confirm=True)
    assert res.ok and len(spy.calls) == 1


async def test_upload_is_refused_with_a_pointer_to_the_real_uploader():
    """The server exposes an upload action, but it can only carry text — YouTube needs
    the binary. Failing loudly beats a caller discovering it at 965MB."""
    ops, _ = _ops()
    for alias in ("upload", "upload_video", "upload_video_or_shorts"):
        with pytest.raises(yo.YouTubeOpsError, match="youtube_upload.py"):
            await ops.run(alias, "go")


async def test_unknown_action_is_refused_not_forwarded():
    ops, spy = _ops()
    with pytest.raises(yo.YouTubeOpsError, match="unknown action"):
        await ops.run("make_me_famous", "please")
    assert spy.calls == []


async def test_bad_privacy_is_refused():
    ops, _ = _ops()
    with pytest.raises(ValueError, match="bad privacy"):
        await ops.update_details("VID9", privacy="everyone")


async def test_an_error_reply_is_reported_as_failure_not_success():
    ops, _ = _ops(reply="ERROR: MCP mcp__viasocket__YouTube failed: 401")
    res = await ops.post_comment("VID9", "hi")
    assert res.ok is False
    assert res.json() is None


async def test_json_replies_are_parsed():
    ops, _ = _ops(reply='{"videoId":"VID9","status":"ok"}')
    res = await ops.video_details("VID9")
    assert res.ok and res.json()["videoId"] == "VID9"


async def test_thread_id_is_stable_per_brand_and_action():
    """The remote tool remembers the last few turns per thread_id; a random id each call
    throws that context away, a shared one blends unrelated operations."""
    ops, spy = _ops()
    await ops.list_videos()
    await ops.list_videos()
    ids = {a["thread_id"] for _n, a in spy.calls}
    assert ids == {"example-list_videos"}
