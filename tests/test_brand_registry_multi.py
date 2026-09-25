"""Two brands, one registry (2026-09-12). The multi-brand file path had never been exercised: the
default brand ran on the built-in default because `brand/configs/` did not exist."""
from __future__ import annotations

import json
import pathlib

import pytest

from meshpilot import config

CONFIGS = pathlib.Path(__file__).resolve().parents[1] / "brand" / "configs"


def _example_brand_config() -> dict:
    """The tracked, fictional reference brand (`brand/configs/example.json`), EXACTLY as shipped.

    There is no more built-in default to build a fixture from — the file itself is the fixture,
    which is the point: the thing the template hands a stranger is the thing the tests exercise.
    Nothing is stripped. It used to drop the `_`-prefixed documentation keys because the schema
    rejected them, which quietly meant the shipped file was never validated as shipped."""
    return json.loads((CONFIGS / "example.json").read_text())


PETCARE = {
    "brand_id": "petcare", "display_name": "PetCare", "env_prefix": "AP", "timezone": "America/New_York",
    "content_source": "ai_generated", "site_url": "https://thepetcare.com",
    "brand": {"name": "PetCare", "accent_color": "#2e7d32", "base_color": "#fbf8f1",
              "watermark_path": None, "voice": "warm, plain-spoken"},
    "video_model_routing": {"phase": 1, "model_map": {"cinematic": "kling_2", "realistic": "kling_2",
                                                      "text_in_video": "kling_2", "fast": "kling_2"}},
    "orm_guardrails": {"hard_stop_phrases": ["cure", "FDA approved"], "competitor_names": [],
                       "auto_respond_tiers": ["positive", "neutral_faq"],
                       "review_window_seconds": {"negative_mild": 3600, "neutral_technical": 3600},
                       "escalate_tiers": ["negative_severe", "legal_flag"]},
    "platforms": {}, "default_hashtags": ["petcare"], "voice_prompt_path": None,
    "seo": {"publisher": "shopify", "blog_handle": "news"},
}


@pytest.fixture
def two_brands(tmp_path, monkeypatch):
    """Both brands as FILES in a tmp dir.

    ⚠️ Not the shipped `brand/configs/` — that directory is gitignored, so it exists on the
    operator's Mac and NOT on a CI runner. The first version of these tests read the shipped files,
    passed locally, and failed in the PR gate — the same fact this lane documents, biting its own
    tests. Everything here is built from the tracked `example.json` and an inline PetCare."""
    monkeypatch.setenv("BRAND_CONFIGS_DIR", str(tmp_path))
    monkeypatch.setenv("DEFAULT_BRAND_ID", "example")
    monkeypatch.delenv("BRAND_CONFIGS_JSON", raising=False)
    config.settings.cache_clear()
    config._reset_brand_registry_for_tests()
    (tmp_path / "example.json").write_text(json.dumps(_example_brand_config()))
    (tmp_path / "petcare.json").write_text(json.dumps(PETCARE))
    config._reset_brand_registry_for_tests()
    yield tmp_path
    config.settings.cache_clear()
    config._reset_brand_registry_for_tests()


def test_both_brands_are_registered_and_the_default_still_resolves(two_brands):
    assert config.brand_ids() == ["example", "petcare"]
    assert config.brand_config()["brand_id"] == "example"


def test_adding_a_brand_without_the_default_would_crash_startup(tmp_path, monkeypatch):
    """The trap: `brand/configs/` present but missing the default brand raises at load — the API
    would not start. That is why a real second brand's file once had to land in the same change
    as the default's own (a real 2026-09-12 deploy, recorded here without the customer's name)."""
    (tmp_path / "petcare.json").write_text(json.dumps({"brand_id": "petcare", "env_prefix": "AP"}))
    monkeypatch.setenv("BRAND_CONFIGS_DIR", str(tmp_path))
    monkeypatch.delenv("BRAND_CONFIGS_JSON", raising=False)
    config.settings.cache_clear()
    with pytest.raises(RuntimeError, match="has no matching config"):
        config._load_brand_registry()


def test_petcare_is_isolated_and_inert(two_brands):
    """Its own prefix, and no credentials — so nothing can post for it until the operator arms it."""
    assert config.brand_env_prefix("petcare") == "AP"
    assert config.brand_env("META_APP_ID", "petcare") == ""
    assert config.brand_env("BUFFER_API_KEY", "petcare") == ""


def test_the_petcare_config_validates_against_the_schema():
    """The schema was extended for `site_url` and `seo`; the config shape used in prod must pass it.

    The example brand is validated UNMODIFIED — doc keys, media_pipeline and all. An
    earlier pass had to `pop("media_pipeline")` to get here, which looked like a fixture
    quirk and was really a stale schema: it allowed only the bare string `"strip_audio"`
    while the product had gained `replace_audio` and `vertical_reframe`, and it forbade
    the `_`-prefixed comment keys that the template's own reference config teaches with.
    Every brand using those transforms was failing validation, not just this one.
    """
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.load(open(CONFIGS.parent / "schema" / "brand.config.schema.json"))
    jsonschema.validate(PETCARE, schema)
    jsonschema.validate(_example_brand_config(), schema)


def test_the_schema_enum_still_matches_the_transform_registry():
    """The guard for how that drifted. The schema hardcodes transform names; ffmpeg.py owns
    them. Nothing connected the two, so adding a transform left the schema quietly rejecting
    configs that the product would happily run."""
    from meshpilot.media.ffmpeg import registered_transforms

    schema = json.load(open(CONFIGS.parent / "schema" / "brand.config.schema.json"))
    step = schema["properties"]["media_pipeline"]["additionalProperties"]["items"]["oneOf"]
    bare = set(step[0]["enum"])
    parameterised = set(step[1]["properties"]["name"]["enum"])
    assert bare == parameterised == set(registered_transforms())


def test_petcare_declares_it_cannot_use_the_git_publisher(two_brands):
    """Shopify store — the repo-based SEO publisher does not apply. Recorded in the config so the
    SEO lane refuses rather than tries."""
    ap = config.brand_config("petcare")
    assert ap["seo"]["publisher"] == "shopify"
    assert ap["site_url"].startswith("https://thepetcare.com")


# ── the path that reaches production ──
def test_configs_arrive_from_the_cloud_env(tmp_path, monkeypatch):
    """`brand/configs/*.json` is gitignored and the runtime is FastAPI Cloud, so files never reach
    prod. BRAND_CONFIGS_JSON is the path that does."""
    monkeypatch.setenv("BRAND_CONFIGS_DIR", str(tmp_path))       # empty: no files, like prod
    monkeypatch.setenv("BRAND_CONFIGS_JSON", json.dumps({
        "example": {"env_prefix": "ACME", "display_name": "Acme"},
        "petcare": {"env_prefix": "AP", "display_name": "PetCare"},
    }))
    config.settings.cache_clear()
    config._reset_brand_registry_for_tests()
    assert config.brand_ids() == ["example", "petcare"]
    assert config.brand_config("petcare")["brand_id"] == "petcare"   # stamped from the key
    assert config.brand_env_prefix("petcare") == "AP"


def test_env_wins_over_a_file_for_the_same_brand(tmp_path, monkeypatch):
    """Env is the source of truth for brand-scoped values — a stale local file must not shadow it."""
    (tmp_path / "example.json").write_text(json.dumps({"env_prefix": "ACME", "display_name": "from-file"}))
    monkeypatch.setenv("BRAND_CONFIGS_DIR", str(tmp_path))
    monkeypatch.setenv("BRAND_CONFIGS_JSON", json.dumps({"example": {"env_prefix": "ACME", "display_name": "from-env"}}))
    config.settings.cache_clear()
    config._reset_brand_registry_for_tests()
    assert config.brand_config("example")["display_name"] == "from-env"


def test_malformed_env_configs_fail_loudly_at_boot(tmp_path, monkeypatch):
    """A typo in the cloud env must stop the app, not silently drop a brand."""
    monkeypatch.setenv("BRAND_CONFIGS_DIR", str(tmp_path))
    monkeypatch.setenv("BRAND_CONFIGS_JSON", "{not json")
    config.settings.cache_clear()
    with pytest.raises(RuntimeError, match="not valid JSON"):
        config._load_brand_registry()


def test_a_key_that_disagrees_with_its_brand_id_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAND_CONFIGS_DIR", str(tmp_path))
    monkeypatch.setenv("BRAND_CONFIGS_JSON", json.dumps({"petcare": {"brand_id": "other", "env_prefix": "AP"}}))
    config.settings.cache_clear()
    with pytest.raises(RuntimeError, match="disagrees"):
        config._load_brand_registry()
