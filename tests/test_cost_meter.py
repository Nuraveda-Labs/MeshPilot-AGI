"""COST-METER INC-1 — per-brand spend metering (context + price book + meter + rollup)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from meshpilot.analytics.cost import brand_scope, get_brand, record_usage, set_brand, spend_summary
from meshpilot.analytics.cost.pricing import anthropic_cost, higgsfield_cost

# asyncio_mode = "auto" (pyproject) runs the async tests; sync tests stay unmarked.


# ── brand context ──
def test_brand_context_set_get_and_scope():
    set_brand(None)
    assert get_brand() is None
    set_brand("example")
    assert get_brand() == "example"
    with brand_scope("other_brand"):
        assert get_brand() == "other_brand"
    assert get_brand() == "example"  # restored on scope exit


# ── price book ──
def test_anthropic_cost_tokens_to_usd():
    # 1M input + 1M output on haiku ($1 / $5 per MTok) = $6.00
    usd = anthropic_cost("claude-haiku-4-5-20251001",
                         {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    assert usd == pytest.approx(6.0)


def test_anthropic_cost_counts_cache_tokens():
    usd = anthropic_cost("claude-haiku-4-5-20251001",
                         {"input_tokens": 0, "output_tokens": 0,
                          "cache_read_input_tokens": 1_000_000})  # $0.10 / MTok
    assert usd == pytest.approx(0.10)


def test_anthropic_cost_sonnet5_is_2_and_10():
    # Sonnet 5 is $2 in / $10 out per MTok (corrected from a stale $3/$15). 1M+1M = $12.
    usd = anthropic_cost("claude-sonnet-5",
                         {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    assert usd == pytest.approx(12.0)
    # cache read = 0.1x input = $0.20 / MTok
    assert anthropic_cost("claude-sonnet-5",
                          {"cache_read_input_tokens": 1_000_000}) == pytest.approx(0.20)


def test_anthropic_cost_web_search_requests():
    # server-side web_search billed at $0.01/request via usage.server_tool_use
    usd = anthropic_cost("claude-sonnet-5",
                         {"input_tokens": 0, "output_tokens": 0,
                          "server_tool_use": {"web_search_requests": 3, "web_fetch_requests": 9}})
    assert usd == pytest.approx(0.03)   # web_fetch is free


def test_anthropic_cost_unknown_model_returns_none():
    # An unknown/non-Anthropic model must NOT be priced at an arbitrary Claude tier (#194) — the
    # price book only covers Claude models, so a miss must surface as "unknown", not a guessed cost.
    assert anthropic_cost("mystery-model", {"input_tokens": 1_000_000}) is None


def test_anthropic_cost_router_fallback_models_return_none():
    # Non-Anthropic models the router can fail over to (routing.py tiers) must not silently price at
    # claude-haiku-4-5-20251001 rates just because that happens to be the first price-book entry (#194).
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    for model in ("glm-5.3", "kimi-k3", "deepseek-v4-pro", "gpt-5.6-luna", "gpt-5.6-sol"):
        assert anthropic_cost(model, usage) is None


async def test_meter_records_zero_not_a_guessed_tier_for_router_fallback_model(monkeypatch):
    # #194 regression: when OpenRouter omits usage.cost for a non-Anthropic router-fallback model
    # (e.g. z-ai/glm-5.3), _meter must never record the Claude Haiku rate it used to fall back to
    # via anthropic_cost's old "first price-book entry" default.
    from meshpilot.agent.loop.llm import _meter

    recorded: dict = {}

    async def _fake_record_usage(**kwargs):
        recorded.update(kwargs)

    monkeypatch.setattr("meshpilot.analytics.cost.record_usage", _fake_record_usage)
    monkeypatch.setattr("meshpilot.analytics.cost.get_brand", lambda: "acme")

    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}  # no "cost" key (OpenRouter omitted it)
    await _meter("z-ai/glm-5.3", usage, "req-1")

    # not the guessed Claude Haiku tier (would be $6.0 for this usage)
    assert recorded["cost_usd"] != pytest.approx(6.0)
    assert recorded["model"] == "z-ai/glm-5.3"


async def test_meter_records_nonzero_conservative_estimate_for_unknown_model(monkeypatch):
    # #196 regression: recording cost_usd=0.0 for a model missing from the price book makes a real
    # paid call look genuinely free to the daily-budget check and cost reconciliation
    # (usage_events.cost_usd is their source), letting an unknown model burn budget invisibly.
    # _meter must record a non-zero, configurable conservative estimate instead of 0.0.
    from meshpilot.agent.loop.llm import _meter

    recorded: dict = {}

    async def _fake_record_usage(**kwargs):
        recorded.update(kwargs)

    monkeypatch.setattr("meshpilot.analytics.cost.record_usage", _fake_record_usage)
    monkeypatch.setattr("meshpilot.analytics.cost.get_brand", lambda: "acme")
    monkeypatch.setenv("COST_UNKNOWN_MODEL_USD", "0.07")

    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}  # no "cost" key
    await _meter("z-ai/glm-5.3", usage, "req-2")

    assert recorded["cost_usd"] == pytest.approx(0.07)
    assert recorded["cost_usd"] != 0.0


def test_unknown_model_cost_usd_default_and_override(monkeypatch):
    from meshpilot.analytics.cost.pricing import unknown_model_cost_usd

    monkeypatch.delenv("COST_UNKNOWN_MODEL_USD", raising=False)
    assert unknown_model_cost_usd() == pytest.approx(0.05)

    monkeypatch.setenv("COST_UNKNOWN_MODEL_USD", "0.13")
    assert unknown_model_cost_usd() == pytest.approx(0.13)


def test_higgsfield_cost_credits_to_usd(monkeypatch):
    monkeypatch.setenv("COST_HIGGSFIELD_CREDIT_USD", "0.01")
    credits, usd = higgsfield_cost("higgsfield-ai/dop/standard")  # 9 credits
    assert credits == 9.0
    assert usd == pytest.approx(0.09)


def test_higgsfield_cost_zero_for_soul():
    credits, usd = higgsfield_cost("higgsfield-ai/soul/v2/standard")
    assert credits == 0.0 and usd == 0.0


# ── fake engine (no DB) ──
class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeConn:
    def __init__(self, sink, rows):
        self._sink, self._rows = sink, rows

    async def execute(self, stmt, params=None):
        self._sink.append((str(stmt), params))
        return _FakeResult(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeEngine:
    def __init__(self, rows=None):
        self.calls = []
        self._rows = rows or []

    def begin(self):
        return _FakeConn(self.calls, self._rows)

    def connect(self):
        return _FakeConn(self.calls, self._rows)


# ── meter ──
async def test_record_usage_inserts_row():
    eng = _FakeEngine()
    await record_usage(brand_id="example", vendor="anthropic", operation="chat",
                       model="claude-haiku-4-5-20251001",
                       units={"input_tokens": 10}, cost_usd=0.001, request_id="req_1", engine=eng)
    assert len(eng.calls) == 1
    stmt, params = eng.calls[0]
    assert "insert into usage_events" in stmt.lower()
    assert params["brand_id"] == "example"
    assert params["vendor"] == "anthropic"
    assert params["cost_usd"] == 0.001


async def test_record_usage_insert_is_dedup_guarded():
    # #97: the INSERT carries ON CONFLICT ... DO NOTHING keyed on (vendor, request_id)
    eng = _FakeEngine()
    await record_usage(brand_id="b", vendor="heygen", operation="video.generate",
                       request_id="vid-1", engine=eng)
    stmt = eng.calls[0][0].lower()
    assert "on conflict (vendor, request_id)" in stmt and "do nothing" in stmt


async def test_record_usage_defaults_unattributed_brand():
    eng = _FakeEngine()
    await record_usage(brand_id=None, vendor="higgsfield", operation="image.generate", engine=eng)
    assert eng.calls[0][1]["brand_id"] == "unattributed"


async def test_record_usage_is_fail_soft():
    class _Boom:
        def begin(self):
            raise RuntimeError("db down")

    # must not raise — metering never breaks the generation it measures
    await record_usage(brand_id="b", vendor="anthropic", operation="chat", engine=_Boom())


async def test_spend_summary_rolls_up_by_vendor():
    rows = [
        {"vendor": "openrouter", "events": 3, "cost_usd": 0.25, "measured_usd": 0.25, "estimated_usd": 0.0},
        {"vendor": "higgsfield", "events": 2, "cost_usd": 0.18, "measured_usd": 0.0, "estimated_usd": 0.18},
    ]
    eng = _FakeEngine(rows=rows)
    to_ts = datetime.now(timezone.utc)
    from_ts = to_ts - timedelta(days=30)
    out = await spend_summary("example", from_ts, to_ts, engine=eng)
    assert out["brand_id"] == "example"
    assert out["total_events"] == 5
    assert out["total_usd"] == pytest.approx(0.43)
    assert {v["vendor"] for v in out["by_vendor"]} == {"openrouter", "higgsfield"}
    # the split is the point: 0.25 of 0.43 is the vendor's own number, the rest is our price book
    assert out["measured_usd"] == pytest.approx(0.25)
    assert out["estimated_usd"] == pytest.approx(0.18)
    assert out["measured_share"] == pytest.approx(0.5814, abs=1e-4)


async def test_spend_summary_measured_share_is_none_when_nothing_was_spent():
    """No spend must not divide by zero — and `None` says "unknown", which 0.0 would not."""
    out = await spend_summary("example", "2026-09-20", "2026-09-21",
                              engine=_FakeEngine(rows=[]))
    assert out["total_usd"] == 0 and out["measured_share"] is None


# ── the `estimated` flag actually means something (COST-TRUTH) ──
# It was dead: no call site ever passed it, so the column was a constant `true` — including on 909
# OpenRouter rows carrying the vendor's own reported charge. A flag that is always true cannot tell
# you which numbers are trustworthy, which is the only reason to have it.

async def test_openrouter_cost_from_the_vendor_is_recorded_as_measured():
    from meshpilot.agent.loop import llm

    seen = {}

    async def fake_record(**kw):
        seen.update(kw)

    import meshpilot.analytics.cost as cost_mod
    orig = cost_mod.record_usage
    cost_mod.record_usage = fake_record
    try:
        await llm._meter("anthropic/claude-sonnet-5",
                         {"input_tokens": 10, "output_tokens": 5, "cost": 0.00421}, "req-1")
    finally:
        cost_mod.record_usage = orig
    assert seen["cost_usd"] == 0.00421
    assert seen["estimated"] is False          # the vendor priced it; this IS the bill


async def test_a_priced_from_our_book_fallback_is_recorded_as_estimated():
    from meshpilot.agent.loop import llm

    seen = {}

    async def fake_record(**kw):
        seen.update(kw)

    import meshpilot.analytics.cost as cost_mod
    orig = cost_mod.record_usage
    cost_mod.record_usage = fake_record
    try:                                        # no "cost" key → price book → an estimate
        await llm._meter("anthropic/claude-sonnet-5",
                         {"input_tokens": 1_000_000, "output_tokens": 0}, "req-2")
    finally:
        cost_mod.record_usage = orig
    assert seen["estimated"] is True
    assert seen["cost_usd"] == pytest.approx(2.0)   # sonnet-5 input $2/MTok


# ── COST-TRUTH-2: the cron job boundary must attribute its spend ──
# Measured 2026-09-21: 905 of 1,059 rows (73%, $18.22) were `unattributed`, 881 of them `chat`.
# `agent.loop.runner`, the curator and the media runner each set the brand for their own path, so
# agentTurn and media were fine — the 18 CAPABILITIES dispatched by `cron.service._dispatch` were
# not, and that is where most of the spend happens. Nothing failed; the money just lost its owner.

async def test_dispatching_a_capability_attributes_its_vendor_calls_to_the_brand():
    from meshpilot.agent.cron import capabilities, service
    from meshpilot.analytics.cost import get_brand

    seen = {}

    async def _cap(brand_id, args):
        seen["brand_from_context"] = get_brand()      # what a deep vendor call would bill
        return {"ok": True}

    async def _finish(*a, **kw):
        return None

    orig_get, orig_finish = capabilities.get, service.store.finish_run
    capabilities.get = lambda name: _cap
    service.store.finish_run = _finish
    try:
        await service._dispatch({"run_id": "r1", "id": "j1", "brand_id": "northwind",
                                 "payload_kind": "capability",
                                 "payload": {"name": "curate", "args": {}}})
    finally:
        capabilities.get, service.store.finish_run = orig_get, orig_finish
    assert seen["brand_from_context"] == "northwind"


async def test_the_brand_does_not_leak_out_of_a_dispatched_job():
    """A restoring scope, so a future inline `await _dispatch` cannot bill the next job's brand."""
    from meshpilot.agent.cron import capabilities, service
    from meshpilot.analytics.cost import get_brand, set_brand

    async def _cap(brand_id, args):
        return {"ok": True}

    async def _finish(*a, **kw):
        return None

    set_brand(None)
    orig_get, orig_finish = capabilities.get, service.store.finish_run
    capabilities.get = lambda name: _cap
    service.store.finish_run = _finish
    try:
        await service._dispatch({"run_id": "r1", "id": "j1", "brand_id": "northwind",
                                 "payload_kind": "capability",
                                 "payload": {"name": "curate", "args": {}}})
    finally:
        capabilities.get, service.store.finish_run = orig_get, orig_finish
    assert get_brand() is None


async def test_a_failing_job_still_restores_the_brand():
    """_dispatch swallows job errors; the scope must unwind on that path too."""
    from meshpilot.agent.cron import capabilities, service
    from meshpilot.analytics.cost import get_brand, set_brand

    async def _cap(brand_id, args):
        raise RuntimeError("boom")

    async def _finish(*a, **kw):
        return None

    set_brand(None)
    orig_get, orig_finish = capabilities.get, service.store.finish_run
    capabilities.get = lambda name: _cap
    service.store.finish_run = _finish
    try:
        await service._dispatch({"run_id": "r1", "id": "j1", "brand_id": "northwind",
                                 "payload_kind": "capability",
                                 "payload": {"name": "curate", "args": {}}})
    finally:
        capabilities.get, service.store.finish_run = orig_get, orig_finish
    assert get_brand() is None
