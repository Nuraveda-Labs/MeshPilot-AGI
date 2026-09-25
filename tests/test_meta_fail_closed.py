"""Meta publishing must never borrow another brand's credentials.

Measured 2026-09-24: prod held UNPREFIXED META_PAGE_ID / META_IG_USER_ID / SYSTEM_USER_TOKEN
pointing at a Page that belongs to no onboarded brand, and both Meta resolvers used
`brand_env_or_default`. A brand missing one of its own keys would therefore have published to
that other Page, silently. Auto mode posts unattended, so this must fail closed.

`brand_env_or_default` itself is NOT removed: Drive depends on its fallback on purpose
(tests/test_meta_default.py covers the helper).
"""
import pytest

from meshpilot.platforms.facebook import resolve_facebook_creds
from meshpilot.platforms.instagram import resolve_instagram_creds

_GLOBALS = {"META_PAGE_ID": "global_page", "META_IG_USER_ID": "global_ig",
            "SYSTEM_USER_TOKEN": "global_token"}
_OWN = ("ACME_META_PAGE_ID", "ACME_META_IG_USER_ID", "ACME_SYSTEM_USER_TOKEN")


@pytest.fixture
def globals_only(monkeypatch):
    for k, v in _GLOBALS.items():
        monkeypatch.setenv(k, v)
    for k in _OWN:
        monkeypatch.delenv(k, raising=False)


def test_facebook_refuses_the_global_page(globals_only):
    with pytest.raises(RuntimeError, match="META_PAGE_ID"):
        resolve_facebook_creds("example")


def test_facebook_refuses_the_global_token(globals_only, monkeypatch):
    monkeypatch.setenv("ACME_META_PAGE_ID", "own_page")
    with pytest.raises(RuntimeError, match="SYSTEM_USER_TOKEN"):
        resolve_facebook_creds("example")


def test_instagram_refuses_the_global_ig_account(globals_only):
    with pytest.raises(RuntimeError, match="META_IG_USER_ID"):
        resolve_instagram_creds("example")


def test_instagram_refuses_a_mix_of_own_and_global(globals_only, monkeypatch):
    monkeypatch.setenv("ACME_META_IG_USER_ID", "own_ig")
    monkeypatch.setenv("ACME_META_PAGE_ID", "own_page")
    with pytest.raises(RuntimeError, match="SYSTEM_USER_TOKEN"):
        resolve_instagram_creds("example")


def test_a_fully_configured_brand_uses_only_its_own_keys(globals_only, monkeypatch):
    monkeypatch.setenv("ACME_META_IG_USER_ID", "own_ig")
    monkeypatch.setenv("ACME_META_PAGE_ID", "own_page")
    monkeypatch.setenv("ACME_SYSTEM_USER_TOKEN", "own_token")
    assert resolve_instagram_creds("example") == ("own_ig", "own_page", "own_token")
    assert resolve_facebook_creds("example") == ("own_page", "own_token")
