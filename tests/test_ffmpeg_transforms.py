"""Pre-publish ffmpeg transform pipeline — registry, routing, caching, errors."""
from __future__ import annotations

import json
import os
import pathlib

import pytest

os.environ.setdefault("DISPATCH_MODE", "dry_run")
os.environ.setdefault("SIGNAL_DB_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN_SIGNAL", "0:test")
os.environ.setdefault("TELEGRAM_ADMIN_IDS", "0")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("GOOGLE_API_KEY", "test")
os.environ.setdefault("AUTH_ENCRYPTION_KEY", "l3mgT3MDKZ2g8oh2l8r4e1XaS0o7Q8mT9H5V1v3P2Hk=")


@pytest.fixture(autouse=True)
def _reset_caches():
    from meshpilot import config as cfg
    cfg._reset_brand_registry_for_tests()
    cfg.settings.cache_clear()
    yield
    cfg._reset_brand_registry_for_tests()
    cfg.settings.cache_clear()


def _write_brand(configs_dir: pathlib.Path, brand_id: str, media_pipeline: dict | None) -> None:
    cfg = {
        "brand_id": brand_id,
        "display_name": brand_id,
        "timezone": "UTC",
        "platforms": {},
    }
    if media_pipeline is not None:
        cfg["media_pipeline"] = media_pipeline
    (configs_dir / f"{brand_id}.json").write_text(json.dumps(cfg))


class TestCanonicalPlatform:
    def test_buffer_prefix_stripped(self):
        from meshpilot.media.ffmpeg import canonical_platform
        assert canonical_platform("buffer_tiktok") == "tiktok"
        assert canonical_platform("meta_instagram") == "instagram"

    def test_zernio_prefix_stripped(self):
        from meshpilot.media.ffmpeg import canonical_platform
        assert canonical_platform("zernio_tiktok") == "tiktok"

    def test_direct_keys_stable(self):
        from meshpilot.media.ffmpeg import canonical_platform
        assert canonical_platform("tiktok") == "tiktok"
        assert canonical_platform("youtube_shorts") == "youtube"
        assert canonical_platform("instagram_reels") == "instagram"


class TestApplyTransformsNoOp:
    """No brand config, no media_pipeline, or empty list → return input unchanged."""

    @pytest.mark.asyncio
    async def test_unknown_brand_returns_input(self, tmp_path, monkeypatch):
        from meshpilot.media.ffmpeg import apply_transforms
        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"x")
        out = await apply_transforms(str(vid), "does_not_exist", "buffer_tiktok")
        assert out == str(vid)

    @pytest.mark.asyncio
    async def test_no_media_pipeline_returns_input(self, tmp_path, monkeypatch):
        from meshpilot import config as cfg
        from meshpilot.media.ffmpeg import apply_transforms

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "brand_a", media_pipeline=None)
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "brand_a")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"x")
        out = await apply_transforms(str(vid), "brand_a", "buffer_tiktok")
        assert out == str(vid)

    @pytest.mark.asyncio
    async def test_platform_not_in_pipeline_returns_input(self, tmp_path, monkeypatch):
        from meshpilot import config as cfg
        from meshpilot.media.ffmpeg import apply_transforms

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "brand_b", media_pipeline={"instagram": ["strip_audio"]})
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "brand_b")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"x")
        # Brand wants strip_audio only for instagram; TikTok publish leaves input alone.
        out = await apply_transforms(str(vid), "brand_b", "buffer_tiktok")
        assert out == str(vid)

    @pytest.mark.asyncio
    async def test_empty_brand_id_returns_input(self, tmp_path):
        from meshpilot.media.ffmpeg import apply_transforms
        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"x")
        out = await apply_transforms(str(vid), "", "buffer_tiktok")
        assert out == str(vid)


class TestStripAudioBuilder:
    """The argv builder is pure — verify the shape without invoking ffmpeg."""

    def test_strip_audio_argv(self, tmp_path):
        from meshpilot.media.ffmpeg import _strip_audio
        src = tmp_path / "in.mp4"
        dst = tmp_path / "in.strip_audio.mp4"
        argv = _strip_audio(src, dst, {})
        # Keep the video track untouched, drop audio, write to dst.
        assert str(src) in argv
        assert str(dst) in argv
        assert "-an" in argv
        assert "-c:v" in argv
        assert "copy" in argv
        # No re-encoding of video → no codec like libx264.
        assert "libx264" not in argv


class TestReplaceAudioBuilder:
    """Verify the replace_audio argv without invoking ffmpeg."""

    def test_replace_audio_argv(self, tmp_path):
        from meshpilot.media.ffmpeg import _replace_audio
        src = tmp_path / "in.mp4"
        dst = tmp_path / "in.replace_audio.mp4"
        audio = tmp_path / "bgm.mp3"
        audio.write_bytes(b"fake audio")
        argv = _replace_audio(src, dst, {"audio_path": str(audio)})
        # Two inputs: video + audio (looped).
        assert argv.count("-i") == 2
        assert str(src) in argv
        assert str(audio) in argv
        assert str(dst) in argv
        # Audio is looped so short tracks still cover the video.
        assert "-stream_loop" in argv
        # Video is copied, audio is re-encoded to AAC.
        assert "copy" in argv
        assert "aac" in argv
        # Explicit stream mapping drops the original audio track.
        assert "-map" in argv
        assert "0:v:0" in argv
        assert "1:a:0" in argv
        # Trim to whichever stream ends first (usually the video).
        assert "-shortest" in argv

    def test_replace_audio_requires_audio_path(self, tmp_path):
        from meshpilot.media.ffmpeg import _replace_audio
        src = tmp_path / "in.mp4"
        dst = tmp_path / "in.replace_audio.mp4"
        with pytest.raises(ValueError, match="audio_path"):
            _replace_audio(src, dst, {})

    def test_replace_audio_missing_file_raises(self, tmp_path):
        from meshpilot.media.ffmpeg import _replace_audio
        src = tmp_path / "in.mp4"
        dst = tmp_path / "in.replace_audio.mp4"
        with pytest.raises(FileNotFoundError):
            _replace_audio(src, dst, {"audio_path": str(tmp_path / "nope.mp3")})

    def test_replace_audio_custom_bitrate(self, tmp_path):
        from meshpilot.media.ffmpeg import _replace_audio
        src = tmp_path / "in.mp4"
        dst = tmp_path / "in.replace_audio.mp4"
        audio = tmp_path / "bgm.mp3"
        audio.write_bytes(b"x")
        argv = _replace_audio(src, dst, {"audio_path": str(audio), "bitrate": "192k"})
        assert "192k" in argv


class TestParseEntry:
    def test_string_entry(self):
        from meshpilot.media.ffmpeg import _parse_entry
        assert _parse_entry("strip_audio") == ("strip_audio", {})

    def test_dict_entry_splits_name_from_options(self):
        from meshpilot.media.ffmpeg import _parse_entry
        name, opts = _parse_entry({"name": "replace_audio", "audio_path": "x.mp3"})
        assert name == "replace_audio"
        assert opts == {"audio_path": "x.mp3"}

    def test_dict_entry_missing_name(self):
        from meshpilot.media.ffmpeg import _parse_entry
        with pytest.raises(ValueError, match="name"):
            _parse_entry({"audio_path": "x.mp3"})

    def test_bad_entry_type(self):
        from meshpilot.media.ffmpeg import _parse_entry
        with pytest.raises(ValueError):
            _parse_entry(123)


class TestApplyTransformsReplaceAudio:
    @pytest.mark.asyncio
    async def test_dict_entry_passes_options_through(self, tmp_path, monkeypatch):
        from meshpilot import config as cfg
        from meshpilot.media import ffmpeg as mod

        configs = tmp_path / "configs"
        configs.mkdir()
        audio = tmp_path / "bgm.mp3"
        audio.write_bytes(b"bgm bytes")
        _write_brand(
            configs,
            "brand_ra",
            media_pipeline={
                "tiktok": [
                    {"name": "replace_audio", "audio_path": str(audio)},
                ]
            },
        )
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "brand_ra")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"input")

        calls: list[list[str]] = []

        async def fake_run(argv):
            calls.append(argv)
            pathlib.Path(argv[-1]).write_bytes(b"o")
        monkeypatch.setattr(mod, "_run_ffmpeg", fake_run)

        out = await mod.apply_transforms(str(vid), "brand_ra", "buffer_tiktok")
        assert out.endswith(".replace_audio.mp4")
        assert len(calls) == 1
        assert str(audio) in calls[0]
        assert "aac" in calls[0]


class TestApplyTransformsRuns:
    @pytest.mark.asyncio
    async def test_invokes_ffmpeg_and_returns_output_path(self, tmp_path, monkeypatch):
        from meshpilot import config as cfg
        from meshpilot.media import ffmpeg as mod

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "brand_c", media_pipeline={"tiktok": ["strip_audio"]})
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "brand_c")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"input bytes")

        ffmpeg_calls: list[list[str]] = []

        async def fake_run(argv):
            ffmpeg_calls.append(argv)
            # Simulate ffmpeg writing the output file.
            out = pathlib.Path(argv[-1])
            out.write_bytes(b"stripped")
        monkeypatch.setattr(mod, "_run_ffmpeg", fake_run)

        result = await mod.apply_transforms(str(vid), "brand_c", "buffer_tiktok")
        expected_out = tmp_path / "clip.strip_audio.mp4"
        assert result == str(expected_out)
        assert expected_out.exists()
        assert len(ffmpeg_calls) == 1
        # _run_ffmpeg receives the argv tail (binary name is prepended
        # inside). Verify the key flags are set on the transform.
        assert "-an" in ffmpeg_calls[0]
        assert "-c:v" in ffmpeg_calls[0]

    @pytest.mark.asyncio
    async def test_cache_hit_skips_ffmpeg(self, tmp_path, monkeypatch):
        """Second call with the same input file must not invoke ffmpeg again."""
        from meshpilot import config as cfg
        from meshpilot.media import ffmpeg as mod

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "brand_d", media_pipeline={"tiktok": ["strip_audio"]})
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "brand_d")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"x")
        # Pre-create the expected output so the cache hit path triggers.
        cached = tmp_path / "clip.strip_audio.mp4"
        cached.write_bytes(b"already there")

        async def must_not_run(argv):
            raise AssertionError("_run_ffmpeg should not be called on cache hit")
        monkeypatch.setattr(mod, "_run_ffmpeg", must_not_run)

        out = await mod.apply_transforms(str(vid), "brand_d", "buffer_tiktok")
        assert out == str(cached)

    @pytest.mark.asyncio
    async def test_missing_input_file_raises(self, tmp_path, monkeypatch):
        from meshpilot import config as cfg
        from meshpilot.media import ffmpeg as mod

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "brand_e", media_pipeline={"tiktok": ["strip_audio"]})
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "brand_e")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        with pytest.raises(FileNotFoundError):
            await mod.apply_transforms(
                str(tmp_path / "missing.mp4"), "brand_e", "buffer_tiktok"
            )

    @pytest.mark.asyncio
    async def test_unknown_transform_name_raises(self, tmp_path, monkeypatch):
        from meshpilot import config as cfg
        from meshpilot.media import ffmpeg as mod

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "brand_f", media_pipeline={"tiktok": ["bogus_transform"]})
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "brand_f")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"x")

        with pytest.raises(ValueError, match="unknown transform"):
            await mod.apply_transforms(str(vid), "brand_f", "buffer_tiktok")


class TestCanonicalRouting:
    """All three publisher key families route to the same canonical platform,
    so `tiktok` config applies whether the brand posts via buffer_tiktok,
    zernio_tiktok, or direct tiktok."""

    @pytest.mark.asyncio
    async def test_zernio_tiktok_hits_tiktok_pipeline(self, tmp_path, monkeypatch):
        from meshpilot import config as cfg
        from meshpilot.media import ffmpeg as mod

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "brand_z", media_pipeline={"tiktok": ["strip_audio"]})
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "brand_z")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"x")

        ran: list[list[str]] = []
        async def fake_run(argv):
            ran.append(argv)
            pathlib.Path(argv[-1]).write_bytes(b"o")
        monkeypatch.setattr(mod, "_run_ffmpeg", fake_run)

        out = await mod.apply_transforms(str(vid), "brand_z", "zernio_tiktok")
        assert out.endswith(".strip_audio.mp4")
        assert len(ran) == 1

    @pytest.mark.asyncio
    async def test_direct_tiktok_hits_tiktok_pipeline(self, tmp_path, monkeypatch):
        from meshpilot import config as cfg
        from meshpilot.media import ffmpeg as mod

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "brand_t", media_pipeline={"tiktok": ["strip_audio"]})
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "brand_t")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"x")

        ran: list[list[str]] = []
        async def fake_run(argv):
            ran.append(argv)
            pathlib.Path(argv[-1]).write_bytes(b"o")
        monkeypatch.setattr(mod, "_run_ffmpeg", fake_run)

        out = await mod.apply_transforms(str(vid), "brand_t", "tiktok")
        assert len(ran) == 1
        assert out.endswith(".strip_audio.mp4")


class TestFfmpegErrorPropagation:
    @pytest.mark.asyncio
    async def test_nonzero_exit_raises_runtime_error(self, tmp_path, monkeypatch):
        """_run_ffmpeg must surface ffmpeg stderr when the binary exits nonzero."""
        import subprocess

        from meshpilot.media import ffmpeg as mod

        class _FakeResult:
            returncode = 1
            stderr = "[error] input #0 does not contain any stream"
            stdout = ""

        def fake_subprocess(*args, **kwargs):
            return _FakeResult()

        monkeypatch.setattr(subprocess, "run", fake_subprocess)

        with pytest.raises(RuntimeError, match="ffmpeg failed"):
            await mod._run_ffmpeg(["-i", "/dev/null", "/tmp/out.mp4"])


class TestVerticalReframeBuilder:
    """The 9:16 reframe used for stream clips (CLIPS lane).

    Builder is pure, so the filter graph is asserted without invoking ffmpeg —
    same contract as every other transform here.
    """

    def _argv(self, options=None):
        from meshpilot.media.ffmpeg import _vertical_reframe
        return _vertical_reframe(pathlib.Path("in.mp4"), pathlib.Path("out.mp4"),
                                 options or {})

    def _graph(self, options=None):
        argv = self._argv(options)
        return argv[argv.index("-filter_complex") + 1]

    def test_output_is_1080x1920_by_default(self):
        g = self._graph()
        assert "scale=1080:1920" in g
        assert "crop=1080:1920" in g

    def test_footage_is_full_width_not_centre_cropped(self):
        # The whole point: a centre-crop would keep ~56% of the width, which in
        # a shooter is where opponents are. The strip scales to full width and
        # lets ffmpeg derive the height (-2 keeps it even for yuv420p).
        assert "[fg]scale=1080:-2[fgs]" in self._graph()

    def test_strip_is_centred_over_the_blurred_fill(self):
        assert "overlay=(W-w)/2:(H-h)/2" in self._graph()

    def test_crop_is_applied_before_reframing(self):
        # Northwind's capture letterboxes a phone screen into 1080p; those bars
        # must be removed BEFORE the split, or they ride into the strip.
        g = self._graph({"crop": "1920:884:0:98"})
        assert g.index("crop=1920:884:0:98") < g.index("split=2")

    def test_no_crop_by_default(self):
        assert "split=2" in self._graph()
        assert self._graph().startswith("[0:v]split=2")

    def test_audio_is_copied_not_reencoded(self):
        # A second AAC generation is audible on voice.
        argv = self._argv()
        assert "-c:a" in argv and argv[argv.index("-c:a") + 1] == "copy"

    def test_audio_is_optional_so_silent_clips_do_not_fail(self):
        assert "0:a?" in self._argv()

    def test_blur_and_dim_are_tunable(self):
        g = self._graph({"blur": 8, "dim": -0.3})
        assert "gblur=sigma=8" in g and "eq=brightness=-0.3" in g

    def test_pixel_format_is_set_for_player_compatibility(self):
        argv = self._argv()
        assert argv[argv.index("-pix_fmt") + 1] == "yuv420p"


class TestVerticalReframeOverlay:
    """Hook/branding is composited from a PNG, not drawn with drawtext.

    drawtext needs libfreetype at ffmpeg BUILD time and is routinely missing —
    the ffmpeg this was developed against has no drawtext at all. These tests
    pin the PNG-overlay contract so nobody "simplifies" it back.
    """

    def _argv(self, options):
        from meshpilot.media.ffmpeg import _vertical_reframe
        return _vertical_reframe(pathlib.Path("in.mp4"), pathlib.Path("out.mp4"), options)

    def test_no_overlay_input_when_none_requested(self):
        argv = self._argv({})
        assert argv.count("-i") == 1
        assert argv[argv.index("-map") + 1] == "[v]"

    def test_overlay_is_a_second_input_composited_at_origin(self):
        argv = self._argv({"overlay": "ov.png"})
        assert argv.count("-i") == 2
        graph = argv[argv.index("-filter_complex") + 1]
        # full-frame PNG, so it lands at 0:0 rather than being positioned
        assert "[v][1:v]overlay=0:0" in graph

    def test_overlay_output_is_converted_to_yuv420p_inside_the_graph(self):
        # Overlaying RGBA leaves the chain in an alpha format and libx264 then
        # refuses to open at all (-22) instead of converting. -pix_fmt alone
        # does NOT rescue it.
        graph = self._argv({"overlay": "ov.png"})[
            self._argv({"overlay": "ov.png"}).index("-filter_complex") + 1]
        assert "overlay=0:0,format=yuv420p" in graph

    def test_map_follows_the_overlay_label(self):
        argv = self._argv({"overlay": "ov.png"})
        assert argv[argv.index("-map") + 1] == "[vo]"

    def test_never_uses_drawtext(self):
        for opts in ({}, {"overlay": "ov.png"}, {"crop": "1920:884:0:98"}):
            assert "drawtext" not in " ".join(self._argv(opts))


class TestClipOverlayRender:
    def test_renders_a_transparent_png_of_the_requested_size(self):
        from io import BytesIO

        from PIL import Image

        from meshpilot.media.render.clip_overlay import render_clip_overlay
        img = Image.open(BytesIO(render_clip_overlay("hook", "@brand")))
        assert img.size == (1080, 1920)
        assert img.mode == "RGBA"
        assert img.getpixel((5, 5))[3] == 0      # corner stays transparent

    def test_empty_inputs_render_a_fully_transparent_frame(self):
        from io import BytesIO

        from PIL import Image

        from meshpilot.media.render.clip_overlay import render_clip_overlay
        img = Image.open(BytesIO(render_clip_overlay("", "")))
        assert img.getbbox() is None              # nothing drawn at all

    def test_apostrophes_and_colons_need_no_escaping(self):
        # The whole reason for leaving drawtext: its quoting rules mangle these.
        from meshpilot.media.render.clip_overlay import render_clip_overlay
        assert render_clip_overlay("he's reloading: now", "@x")

    def test_a_long_hook_shrinks_instead_of_overflowing(self):
        from io import BytesIO

        from PIL import Image

        from meshpilot.media.render.clip_overlay import render_clip_overlay
        long_hook = "this is a deliberately long hook that must wrap and shrink"
        img = Image.open(BytesIO(render_clip_overlay(long_hook, "")))
        box = img.getbbox()
        # must stay clear of the gameplay strip, which starts around 0.37*h
        assert box is not None and box[3] < int(1920 * 0.37)


class TestClipOverlayStyles:
    """Two hook treatments that disagree on purpose — see clip_overlay docstring.

    The brand voice argues for `cold`; the observable category evidence argues
    for `hype`. Both ship so performance can settle it instead of an argument.
    """

    def _img(self, **kw):
        from io import BytesIO

        from PIL import Image

        from meshpilot.media.render.clip_overlay import render_clip_overlay
        return Image.open(BytesIO(render_clip_overlay(**kw)))

    def test_both_styles_exist(self):
        from meshpilot.media.render.clip_overlay import STYLES
        assert {"cold", "hype"} <= set(STYLES)

    def test_cold_is_lowercase_and_hype_is_uppercase(self):
        from meshpilot.media.render.clip_overlay import STYLES
        assert STYLES["cold"]["transform"]("Ego") == "ego"
        assert STYLES["hype"]["transform"]("Ego") == "EGO"

    def test_styles_render_visibly_differently(self):
        a = self._img(headline="one four clutch", footer="", style="cold")
        b = self._img(headline="one four clutch", footer="", style="hype")
        assert list(a.getdata()) != list(b.getdata())

    def test_unknown_style_falls_back_rather_than_raising(self):
        # A typo in a batch of 20 clips should not lose the batch.
        assert self._img(headline="x", footer="", style="nonsense").getbbox()

    def test_emoji_are_dropped_not_boxed_when_no_emoji_font(self, monkeypatch):
        # Inter has no emoji glyphs: drawing them with it yields "NO GLYPH"
        # boxes, which look broken. Absent a colour font, omit them instead.
        from meshpilot.media.render import clip_overlay
        monkeypatch.setattr(clip_overlay, "_emoji_available", lambda: False)
        only_emoji = clip_overlay.render_clip_overlay(headline="🔥😱", footer="")
        from io import BytesIO

        from PIL import Image
        assert Image.open(BytesIO(only_emoji)).getbbox() is None

    def test_emoji_detection_covers_the_common_ranges(self):
        from meshpilot.media.render.clip_overlay import _is_emoji
        assert _is_emoji("🔥") and _is_emoji("😱") and _is_emoji("⚡")
        assert not _is_emoji("a") and not _is_emoji("4")
