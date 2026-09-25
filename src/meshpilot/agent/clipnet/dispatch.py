"""CLIPNET dispatcher (sub-lane A3): turn queued jobs into worker executions, and heal stuck ones.

Runs as the `clipnet_dispatch` capability on a short cron, per brand. One tick:
  1. REQUEUE  in-progress jobs whose worker heartbeat is stale (the execution died or timed out).
  2. FAIL     queued jobs that have used up MAX_ATTEMPTS.
  3. START    the oldest queued job — only if no job for this brand is running, and only if it was
              not started in the last DISPATCH_GRACE_MIN (a started execution needs a minute to
              claim its lease; starting it again in that window would double-run it).

A started job is marked by setting heartbeat_at while status stays `queued`: the worker's own claim
needs `lease_until` free, so the dispatcher must not touch the lease.
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text

log = structlog.get_logger(__name__)
MAX_ATTEMPTS = 3
STALE_HEARTBEAT_MIN = 4
DISPATCH_GRACE_MIN = 10
ACTIVE = ("fetching", "transcribing", "selecting", "rendering")

_REQUEUE = text(
    "UPDATE clipnet_job SET status='queued', attempt=attempt+1, lease_until=NULL, "
    "error='stale heartbeat: the worker stopped reporting', updated_at=now() "
    "WHERE brand_id=:b AND status = ANY(:active) "
    "AND coalesce(heartbeat_at, updated_at) < now() - make_interval(mins => :stale) RETURNING id"
)
_FAIL = text(
    "UPDATE clipnet_job SET status='failed', updated_at=now() "
    "WHERE brand_id=:b AND status='queued' AND attempt >= :max RETURNING id"
)
_RUNNING = text(
    "SELECT count(*) FROM clipnet_job WHERE brand_id=:b AND ("
    "status = ANY(:active) OR (status='queued' AND heartbeat_at > now() - make_interval(mins => :grace)))"
)
_NEXT = text(
    "UPDATE clipnet_job SET heartbeat_at=now(), updated_at=now() WHERE id = ("
    "SELECT id FROM clipnet_job WHERE brand_id=:b AND status='queued' AND attempt < :max "
    "AND (heartbeat_at IS NULL OR heartbeat_at < now() - make_interval(mins => :grace)) "
    "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id"
)
_NEW_BLOCKED = text(
    "UPDATE clipnet_job SET stage_outputs = stage_outputs || '{\"notified_blocked\": true}'::jsonb "
    "WHERE brand_id=:b AND status='blocked' AND NOT (stage_outputs ? 'notified_blocked') "
    "RETURNING id, url, error"
)
_FAILED_INFO = text("SELECT id, url, error FROM clipnet_job WHERE id = ANY(CAST(:ids AS uuid[]))")
_UNMARK = text("UPDATE clipnet_job SET heartbeat_at=NULL, error=:e, updated_at=now() WHERE id=:id")


def _engine_or(engine: Any):
    from meshpilot.db.session import _engine

    return engine or _engine()


async def dispatch(brand_id: str, *, engine: Any = None, start: Any = None, bucket: str | None = None,
                   notify_fn: Any = None) -> dict:
    from meshpilot.agent.clipnet import worker_client
    from meshpilot.media.generation.storage import bucket_for

    start = start or worker_client.start_execution
    bucket = bucket or bucket_for(brand_id)
    eng = _engine_or(engine)
    p = {"b": brand_id, "active": list(ACTIVE), "stale": STALE_HEARTBEAT_MIN,
         "max": MAX_ATTEMPTS, "grace": DISPATCH_GRACE_MIN}
    async with eng.begin() as conn:
        requeued = [str(r[0]) for r in (await conn.execute(_REQUEUE, p)).all()]
        failed = [str(r[0]) for r in (await conn.execute(_FAIL, p)).all()]
        running = int((await conn.execute(_RUNNING, p)).scalar() or 0)
        job_id = None if running else (await conn.execute(_NEXT, p)).scalar()
        newly_blocked = [dict(r) for r in (await conn.execute(_NEW_BLOCKED, p)).mappings().all()]
        failed_info = ([dict(r) for r in (await conn.execute(_FAILED_INFO, {"ids": failed})).mappings().all()]
                       if failed else [])
    started = None
    if job_id is not None:
        try:
            start(str(job_id), bucket)
            started = str(job_id)
        except Exception as exc:  # the worker never started: clear the mark so the next tick retries
            async with eng.begin() as conn:
                await conn.execute(_UNMARK, {"id": job_id, "e": f"dispatch failed: {exc}"[:500]})
            log.warning("clipnet.dispatch_failed", brand_id=brand_id, job_id=str(job_id), error=str(exc)[:200])
    if notify_fn is None:
        from meshpilot.agent.clipnet.notify import notify as notify_fn
    for j in failed_info:
        await notify_fn(brand_id, f"❌ **Clip job failed after {MAX_ATTEMPTS} attempts:** {j['url']}\n{(j['error'] or '')[:300]}")
    for j in newly_blocked:
        await notify_fn(brand_id, f"🛑 **Clip job blocked — needs you:** {j['url']}\n{(j['error'] or '')[:300]}")
    out = {"requeued": requeued, "failed": failed, "running": running, "started": started}
    log.info("clipnet.dispatch", brand_id=brand_id, **out)
    return out
