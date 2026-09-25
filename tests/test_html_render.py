"""GROW-SOCIAL-RENDER-2 — deterministic HTML card renderer.

Pure helpers need no env/DB; the render tests shell out to headless Chrome
(skipped if no Chrome on the box).
"""

import pytest

from meshpilot.media import html_render as hr


def test_every_brand_gets_the_same_neutral_theme():
    """Replaces `test_theme_is_brand_scoped_no_cross_tenant_bleed`, which asserted behaviour that
    no longer exists: one customer id was mapped to its own accent. That map was removed by
    operator decision 2026-09-23, so the old test is DELETED rather than hollowed out to pass.

    ⚠️ The old version also contained a vacuous assertion — `assert "indigo" or other.accent ==
    "#6366f1"` is a non-empty string `or` anything, which is always true. It would have passed
    for ANY accent. Worth recording: it sat in a test whose name promised tenant isolation.
    """
    a = hr.theme_for("some-other-client")
    b = hr.theme_for("a_third_client")
    assert a.accent == "#6366f1"            # the one neutral accent, asserted directly
    assert a.accent == b.accent             # no brand gets a look of its own any more


def test_the_theme_seam_still_takes_a_brand_id():
    """`theme_for` keeps its parameter on purpose — it is where config-driven theming lands if it
    is ever wanted, and callers already pass a brand_id. A signature change would touch them all."""
    import inspect
    assert list(inspect.signature(hr.theme_for).parameters) == ["brand_id"]


def test_build_card_html_includes_real_text_and_escapes():
    spec = hr.CardSpec(
        eyebrow="Rule Trap Autopsy",
        headline='Same $100k account. Different floor.',
        subhead="Static vs trailing drawdown",
        rows=[
            {"label": "Floor basis", "a": "static $90,000", "b": "trailing $95,000"},
        ],
        col_a="FTMO", col_b="FundingPips",
        footer="acmecorp.com",
    )
    out = hr.build_card_html(spec, hr.theme_for("any-brand"), 1600, 900)
    # exact strings present verbatim (the whole point — no garbling)
    assert "Same $100k account. Different floor." in out
    assert "static $90,000" in out and "trailing $95,000" in out
    assert "FundingPips" in out
    assert "#6366f1" in out                              # the theme's accent reaches the HTML
    # html-escaping active
    spec2 = hr.CardSpec(headline="A & B <script>x</script>")
    out2 = hr.build_card_html(spec2, hr.theme_for("x"), 1080, 1080)
    assert "<script>" not in out2 and "&amp;" in out2


def test_bullets_branch_renders_list():
    spec = hr.CardSpec(headline="Three traps", bullets=["daily cap", "trailing DD", "consistency"])
    out = hr.build_card_html(spec, hr.theme_for("x"), 1080, 1080)
    assert out.count("<li>") == 3


def _chrome_available() -> bool:
    try:
        hr._chrome_bin()
        return True
    except hr.HtmlRenderError:
        return False


@pytest.mark.skipif(not _chrome_available(), reason="no Chrome on this box")
def test_render_card_live_produces_png(tmp_path):
    spec = hr.CardSpec(
        eyebrow="True Cost Math",
        headline="The $497 fee is the smallest line item.",
        rows=[
            {"label": "Entry fee", "a": "$497", "b": ""},
            {"label": "Expected resets", "a": "6–9", "b": ""},
            {"label": "Total to funded", "a": "$2,982–$4,473", "b": ""},
        ],
        col_a="Apex $50k", col_b="",
        footer="acmecorp.com",
    )
    out = hr.render_card(spec, "any-brand", aspect="16:9", out_dir=tmp_path)
    assert out.exists() and out.stat().st_size > 0
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"   # PNG magic


def test_playwright_browsers_path_is_honoured(tmp_path, monkeypatch):
    """⚠️ Playwright's own Docker image installs browsers to /ms-playwright, NOT ~/.cache — so a
    lookup that searched only the home cache found nothing inside the one container built
    specifically to have a browser. The job submitter failed every pass with "no Chrome/Chromium
    binary found" while Chromium sat on disk (observed live 2026-09-16, Railway)."""
    from meshpilot.media.html_render import _playwright_chromium

    root = tmp_path / "ms-playwright"
    exe = root / "chromium_headless_shell-1200" / "chrome-headless-shell-linux64" / "chrome-headless-shell"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    assert _playwright_chromium() == str(exe)


def test_the_zero_sentinel_is_not_treated_as_a_path(tmp_path, monkeypatch):
    """`PLAYWRIGHT_BROWSERS_PATH=0` means "install beside the package", not a directory called 0."""
    from meshpilot.media.html_render import _playwright_chromium

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    assert _playwright_chromium() is None or "/0/" not in _playwright_chromium()
