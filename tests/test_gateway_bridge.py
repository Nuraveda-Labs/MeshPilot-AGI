"""Routing/auth config for the Discord gateway (gateway/bridge.py).

The gateway is deployed separately (Railway) and its discord.py dependency is not in this
project's env, so `discord` is stubbed and the module is loaded from its path. The logic under
test is pure config parsing — which is precisely where the last outage lived: a token that had
gone stale, accepted silently, 401ing every message.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest


@pytest.fixture(scope="module")
def bridge():
    stub = types.ModuleType("discord")

    class _Intents:
        @staticmethod
        def default():
            return types.SimpleNamespace(message_content=False)

    stub.Intents = _Intents
    stub.Client = lambda **_kw: types.SimpleNamespace(
        event=lambda fn: fn, user=None, guilds=[], run=lambda *_a, **_k: None)
    stub.Message = object
    sys.modules.setdefault("discord", stub)

    path = pathlib.Path(__file__).resolve().parents[1] / "gateway" / "bridge.py"
    spec = importlib.util.spec_from_file_location("meshpilot_gateway_bridge", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_channel_maps_to_its_brand_and_that_brands_token(bridge):
    routes, problems = bridge.parse_routes({
        "DISCORD_CHANNEL_BRANDS": '{"111": {"brand": "exampleco_lab", "prefix": "EXAMPLECO"},'
                                  ' "222": {"brand": "example", "prefix": "ACME"}}',
        "EXAMPLECO_JOBS_AUTH_TOKEN": "nl-token",
        "ACME_JOBS_AUTH_TOKEN": "ge-token",
    })
    assert problems == []
    assert routes[111].brand == "exampleco_lab" and routes[111].token == "nl-token"
    assert routes[222].brand == "example" and routes[222].token == "ge-token"


def test_a_route_whose_token_is_missing_is_dropped_not_silently_kept(bridge):
    """The whole point. A route with no token can only 401 on every message, so it must not
    exist — and the reason must be stated, not inferred later from a user's screenshot."""
    routes, problems = bridge.parse_routes({
        "DISCORD_CHANNEL_BRANDS": '{"111": {"brand": "exampleco_lab", "prefix": "EXAMPLECO"},'
                                  ' "222": {"brand": "example", "prefix": "ACME"}}',
        "ACME_JOBS_AUTH_TOKEN": "ge-token",
    })
    assert 111 not in routes
    assert 222 in routes                      # one bad brand must not take the others down
    assert any("EXAMPLECO_JOBS_AUTH_TOKEN is unset" in p for p in problems)


def test_brands_do_not_share_a_token(bridge):
    """#95: one project's chat must not be able to act as another."""
    routes, _ = bridge.parse_routes({
        "DISCORD_CHANNEL_BRANDS": '{"111": {"brand": "exampleco_lab", "prefix": "EXAMPLECO"},'
                                  ' "222": {"brand": "example", "prefix": "ACME"}}',
        "EXAMPLECO_JOBS_AUTH_TOKEN": "nl-token",
        "ACME_JOBS_AUTH_TOKEN": "ge-token",
    })
    assert routes[111].token != routes[222].token


def test_bad_json_yields_no_routes_and_says_why(bridge):
    routes, problems = bridge.parse_routes({"DISCORD_CHANNEL_BRANDS": "{not json"})
    assert routes == {}
    assert any("not valid JSON" in p for p in problems)


def test_entry_missing_prefix_is_rejected(bridge):
    routes, problems = bridge.parse_routes({
        "DISCORD_CHANNEL_BRANDS": '{"111": {"brand": "exampleco_lab"}}',
        "EXAMPLECO_JOBS_AUTH_TOKEN": "nl-token",
    })
    assert routes == {}
    assert any("needs both" in p for p in problems)


def test_legacy_single_channel_still_works_but_complains(bridge):
    routes, problems = bridge.parse_routes({
        "DISCORD_AGENT_CHANNEL_ID": "999",
        "MESHPILOT_BRAND": "example",
        "MESHPILOT_JOBS_TOKEN": "legacy",
    })
    assert routes[999].brand == "example"
    assert any("legacy" in p for p in problems)


def test_no_config_at_all_produces_no_routes(bridge):
    routes, problems = bridge.parse_routes({})
    assert routes == {} and problems


def test_repr_never_leaks_the_token(bridge):
    routes, _ = bridge.parse_routes({
        "DISCORD_CHANNEL_BRANDS": '{"111": {"brand": "exampleco_lab", "prefix": "EXAMPLECO"}}',
        "EXAMPLECO_JOBS_AUTH_TOKEN": "super-secret-value",
    })
    assert "super-secret-value" not in repr(routes[111])


# ── SCHED-1: the keepalive that holds the API awake ─────────────────────────────
#
# The bridge's real job is Discord. These pin the contract that a heartbeat can never be the
# thing that takes the websocket down — every unusable shape must degrade to None, not raise.

def test_healthz_body_yields_the_scheduler_age(bridge):
    body = {"status": "ok", "scheduler": {"cron_enabled": True,
                                          "last_run_age_s": 441, "worst_overdue_s": 0}}
    assert bridge.scheduler_age_s(body) == 441


def test_a_stalled_scheduler_reports_its_real_age_not_a_verdict(bridge):
    """The watchdog caught last_run_age_s=6557 during a real stall; the parser must pass it
    through untouched rather than deciding what "too old" means."""
    assert bridge.scheduler_age_s({"scheduler": {"last_run_age_s": 6557}}) == 6557


@pytest.mark.parametrize("body", [
    None, "ok", 42, [],                              # not a dict at all
    {}, {"scheduler": None}, {"scheduler": "ok"},    # no usable scheduler block
    {"scheduler": {}},                               # no age reported
    {"scheduler": {"last_run_age_s": None}},
    {"scheduler": {"last_run_age_s": "441"}},        # a string is not an age
])
def test_an_unusable_healthz_body_degrades_to_none_and_never_raises(bridge, body):
    assert bridge.scheduler_age_s(body) is None


def test_a_bool_is_not_an_age(bridge):
    """bool is a subclass of int in Python, so `True` would otherwise sail through as age=1."""
    assert bridge.scheduler_age_s({"scheduler": {"last_run_age_s": True}}) is None


def test_a_float_age_is_accepted_and_floored(bridge):
    assert bridge.scheduler_age_s({"scheduler": {"last_run_age_s": 12.9}}) == 12


def test_the_interval_sits_well_inside_the_measured_idle_window(bridge):
    """Idle was measured at roughly 60 min after the last inbound request. A default anywhere
    near that would race the reaper it exists to beat."""
    assert 0 < bridge.KEEPALIVE_INTERVAL <= 1800


# ── CLIPNET #clip-queue ─────────────────────────────────────────────────────────────────────────

def test_clip_message_finds_links_in_order_and_dedupes(bridge):
    urls, camp = bridge.parse_clip_message(
        "clip these https://www.youtube.com/watch?v=9FGMhz-e97k and https://youtu.be/abcdefghijk, "
        "https://www.youtube.com/watch?v=9FGMhz-e97k campaign: Lovable")
    assert urls == ["https://www.youtube.com/watch?v=9FGMhz-e97k", "https://youtu.be/abcdefghijk"]
    assert camp == "lovable"


def test_clip_message_without_links_or_campaign(bridge):
    assert bridge.parse_clip_message("hello there") == ([], "")
    assert bridge.parse_clip_message("https://youtu.be/abcdefghijk")[1] == ""


def test_clip_message_accepts_shorts_and_campaign_equals(bridge):
    urls, camp = bridge.parse_clip_message("https://youtube.com/shorts/abcdefghijk campaign=ent-vault")
    assert urls == ["https://youtube.com/shorts/abcdefghijk"] and camp == "ent-vault"


def test_clip_routes_map_channel_to_brand_token_and_default_campaign(bridge):
    routes, problems = bridge.parse_clip_routes({
        "CLIPQUEUE_CHANNEL_BRANDS": '{"111": {"brand": "ai_empire", "prefix": "AIE", "campaign": "lovable"},'
                                    ' "222": {"brand": "hypedrop_gaming", "prefix": "HD"}}',
        "AIE_JOBS_AUTH_TOKEN": "aie-tok",
    })
    route, campaign = routes[111]
    assert (route.brand, route.token, campaign) == ("ai_empire", "aie-tok", "lovable")
    assert 222 not in routes and any("HD_JOBS_AUTH_TOKEN" in p for p in problems)


def test_no_clip_queue_config_means_no_clip_routes(bridge):
    assert bridge.parse_clip_routes({}) == ({}, [])
