-- CLIPNET-LEARN L2 — per-platform readings for posted clips (1h / 24h / 7d after posting).
-- Additive. One reading per (clip, platform, bucket); re-collection is idempotent. NULL means
-- "not measured", never zero: a platform that returned nothing leaves no row.
create table if not exists clipnet_post_metric (
  clip_id       uuid not null references clipnet_clip(id) on delete cascade,
  platform      text not null,
  age_bucket    text not null check (age_bucket in ('1h','24h','7d')),
  collected_at  timestamptz not null default now(),
  video_views   bigint,
  reach         bigint,
  impressions   bigint,
  likes         bigint,
  comments      bigint,
  shares        bigint,
  saves         bigint,
  clicks        bigint,
  raw           jsonb not null default '{}'::jsonb,
  primary key (clip_id, platform, age_bucket)
);
create index if not exists clipnet_post_metric_clip_idx on clipnet_post_metric (clip_id);
alter table clipnet_post_metric enable row level security;
