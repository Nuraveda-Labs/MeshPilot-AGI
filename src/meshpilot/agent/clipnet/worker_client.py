"""Start one Cloud Run Job execution of the CLIPNET render worker (spec D6, amended 2026-09-24).

The worker is a Cloud Run JOB in MeshPilot's own GCP project; each clipnet_job gets one execution
with JOB_ID / BUCKET overridden. Auth is MeshPilot's own service account (the unprefixed
GOOGLE_DRIVE_SA_JSON — agent infrastructure, not a brand credential), which holds
roles/run.developer on the job and actAs on the worker's runtime service account.
"""
from __future__ import annotations

import json
import os
from typing import Any

import structlog

log = structlog.get_logger(__name__)
_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _job_path() -> str:
    project = os.environ.get("CLIPNET_GCP_PROJECT", "capable-boulder-487806-j0")
    region = os.environ.get("CLIPNET_GCP_REGION", "us-east4")
    job = os.environ.get("CLIPNET_GCP_JOB", "clip-worker")
    return f"projects/{project}/locations/{region}/jobs/{job}"


def _session() -> Any:
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    raw = (os.environ.get("GOOGLE_DRIVE_SA_JSON") or "").strip()
    if not raw:
        raise RuntimeError("clipnet: GOOGLE_DRIVE_SA_JSON (MeshPilot's own SA) is not set")
    info = json.loads(raw) if raw.startswith("{") else json.load(open(raw))
    creds = service_account.Credentials.from_service_account_info(info, scopes=[_SCOPE])
    return AuthorizedSession(creds)


def run_body(job_id: str, bucket: str, n_clips: int) -> dict:
    """The Cloud Run v2 `jobs.run` body: per-execution env overrides for the one container."""
    return {"overrides": {"containerOverrides": [{"env": [
        {"name": "JOB_ID", "value": job_id},
        {"name": "BUCKET", "value": bucket},
        {"name": "N_CLIPS", "value": str(n_clips)},
    ]}]}}


def start_execution(job_id: str, bucket: str, n_clips: int = 2, *, session: Any = None) -> str:
    """Start an execution; returns the long-running operation name. Raises on a non-2xx."""
    s = session or _session()
    r = s.post(f"https://run.googleapis.com/v2/{_job_path()}:run", json=run_body(job_id, bucket, n_clips),
               timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"cloud run jobs.run -> {r.status_code}: {r.text[:300]}")
    op = r.json().get("name", "")
    log.info("clipnet.execution_started", job_id=job_id, bucket=bucket, operation=op)
    return op
