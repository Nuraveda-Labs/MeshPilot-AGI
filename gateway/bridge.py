"""MeshPilot channel gateway — Discord ↔ the MeshPilot agent.

Our own thin bridge (not OpenClaw): a discord.py gateway bot that relays messages
in the per-project chat channels to the MeshPilot agent's HTTP API and posts the
reply back. The agent is always the brain; this is dumb plumbing. Runs as one
always-on container (Railway). Discord only for now — Telegram/WhatsApp are
future adapters.

Flow: message in a mapped channel → POST /internal/agent/run {goal,brand}
      (header: x-jobs-token, that brand's) → poll GET /internal/agent/run/{id}
      until done/error → reply with `final`.

**The channel IS the brand declaration.** Posting in #ge-chat *is* saying "as
Acme Corp". That is deliberate: there is no brand inference from message
text, so a message can never silently run as the wrong brand, and "I don't like
that content" resolves against the right project's episodes without asking.

Auth stays brand-scoped (#95): each channel's call is authorised with THAT
brand's `<PREFIX>_JOBS_AUTH_TOKEN`, so one project's chat cannot act on another.

Env:
  DISCORD_BOT_TOKEN          the bot token
  DISCORD_CHANNEL_BRANDS     JSON: {"<channel_id>": {"brand": "<brand_id>", "prefix": "<ENV_PREFIX>"}}
                             e.g. {"123": {"brand": "exampleco_lab", "prefix": "NL"}}
  <PREFIX>_JOBS_AUTH_TOKEN   per brand, the SAME name the API uses (ACME_JOBS_AUTH_TOKEN, …).
                             ⚠️ Deliberately not a second copy under a gateway-specific name: a
                             duplicate of GE's token under MESHPILOT_JOBS_TOKEN went stale after a
                             rotation and 401'd every message in #agent-chat until 2026-09-19.
                             One secret, one name.
  MESHPILOT_URL              agent base URL (default https://api.meshpilot.app)
  AGENT_MAX_STEPS            per-turn step cap (default 6)
  POLL_INTERVAL_S / POLL_TIMEOUT_S   run polling cadence + ceiling

  Legacy single-channel fallback, used only when DISCORD_CHANNEL_BRANDS is unset:
  DISCORD_AGENT_CHANNEL_ID + MESHPILOT_BRAND + MESHPILOT_JOBS_TOKEN
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

import discord
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("meshpilot.gateway")

AGENT_URL = os.environ.get("MESHPILOT_URL", "https://api.meshpilot.app").rstrip("/")
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "6"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL_S", "2"))
POLL_TIMEOUT = float(os.environ.get("POLL_TIMEOUT_S", "180"))

# SCHED-1 — hold the API awake.
#
# FastAPI Cloud scales the API container to zero on idle, and the in-app cron scheduler dies
# with the process: measured 2026-09-21, `startup complete` appeared in the SAME SECOND as the
# inbound request ending each gap (72 min and 109 min), i.e. the process was BOOTING, not
# resuming. Idle is counted on INBOUND HTTP, not on work done — the app was sweeping every 15
# minutes right up to the cut and was scaled down anyway.
#
# This gateway is already always-on for the Discord websocket, so it is the cheapest possible
# place to put a heartbeat: no new service, no new cost, and no new credential, because
# `/healthz` is deliberately unauthenticated.
#
# 10 min against a measured ~60 min idle window. See docs/plans/2026-09-21-scheduler-stalls.md.
KEEPALIVE_INTERVAL = float(os.environ.get("KEEPALIVE_INTERVAL_S", "600"))
KEEPALIVE_ENABLED = os.environ.get("KEEPALIVE_ENABLED", "1").strip().lower() not in ("0", "false", "no")


class Route:
    """One chat channel and the brand it speaks as."""

    __slots__ = ("channel_id", "brand", "prefix", "token")

    def __init__(self, channel_id: int, brand: str, prefix: str, token: str):
        self.channel_id, self.brand, self.prefix, self.token = channel_id, brand, prefix, token

    def __repr__(self) -> str:  # never render the token
        return f"Route(channel={self.channel_id}, brand={self.brand!r}, prefix={self.prefix!r})"


def parse_routes(env: dict[str, str]) -> tuple[dict[int, Route], list[str]]:
    """Build channel_id -> Route from the environment.

    Returns (routes, problems). A brand whose token is missing is DROPPED with a problem
    recorded rather than silently accepted: a route that cannot authenticate would 401 on
    every message, which is exactly the failure this gateway shipped with for weeks.
    """
    routes: dict[int, Route] = {}
    problems: list[str] = []

    raw = (env.get("DISCORD_CHANNEL_BRANDS") or "").strip()
    if raw:
        try:
            mapping = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {}, [f"DISCORD_CHANNEL_BRANDS is not valid JSON: {exc}"]
        if not isinstance(mapping, dict):
            return {}, ["DISCORD_CHANNEL_BRANDS must be a JSON object keyed by channel id"]
        for cid, spec in mapping.items():
            if not isinstance(spec, dict) or not spec.get("brand") or not spec.get("prefix"):
                problems.append(f"channel {cid}: needs both 'brand' and 'prefix'")
                continue
            try:
                channel_id = int(cid)
            except (TypeError, ValueError):
                problems.append(f"channel key {cid!r} is not an integer channel id")
                continue
            prefix = str(spec["prefix"])
            token = env.get(f"{prefix}_JOBS_AUTH_TOKEN", "")
            if not token:
                problems.append(
                    f"channel {channel_id} ({spec['brand']}): {prefix}_JOBS_AUTH_TOKEN is unset — "
                    f"route dropped, it could only 401")
                continue
            routes[channel_id] = Route(channel_id, str(spec["brand"]), prefix, token)
        return routes, problems

    # Legacy single-channel mode.
    # ⚠️ No default brand. This used to fall back to a hardcoded brand id, which in a
    # CREDENTIAL-ROUTING path means: if MESHPILOT_BRAND were unset, every message on this
    # channel would be attributed to a brand nobody named. Everything else in this function
    # drops a route and records a problem rather than guessing; this now matches.
    cid, brand = env.get("DISCORD_AGENT_CHANNEL_ID"), env.get("MESHPILOT_BRAND", "")
    token = env.get("MESHPILOT_JOBS_TOKEN", "")
    if cid and token and not brand:
        problems.append(
            "legacy single-channel mode: MESHPILOT_BRAND is unset — route dropped rather than "
            "guessing which brand this channel speaks for")
    elif cid and token:
        routes[int(cid)] = Route(int(cid), brand, "MESHPILOT", token)
        problems.append("using legacy DISCORD_AGENT_CHANNEL_ID mode; set DISCORD_CHANNEL_BRANDS")
    else:
        problems.append("no DISCORD_CHANNEL_BRANDS and no usable legacy single-channel config")
    return routes, problems


# ── CLIPNET #clip-queue ─────────────────────────────────────────────────────────────────────────
# A clip-queue channel is a SEPARATE mapping (CLIPQUEUE_CHANNEL_BRANDS, same shape as
# DISCORD_CHANNEL_BRANDS plus an optional default "campaign"). The channel is still the brand
# declaration: links pasted there are queued for THAT brand with THAT brand's token, never inferred.
_YT = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?[^\s]*v=|shorts/|live/)|youtu\.be/)"
                 r"[A-Za-z0-9_-]{11}[^\s>]*")
_CAMPAIGN = re.compile(r"campaign\s*[:=]\s*([a-z0-9][a-z0-9_-]*)", re.I)


def parse_clip_routes(env: dict[str, str]) -> tuple[dict[int, tuple[Route, str]], list[str]]:
    """channel_id -> (Route, default_campaign). A route whose token is missing is dropped."""
    raw = (env.get("CLIPQUEUE_CHANNEL_BRANDS") or "").strip()
    if not raw:
        return {}, []
    routes, problems = parse_routes({**env, "DISCORD_CHANNEL_BRANDS": raw})
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError:
        mapping = {}
    out = {cid: (r, str((mapping.get(str(cid)) or {}).get("campaign") or "")) for cid, r in routes.items()}
    return out, [f"clip-queue: {p}" for p in problems]


def parse_clip_message(text: str) -> tuple[list[str], str]:
    """(YouTube URLs in order, deduplicated; the campaign named in the message or '')."""
    urls: list[str] = []
    for m in _YT.finditer(text or ""):
        u = m.group(0).rstrip(".,)")
        if u not in urls:
            urls.append(u)
    c = _CAMPAIGN.search(text or "")
    return urls, (c.group(1).lower() if c else "")


async def queue_clip(route: Route, url: str, campaign: str) -> str:
    """Queue one link; return the reply line for Discord."""
    async with httpx.AsyncClient(timeout=30.0) as h:
        r = await h.post(f"{AGENT_URL}/internal/clipnet/jobs", params={"brand": route.brand},
                         headers={"x-jobs-token": route.token, "User-Agent": "meshpilot-gateway/1.0"},
                         json={"url": url, "campaign": campaign})
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:200]
        return f"❌ {url} — {detail}"
    d = r.json()
    verb = "queued" if d.get("created") else f"already known ({d.get('status')})"
    return f"✅ {verb}: {url} · job `{d.get('job_id', '')[:8]}`"


async def check_auth(route: Route) -> str:
    """Probe the API with this route's token. Returns '' when fine, else a reason.

    Runs at startup so a stale token is loud IMMEDIATELY instead of surfacing as a 401 the
    first time someone talks to the agent — the exact way the last outage stayed hidden.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as h:
            r = await h.get(f"{AGENT_URL}/internal/agent/run/"
                            "00000000-0000-0000-0000-000000000000",
                            headers={"x-jobs-token": route.token},
                            params={"brand": route.brand})
    except Exception as exc:  # noqa: BLE001 — a probe failure must not stop the bridge
        return f"probe failed: {str(exc)[:120]}"
    if r.status_code in (401, 403):
        return f"token REJECTED ({r.status_code}) — {route.prefix}_JOBS_AUTH_TOKEN is wrong or stale"
    return ""


async def run_agent(route: Route, goal: str) -> str:
    """Start an agent run for `goal` as this route's brand, poll, return the final text."""
    headers = {"x-jobs-token": route.token, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as h:
        # brand goes on the QUERY STRING: _require_jobs_auth validates the token against ?brand=,
        # and the handler derives its target brand from ?brand= (not the body). Kept in the body
        # too (must match) for backward compatibility.
        r = await h.post(f"{AGENT_URL}/internal/agent/run", headers=headers,
                         params={"brand": route.brand},
                         json={"goal": goal, "brand": route.brand, "max_steps": MAX_STEPS})
        r.raise_for_status()
        run_id = r.json()["run_id"]

        loop = asyncio.get_event_loop()
        deadline = loop.time() + POLL_TIMEOUT
        while loop.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            g = await h.get(f"{AGENT_URL}/internal/agent/run/{run_id}",
                            headers=headers, params={"brand": route.brand})
            g.raise_for_status()
            rec = g.json()
            status = rec.get("status")
            if status == "done":
                return (rec.get("final") or "").strip() or "(the agent finished but returned no text)"
            if status == "error":
                return f"⚠️ agent error: {rec.get('error') or 'unknown'}"
        return "⏱️ the agent is taking longer than expected — it may still finish; try again."


ROUTES, PROBLEMS = parse_routes(dict(os.environ))
CLIP_ROUTES, CLIP_PROBLEMS = parse_clip_routes(dict(os.environ))

intents = discord.Intents.default()
intents.message_content = True  # requires "Message Content Intent" enabled in the dev portal
def scheduler_age_s(body: object) -> int | None:
    """Pull `scheduler.last_run_age_s` out of a /healthz body. None when absent or unusable.

    Pure, so the shape contract is testable without a network or a running API. Returns None
    rather than raising on anything unexpected: this feeds a log line on a bridge whose actual
    job is Discord, and a heartbeat must never be able to take the websocket down with it.
    """
    if not isinstance(body, dict):
        return None
    sched = body.get("scheduler")
    if not isinstance(sched, dict):
        return None
    age = sched.get("last_run_age_s")
    if isinstance(age, bool) or not isinstance(age, (int, float)):
        return None
    return int(age)


async def _keepalive_once() -> int | None:
    """One /healthz probe. Returns last_run_age_s, or None if the probe told us nothing."""
    async with httpx.AsyncClient(timeout=30.0) as h:
        r = await h.get(f"{AGENT_URL}/healthz")
    if r.status_code != 200:
        log.warning("keepalive: /healthz returned %s", r.status_code)
        return None
    try:
        return scheduler_age_s(r.json())
    except ValueError:
        log.warning("keepalive: /healthz body was not JSON")
        return None


async def keepalive_loop() -> None:
    """Probe /healthz forever so the API container is never idle long enough to be reclaimed.

    Never exits and never raises: modelled on the API's own `_scheduler_loop`, for the same
    reason — a keepalive that dies on one bad tick is a keepalive that stops silently, which is
    the exact failure it exists to prevent.
    """
    log.info("keepalive: every %.0fs against %s", KEEPALIVE_INTERVAL, AGENT_URL)
    while True:
        try:
            age = await _keepalive_once()
            log.info("keepalive: ok last_run_age_s=%s", "?" if age is None else age)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a heartbeat must not crash the bridge
            log.warning("keepalive: probe failed: %s", str(exc)[:200])
        await asyncio.sleep(KEEPALIVE_INTERVAL)


client = discord.Client(intents=intents)


_keepalive_task: asyncio.Task | None = None


@client.event
async def on_ready():
    # ⚠️ on_ready fires again on EVERY Discord reconnect, so guard the task: without this a
    # flapping websocket silently accumulates keepalive loops, each with its own timer.
    global _keepalive_task
    if KEEPALIVE_ENABLED and (_keepalive_task is None or _keepalive_task.done()):
        _keepalive_task = asyncio.create_task(keepalive_loop())
    log.info("bridge online as %s (guilds=%d, routes=%d)", client.user, len(client.guilds), len(ROUTES))
    for r in ROUTES.values():
        log.info("  route %s", r)
    for p in PROBLEMS + CLIP_PROBLEMS:
        log.error("  config problem: %s", p)
    for cid, (r, camp) in CLIP_ROUTES.items():
        log.info("  clip-queue channel %s -> %s (default campaign %r)", cid, r.brand, camp)
    for r in ROUTES.values():
        why = await check_auth(r)
        log.error("  auth FAILED for %s: %s", r.brand, why) if why else \
            log.info("  auth ok for %s", r.brand)


@client.event
async def on_message(msg: discord.Message):
    clip = CLIP_ROUTES.get(msg.channel.id)
    if clip is not None and not msg.author.bot:
        route, default_campaign = clip
        urls, named = parse_clip_message(msg.content or "")
        if not urls:
            await msg.reply("Paste one or more YouTube links (optional: `campaign: <slug>`).",
                            mention_author=False)
            return
        campaign = named or default_campaign
        if not campaign:
            await msg.reply("Which campaign? Add `campaign: <slug>` to the message.", mention_author=False)
            return
        log.info("clip-queue: brand=%s urls=%d campaign=%s", route.brand, len(urls), campaign)
        lines = []
        for u in urls[:10]:
            try:
                lines.append(await queue_clip(route, u, campaign))
            except Exception as exc:  # noqa: BLE001 — report, don't crash the bot
                lines.append(f"❌ {u} — couldn't reach the agent: {str(exc)[:120]}")
        await msg.reply("\n".join(lines)[:1900], mention_author=False)
        return
    route = ROUTES.get(msg.channel.id)
    if msg.author.bot or route is None:
        return
    goal = (msg.content or "").strip()
    if not goal:
        return
    log.info("relay: brand=%s user=%s len=%d", route.brand, msg.author, len(goal))
    async with msg.channel.typing():
        try:
            reply = await run_agent(route, goal)
        except Exception as exc:  # noqa: BLE001 — surface the failure, don't crash the bot
            log.exception("agent call failed for brand=%s", route.brand)
            reply = f"⚠️ couldn't reach the agent: {str(exc)[:200]}"
    # Discord caps a message at 2000 chars — chunk long replies.
    for i in range(0, len(reply), 1900):
        await msg.reply(reply[i:i + 1900], mention_author=False)


if __name__ == "__main__":
    if not ROUTES and not CLIP_ROUTES:
        for p in PROBLEMS + CLIP_PROBLEMS:
            log.error("config problem: %s", p)
        raise SystemExit("no usable chat routes — refusing to start")
    client.run(os.environ["DISCORD_BOT_TOKEN"])
