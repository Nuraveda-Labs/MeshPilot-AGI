"""CLIPNET-LEARN L2: clip posts get measured, and "unreadable" is never recorded as zero."""
from datetime import UTC, datetime, timedelta

import pytest

from meshpilot.agent.clipnet import outcomes
from meshpilot.platforms import buffer, insights


def test_instagram_asks_for_saved_not_saves():
    """Meta rejects `saves`; one bad name failed every IG read (measured 2026-09-25)."""
    assert "saved" in insights._IG_METRICS and "saves" not in insights._IG_METRICS


async def test_a_bare_numeric_facebook_id_is_read_as_a_video(monkeypatch):
    seen = []

    async def video(vid, **k):
        seen.append(("video", vid))
        return {"video_views": 1}

    async def post(pid, **k):
        seen.append(("post", pid))
        return {"likes": 1}

    monkeypatch.setattr(insights, "facebook_video", video)
    monkeypatch.setattr(insights, "facebook_post", post)
    await insights.fetch("facebook", "4464868897059413")
    await insights.fetch("facebook", "1133702696499891_555")
    assert seen == [("video", "4464868897059413"), ("post", "1133702696499891_555")]


def test_buffer_metric_names_map_to_our_columns():
    m = [{"name": "Video Views", "value": 12}, {"name": "Reactions", "value": "3"}, {"name": "Reposts", "value": 2},
         {"name": "Eng. Rate", "value": 0.4}, {"name": "Watch Time (min)", "value": 9}]
    assert buffer.normalise_metrics(m) == {"video_views": 12, "likes": 3, "shares": 2}


async def test_buffer_without_a_measurement_is_none_not_zero(monkeypatch):
    async def g(*a, **k):
        return {"post": {"metricsUpdatedAt": None, "metrics": [{"name": "Video Views", "value": 0}]}}

    monkeypatch.setattr(buffer, "_graphql", g)
    monkeypatch.setattr(buffer, "_buffer_token", lambda b: "t")
    assert await buffer.post_metrics("p1", brand_id="ai_empire") is None


async def test_buffer_zeros_with_a_timestamp_are_a_real_measurement(monkeypatch):
    async def g(*a, **k):
        return {"post": {"metricsUpdatedAt": "2026-09-25T04:00:00Z", "metrics": [{"name": "Video Views", "value": 0}]}}

    monkeypatch.setattr(buffer, "_graphql", g)
    monkeypatch.setattr(buffer, "_buffer_token", lambda b: "t")
    assert (await buffer.post_metrics("p1", brand_id="ai_empire"))["video_views"] == 0


@pytest.mark.parametrize("age,taken,want", [
    (0.5, set(), None), (1.5, set(), "1h"), (1.5, {"1h"}, None),
    (30, {"1h"}, "24h"), (30, set(), "24h"),          # late tick: never back-fill a stale 1h
    (200, {"1h", "24h"}, "7d"), (200, {"1h", "24h", "7d"}, None),
])
def test_due_bucket_takes_only_the_latest_due_reading(age, taken, want):
    assert outcomes.due_bucket(age, taken) == want


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Conn:
    def __init__(self, rows, log):
        self.rows, self.log = rows, log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, params=None):
        self.log.append(params)
        return _Result(self.rows)


class _Eng:
    def __init__(self, rows):
        self.rows, self.log = rows, []

    def connect(self):
        return _Conn(self.rows, self.log)

    def begin(self):
        return _Conn([], self.log)


async def test_collect_writes_a_reading_and_skips_the_unmeasurable():
    now = datetime(2026, 9, 26, 12, tzinfo=UTC)
    rows = [{"clip_id": "c1", "platform": "instagram", "external_id": "m1", "updated_at": now - timedelta(hours=25), "taken": ["1h"]},
            {"clip_id": "c1", "platform": "tiktok", "external_id": "b1", "updated_at": now - timedelta(hours=25), "taken": []}]

    async def read(platform, ext, brand):
        return {"video_views": 52, "raw": {}} if platform == "instagram" else None

    eng = _Eng(rows)
    out = await outcomes.collect("ai_empire", engine=eng, read=read, now=now)
    assert out == {"posts": 2, "readings": 1, "unmeasured": 1}
    inserted = [p for p in eng.log if p and p.get("bucket")]
    assert inserted[0]["bucket"] == "24h" and inserted[0]["video_views"] == 52
