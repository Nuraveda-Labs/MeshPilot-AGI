-- COST-TRUTH — `usage_events.estimated` was a constant, and the constant was wrong.
--
-- `record_usage` has always taken `estimated` with a default of `true`, and NOT ONE of the five
-- call sites ever passed it. So every row said "estimated" — including 909 OpenRouter rows whose
-- `units->>'cost'` is the charge OpenRouter itself returned (we send `usage: {include: true}`, and
-- `_meter` already preferred that value over the price book). The number was the bill; only the
-- label was a guess.
--
-- That mislabelling cost real analysis time: it supported a confident, WRONG conclusion that the
-- meter understated spend by 35%. It does not. Measured 2026-09-21, this key's own OpenRouter
-- usage was $17.76 against our metered $17.61 — a 0.85% gap. The apparent 35% was other API keys
-- on the same OpenRouter account (career-ops, the Kimi bridge), not metering error.
--
-- The presence of `units->>'cost'` is definitive: it only exists when the vendor priced the call.
-- Backfilling on it is safe and reversible (`update usage_events set estimated = true` restores).
update usage_events
   set estimated = false
 where vendor = 'openrouter'
   and estimated
   and units ? 'cost'
   and (units->>'cost') is not null;

comment on column usage_events.estimated is
  'false = cost_usd is the vendor''s own reported charge (the bill). true = we derived it from the price book in analytics/cost/pricing.py, so it can drift and is what nightly-reconcile checks. Set explicitly at every call site: do not let it default.';
