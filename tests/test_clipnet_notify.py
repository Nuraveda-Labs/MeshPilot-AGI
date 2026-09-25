"""CLIPNET Discord notices + per-brand platform selection."""
from meshpilot.agent.clipnet import notify, publish


def test_configured_platforms_skip_meta_for_a_buffer_only_brand(monkeypatch):
    env = {"BUFFER_API_KEY": "b"}
    monkeypatch.setattr("meshpilot.config.brand_env", lambda k, b=None, d="": env.get(k, ""))
    assert publish.configured_platforms("hypedrop_gaming") == ("tiktok", "youtube", "x")


def test_configured_platforms_full_brand_gets_all_five(monkeypatch):
    env = {"BUFFER_API_KEY": "b", "META_PAGE_ID": "p", "SYSTEM_USER_TOKEN": "t", "META_IG_USER_ID": "i"}
    monkeypatch.setattr("meshpilot.config.brand_env", lambda k, b=None, d="": env.get(k, ""))
    assert publish.configured_platforms("ai_empire") == publish.PLATFORMS


def test_facebook_without_instagram_id_still_posts_facebook(monkeypatch):
    env = {"META_PAGE_ID": "p", "SYSTEM_USER_TOKEN": "t"}
    monkeypatch.setattr("meshpilot.config.brand_env", lambda k, b=None, d="": env.get(k, ""))
    assert publish.configured_platforms("x") == ("facebook",)


def test_published_message_lists_links_and_the_whop_deadline():
    msg = notify.published_message(
        "Demo, don't memo",
        {"instagram": {"status": "posted", "url": "https://ig/1", "error": None},
         "tiktok": {"status": "failed", "url": None, "error": "boom"}},
        ("instagram", "tiktok", "youtube"), "04:10 UTC")
    assert "✅ instagram: https://ig/1" in msg and "❌ tiktok: boom" in msg
    assert "Submit to Whop by 04:10 UTC**: instagram" in msg


async def test_notify_is_skipped_without_a_channel(monkeypatch):
    monkeypatch.setattr(notify, "channel_for", lambda b: "")
    assert await notify.notify("ai_empire", "hi", send=lambda **k: None) is False


async def test_notify_sends_to_the_brand_channel(monkeypatch):
    sent = {}

    async def send(**k):
        sent.update(k)

    monkeypatch.setattr(notify, "channel_for", lambda b: "123")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    assert await notify.notify("ai_empire", "hello", send=send) is True
    assert sent == {"token": "tok", "channel_id": "123", "content": "hello"}


async def test_a_failing_send_never_raises(monkeypatch):
    async def send(**k):
        raise RuntimeError("discord down")

    monkeypatch.setattr(notify, "channel_for", lambda b: "123")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    assert await notify.notify("ai_empire", "hello", send=send) is False
