"""Usage recorder + spend rollup (COST-METER INC-1).

`record_usage` writes one row to `usage_events` at our own choke points (every model / media
call). It is FAIL-SOFT by construction: metering must never break the generation it measures, so
any DB or Logfire error is swallowed and logged, not raised. `spend_summary` rolls the events up
per vendor for the `/internal/analytics/spend` endpoint.

The SQLAlchemy engine is injectable so this unit-tests without a real DB.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from meshpilot.db.session import _engine

log = logging.getLogger("meshpilot.cost")

_INSERT = text(
    "INSERT INTO usage_events (brand_id, vendor, operation, model, units, cost_usd, estimated, request_id) "
    "VALUES (:brand_id, :vendor, :operation, :model, cast(:units as jsonb), :cost_usd, :estimated, :request_id) "
    # dedup: a retried/re-polled call carrying the same vendor request_id must not double-count (#97)
    "ON CONFLICT (vendor, request_id) WHERE request_id IS NOT NULL DO NOTHING"
)
_SUMMARY = text(
    "SELECT vendor, count(*) as events, coalesce(sum(cost_usd), 0) as cost_usd, "
    # split the money by how we know it: a vendor-reported charge vs our own price book
    "coalesce(sum(cost_usd) FILTER (WHERE NOT estimated), 0) as measured_usd, "
    "coalesce(sum(cost_usd) FILTER (WHERE estimated), 0) as estimated_usd "
    "FROM usage_events "
    "WHERE brand_id = :brand_id AND created_at >= :from_ts AND created_at < :to_ts "
    "GROUP BY vendor ORDER BY cost_usd DESC"
)


def _logfire_emit(brand_id: str, vendor: str, operation: str, model: str | None, units: dict, cost_usd: float) -> None:
    """Best-effort span so spend shows up in observability alongside the DB row."""
    import os  # noqa: PLC0415

    if not os.environ.get("LOGFIRE_TOKEN"):
        return  # Logfire only configured (in main.py) when a token is present
    try:
        import logfire  # noqa: PLC0415

        logfire.info(
            "vendor_usage",
            **{
                "brand.id": brand_id,
                "gen_ai.system": vendor,
                "gen_ai.operation.name": operation,
                "gen_ai.request.model": model,
                "gen_ai.usage.input_tokens": units.get("input_tokens"),
                "gen_ai.usage.output_tokens": units.get("output_tokens"),
                "cost.usd": cost_usd,
                "cost.units": units,
            },
        )
    except Exception:  # noqa: BLE001 — observability must never break metering
        pass


def _warn_unknown_brand(brand: str) -> None:
    """Say something when spend lands on a brand id that is not in the registry.

    a brand's env PREFIX and its brand_id both existed in `usage_events` (2026-09-21) — e.g. `acme`
    alongside `acme_supply` — because nothing checks
    the id on the way in: a typo does not fail, it silently opens a second bucket, and the split is
    invisible until someone sums the table by hand.

    Deliberately a WARNING and not a rejection. Dropping the row would lose real money from the
    ledger to punish a typo, and `unattributed` is itself a legitimate id here (a call outside any
    job boundary). Loud and recorded beats correct-looking and missing.
    """
    try:
        if brand == "unattributed":
            return
        from meshpilot.config import brand_ids  # noqa: PLC0415

        known = set(brand_ids())
        if known and brand not in known:
            log.warning("cost.unknown_brand", brand_id=brand, known=sorted(known))
    except Exception:  # noqa: BLE001 — a registry lookup must never break metering
        pass


async def record_usage(
    *,
    brand_id: str | None,
    vendor: str,
    operation: str,
    model: str | None = None,
    units: dict | None = None,
    cost_usd: float = 0.0,
    estimated: bool = True,
    request_id: str | None = None,
    engine: Any | None = None,
) -> None:
    """Record one billable vendor call, attributed to `brand_id`. Never raises."""
    brand = brand_id or "unattributed"
    _warn_unknown_brand(brand)
    u = units or {}
    try:
        eng = engine or _engine()
        async with eng.begin() as conn:
            await conn.execute(
                _INSERT,
                {
                    "brand_id": brand,
                    "vendor": vendor,
                    "operation": operation,
                    "model": model,
                    "units": json.dumps(u),
                    "cost_usd": cost_usd,
                    "estimated": estimated,
                    "request_id": request_id,
                },
            )
    except Exception as e:  # noqa: BLE001 — fail-soft: metering never breaks generation
        log.warning("record_usage failed (%s/%s brand=%s): %s", vendor, operation, brand, e)
    _logfire_emit(brand, vendor, operation, model, u, cost_usd)


async def spend_summary(brand_id: str, from_ts, to_ts, *, engine: Any | None = None) -> dict:
    """Per-vendor spend rollup for a brand over [from_ts, to_ts). Returns totals + by-vendor breakdown."""
    eng = engine or _engine()
    async with eng.connect() as conn:
        rows = (await conn.execute(_SUMMARY, {"brand_id": brand_id, "from_ts": from_ts, "to_ts": to_ts})).mappings().all()
    by_vendor = [
        {"vendor": r["vendor"], "events": int(r["events"]), "cost_usd": float(r["cost_usd"]),
         "measured_usd": round(float(r["measured_usd"]), 6),
         "estimated_usd": round(float(r["estimated_usd"]), 6)} for r in rows
    ]
    total = round(sum(v["cost_usd"] for v in by_vendor), 6)
    measured = round(sum(v["measured_usd"] for v in by_vendor), 6)
    return {
        "brand_id": brand_id,
        "from": from_ts.isoformat() if hasattr(from_ts, "isoformat") else str(from_ts),
        "to": to_ts.isoformat() if hasattr(to_ts, "isoformat") else str(to_ts),
        "total_usd": total,
        "measured_usd": measured,
        "estimated_usd": round(total - measured, 6),
        # what share of this figure is the vendor's own number rather than our arithmetic. Only the
        # estimated remainder can drift, so this is the number that says how far to trust the total.
        "measured_share": round(measured / total, 4) if total > 0 else None,
        "total_events": sum(v["events"] for v in by_vendor),
        "by_vendor": by_vendor,
    }
