"""Buffer's CreatePostInput.metadata — per-service post metadata, confirmed by introspecting the live schema.

The first real YouTube post through Buffer (2026-09-24, brand ai_empire) failed with:

    Buffer createPost InvalidInputError: Invalid post: YouTube posts require a title.,
    YouTube posts require a category.

`create_post` had no way to send them. The live schema declares:

    CreatePostInput.metadata: PostInputMetaData
    PostInputMetaData { youtube | tiktok | instagram | … }
    YoutubePostMetadataInput { title, categoryId, isAiGenerated, madeForKids, privacy, … }
"""
from meshpilot.platforms import buffer


async def _captured(monkeypatch, **kwargs):
    seen = {}

    async def _graphql(token, query, variables=None, **k):
        seen["input"] = (variables or {})["input"]
        return {"createPost": {"__typename": "PostActionSuccess",
                               "post": {"id": "p1", "status": "sending"}}}

    async def _channel(brand_id, service):
        return "chan-1"

    monkeypatch.setattr(buffer, "_graphql", _graphql)
    monkeypatch.setattr(buffer, "_channel_id_for_service", _channel)
    monkeypatch.setattr(buffer, "_buffer_token", lambda b: "tok")
    await buffer.create_post("ge", "youtube", text="hello", media_url="https://cdn/clip.mp4", **kwargs)
    return seen["input"]


async def test_youtube_metadata_reaches_the_create_post_input(monkeypatch):
    meta = {"youtube": {"title": "Demo, don't memo", "categoryId": "28"}}
    inp = await _captured(monkeypatch, metadata=meta)
    assert inp["metadata"] == meta


async def test_youtube_without_metadata_gets_a_default_title_and_category(monkeypatch):
    """Every brand posts YouTube via Buffer, so a caller that passes no metadata must still be valid."""
    inp = await _captured(monkeypatch)
    assert inp["metadata"] == {"youtube": {"title": "hello", "categoryId": "22"}}


async def test_caller_youtube_values_win_over_defaults(monkeypatch):
    inp = await _captured(monkeypatch, metadata={"youtube": {"title": "Mine", "categoryId": "28"}})
    assert inp["metadata"] == {"youtube": {"title": "Mine", "categoryId": "28"}}


async def test_non_youtube_without_metadata_omits_the_field(monkeypatch):
    """Omit, don't send null: X / TikTok / LinkedIn callers stay byte-for-byte unchanged."""
    seen = {}

    async def _graphql(token, query, variables=None, **k):
        seen["input"] = (variables or {})["input"]
        return {"createPost": {"__typename": "PostActionSuccess", "post": {"id": "p1", "status": "sending"}}}

    async def _channel(brand_id, service):
        return "chan-1"

    monkeypatch.setattr(buffer, "_graphql", _graphql)
    monkeypatch.setattr(buffer, "_channel_id_for_service", _channel)
    monkeypatch.setattr(buffer, "_buffer_token", lambda b: "tok")
    await buffer.create_post("ge", "tiktok", text="hello", media_url="https://cdn/clip.mp4")
    assert "metadata" not in seen["input"]
