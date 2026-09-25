"""brand_env_or_default — a brand's own value, falling back to the agent-wide default app."""
from __future__ import annotations

from meshpilot.config import brand_env_or_default


def test_falls_back_to_global_default(monkeypatch):
    monkeypatch.delenv("ACME_SYSTEM_USER_TOKEN", raising=False)
    monkeypatch.setenv("SYSTEM_USER_TOKEN", "meshpilot_default")     # agent-wide default
    assert brand_env_or_default("SYSTEM_USER_TOKEN", "example") == "meshpilot_default"


def test_brand_value_overrides_default(monkeypatch):
    monkeypatch.setenv("SYSTEM_USER_TOKEN", "meshpilot_default")
    monkeypatch.setenv("ACME_SYSTEM_USER_TOKEN", "acme_own")            # project brings its own
    assert brand_env_or_default("SYSTEM_USER_TOKEN", "example") == "acme_own"


def test_empty_when_neither_set(monkeypatch):
    monkeypatch.delenv("ACME_META_PAGE_ID", raising=False)
    monkeypatch.delenv("META_PAGE_ID", raising=False)
    assert brand_env_or_default("META_PAGE_ID", "example") == ""
