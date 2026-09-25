-- CLIPNET (A1) — auto clip pipeline state. Spec: docs/plans/2026-09-24-clipnet-auto-pipeline.md § 3.3.
-- Additive only. The API is the only writer of clipnet_post; the Railway worker writes job progress
-- and clips. Every table is service-role only (RLS on, no policies), like drive_post.

create table if not exists clipnet_campaign (
  slug               text primary key,
  brand_ids          text[] not null,
  subject            text not null,               -- what every clip must be ABOUT (gate check)
  required_hashtags  text[] not null default '{}',
  allowed_sources    text[] not null default '{}', -- source URLs/ids the campaign provided
  submit_platforms   text[] not null default '{}', -- what goes to Whop; WHERE to post is the brand's call
  disclosure         text,                         -- e.g. 'paid_promotion'
  submit_window_min  int  not null default 10,
  max_clips_per_day  int  not null default 5,
  active             boolean not null default true,
  created_at         timestamptz not null default now()
);

create table if not exists clipnet_job (
  id             uuid primary key default gen_random_uuid(),
  idem_key       text not null unique,             -- '<video_id>:<campaign>'
  brand_id       text not null,
  campaign       text not null references clipnet_campaign(slug),
  url            text not null,
  status         text not null default 'queued' check (status in (
                   'queued','fetching','transcribing','selecting','rendering','rendered',
                   'gated','publishing','published','failed','blocked')),
  attempt        int  not null default 0,
  lease_until    timestamptz,
  heartbeat_at   timestamptz,
  stage_outputs  jsonb not null default '{}'::jsonb, -- per-stage results, so a requeue resumes
  error          text,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create index if not exists clipnet_job_status_idx on clipnet_job (status, lease_until);

create table if not exists clipnet_clip (
  id           uuid primary key default gen_random_uuid(),
  job_id       uuid not null references clipnet_job(id) on delete cascade,
  start_s      real not null,
  end_s        real not null,
  hook         text,
  caption      text,
  media_url    text,
  gate_status  text not null default 'pending' check (gate_status in ('pending','passed','blocked')),
  gate_reason  text,
  created_at   timestamptz not null default now()
);
create index if not exists clipnet_clip_job_idx on clipnet_clip (job_id);

-- The publish ledger. A row is RESERVED before the platform call, so a retry never posts twice.
create table if not exists clipnet_post (
  clip_id      uuid not null references clipnet_clip(id) on delete cascade,
  platform     text not null,
  status       text not null default 'reserved' check (status in ('reserved','posted','failed')),
  external_id  text,
  permalink    text,
  error        text,
  updated_at   timestamptz not null default now(),
  primary key (clip_id, platform)
);

alter table clipnet_campaign enable row level security;
alter table clipnet_job      enable row level security;
alter table clipnet_clip     enable row level security;
alter table clipnet_post     enable row level security;

-- Seed: the first campaign (rules of record, 2026-09-24). allowed_sources starts with the one
-- source verified from the campaign's provided list; the operator adds the rest.
insert into clipnet_campaign (slug, brand_ids, subject, required_hashtags, allowed_sources,
                              submit_platforms, disclosure, submit_window_min, max_clips_per_day)
values ('lovable', array['ai_empire'], 'Lovable or its CEO Anton Osika',
        array['#LovablePartner'], array['youtube:9FGMhz-e97k'],
        array['instagram','tiktok','youtube'], 'paid_promotion', 10, 5)
on conflict (slug) do nothing;
