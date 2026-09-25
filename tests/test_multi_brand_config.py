"""Multi-brand config layer — discovery, validation, isolation."""
from __future__ import annotations

import json
import os
import pathlib

import pytest

# Minimal env so config.Settings() can instantiate without a real .env.
os.environ.setdefault("DISPATCH_MODE", "dry_run")
os.environ.setdefault("SIGNAL_DB_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN_SIGNAL", "0:test")
os.environ.setdefault("TELEGRAM_ADMIN_IDS", "0")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("GOOGLE_API_KEY", "test")


@pytest.fixture(autouse=True)
def _reset_registry():
    """Drop the cached brand registry AND the cached Settings before and
    after each test — otherwise monkeypatched env vars leak into subsequent
    tests via the lru_cache on settings().
    """
    from meshpilot import config as cfg

    cfg._reset_brand_registry_for_tests()
    cfg.settings.cache_clear()
    yield
    cfg._reset_brand_registry_for_tests()
    cfg.settings.cache_clear()


def _write_config(dir_: pathlib.Path, brand_id: str, **overrides) -> pathlib.Path:
    data = {
        "brand_id": brand_id,
        "display_name": f"Test {brand_id}",
        "timezone": "UTC",
        "content_source": "ai_generated",
        "orm_guardrails": {
            "hard_stop_phrases": [f"{brand_id}_stop"],
            "competitor_names": [],
            "min_confidence_threshold": 0.7,
        },
    }
    data.update(overrides)
    path = dir_ / f"{brand_id}.json"
    path.write_text(json.dumps(data))
    return path


class TestMultiBrandLoader:
    def test_discovers_multiple_brands(self, tmp_path, monkeypatch):
        configs = tmp_path / "configs"
        configs.mkdir()
        _write_config(configs, "example")
        _write_config(configs, "drive_brand", content_source="drive_footage")

        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "example")

        from meshpilot import config as cfg

        cfg.settings.cache_clear()  # pick up the new env
        cfg._reset_brand_registry_for_tests()

        ids = cfg.brand_ids()
        # brand_ids() returns alphabetically sorted
        assert ids == ["drive_brand", "example"]

        assert cfg.brand_config("drive_brand")["content_source"] == "drive_footage"
        assert cfg.brand_config("example")["content_source"] == "ai_generated"

        # Backward-compat: no-arg call returns the default brand's config.
        assert cfg.brand_config()["brand_id"] == "example"

    def test_filename_stem_must_match_brand_id(self, tmp_path, monkeypatch):
        configs = tmp_path / "configs"
        configs.mkdir()
        # Write a file whose internal brand_id disagrees with the filename.
        (configs / "drive_brand.json").write_text(
            json.dumps({
                "brand_id": "something_else",
                "display_name": "Mismatched",
                "timezone": "UTC",
            })
        )
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "something_else")

        from meshpilot import config as cfg

        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        with pytest.raises(RuntimeError, match="filename stem"):
            cfg.brand_ids()

    def test_missing_default_brand_fails(self, tmp_path, monkeypatch):
        configs = tmp_path / "configs"
        configs.mkdir()
        _write_config(configs, "drive_brand")

        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "example")

        from meshpilot import config as cfg

        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        with pytest.raises(RuntimeError, match="default_brand_id"):
            cfg.brand_ids()

    def test_unknown_brand_id_raises(self, tmp_path, monkeypatch):
        configs = tmp_path / "configs"
        configs.mkdir()
        _write_config(configs, "example")

        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "example")

        from meshpilot import config as cfg

        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        with pytest.raises(KeyError, match="unknown_brand"):
            cfg.brand_config("unknown_brand")

    def test_refuses_loudly_when_no_configs_present(self, tmp_path, monkeypatch):
        """Was `test_fallback_when_no_configs_present`: an empty configs dir used to fall back to
        a built-in default brand config — a real customer's, compiled into this file (site, mention
        terms, product line, Reddit handle, Discord ids). That fallback is GONE (see
        `config._load_brand_registry`): a product must not ship a customer's config, and it was
        never load-bearing anyway — every real brand arrived via BRAND_CONFIGS_JSON in production.
        Startup now refuses loudly instead of silently adopting whichever brand happened to be
        configured as default."""
        configs = tmp_path / "configs"
        configs.mkdir()

        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("BRAND_CONFIG_PATH", str(tmp_path / "nope.json"))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "example")

        from meshpilot import config as cfg

        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        with pytest.raises(RuntimeError, match="has no matching config"):
            cfg.brand_config()


# TestBrandScopedGuardrails removed in PRUNE-1 (ORM guardrails subsystem deleted).


class TestModelsCarryBrandId:
    """Every remaining brand-scoped SQLModel table must carry brand_id.

    The legacy pipeline's models (Signal -> ContentScript -> VideoJob -> ... -> MetricsSnapshot,
    plus ScoutCheckpoint) were removed with that pipeline; PlatformAuth is what remains.
    """

    def test_models_have_brand_id_field(self):
        from meshpilot.db.models import PlatformAuth

        assert "brand_id" in PlatformAuth.model_fields
