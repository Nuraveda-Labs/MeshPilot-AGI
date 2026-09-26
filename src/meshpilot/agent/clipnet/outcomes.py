"""CLIPNET-LEARN L2: read back how each posted clip performed, at 1h / 24h / 7d.

Runs as the `clipnet_outcomes` capability, hourly per brand. For every posted clip-platform row it
takes each reading once, when it falls due, and stores it in `clipnet_post_metric`.

Sources (measured 2026-09-25): Instagram + Facebook through the Meta Graph readers in
`platforms/insights.py`; TikTok / YouTube / X through Buffer's own per-post metrics. A reader that
returns None means NOT MEASURED — no row is written, so the learner never mistakes "unreadable"
for "nobody watched".
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text

log = structlog.get_logger(__name__)

BUCKETS = (("1h", 1), ("24h", 24), ("7d", 168))
BUFFER_PLATFORMS = {"tiktok", "youtube", "x"}
_COLS = ("video_views", "reach", "impressions", "likes", "comments", "shares", "saves", "clicks")

_POSTED = text(
    "SELECT p.clip_id, p.platform, p.external_id, p.updated_at, "
    "coalesce(array_agg(m.age_bucket) FILTER (WHERE m.age_bucket IS NOT NULL), '{}') AS taken "
    "FROM clipnet_post p JOIN clipnet_clip c ON c.id = p.clip_id JOIN clipnet_job j ON j.id = c.job_id "
    "LEFT JOIN clipnet_post_metric m ON m.clip_id = p.clip_id AND m.platform = p.platform "
    "WHERE j.brand_id = :b AND p.status = 'posted' AND p.external_id IS NOT NULL "
    "AND p.updated_at > now() - interval '8 days' "
    "GROUP BY p.clip_id, p.platform, p.external_id, p.updated_at"
)
_INSERT = text(
    "INSERT INTO clipnet_post_metric (clip_id, platform, age_bucket, video_views, reach, impressions, "
    "likes, comments, shares, saves, clicks, raw) VALUES (:clip_id, :platform, :bucket, :video_views, "
    ":reach, :impressions, :likes, :comments, :shares, :saves, :clicks, CAST(:raw AS jsonb)) "
    "ON CONFLICT (clip_id, platform, age_bucket) DO NOTHING"
)


def due_bucket(age_hours: float, taken: set[str]) -> str | None:
    """The single reading to take now: the LATEST bucket that is due and not yet taken.

    Late ticks never back-fill stale early readings (a '1h' taken at 30h would be mislabelled), so
    once a later bucket is due the earlier untaken ones are skipped.
    """
    due = [name for name, h in BUCKETS if age_hours >= h]
    if not due:
        return None
    latest = due[-1]
    return None if latest in taken else latest


async def _read(platform: str, external_id: str, brand_id: str) -> dict | None:
    if platform in BUFFER_PLATFORMS:
        from meshpilot.platforms.buffer import post_metrics

        return await post_metrics(external_id, brand_id=brand_id)
    from meshpilot.platforms.insights import fetch

    return await fetch(platform, external_id, brand_id=brand_id)


def _engine_or(engine: Any):
    from meshpilot.db.session import _engine

    return engine or _engine()


async def collect(brand_id: str, *, engine: Any = None, read: Any = None, now: datetime | None = None) -> dict:
    import json

    eng, read, now = _engine_or(engine), read or _read, now or datetime.now(UTC)
    async with eng.connect() as conn:
        rows = [dict(r) for r in (await conn.execute(_POSTED, {"b": brand_id})).mappings().all()]
    taken_n = unmeasured = 0
    for r in rows:
        age_h = (now - r["updated_at"]).total_seconds() / 3600
        bucket = due_bucket(age_h, set(r["taken"] or []))
        if bucket is None:
            continue
        m = await read(r["platform"], str(r["external_id"]), brand_id)
        if not m:
            unmeasured += 1
            continue
        params = {"clip_id": r["clip_id"], "platform": r["platform"], "bucket": bucket,
                  "raw": json.dumps(m.get("raw") or {}, default=str),
                  **{c: m.get(c) for c in _COLS}}
        async with eng.begin() as conn:
            await conn.execute(_INSERT, params)
        taken_n += 1
    out = {"posts": len(rows), "readings": taken_n, "unmeasured": unmeasured}
    log.info("clipnet.outcomes", brand_id=brand_id, **out)
    return out
