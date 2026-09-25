"""Transparent hook/branding overlay for vertical clips (CLIPS lane).

Why Pillow and not ffmpeg's drawtext
------------------------------------
`drawtext` requires libfreetype at ffmpeg BUILD time and is routinely absent —
the Homebrew ffmpeg 9.0.2 used to develop this has no drawtext at all, and a
transform that depends on the host's build flags is not a transform. Pillow is
already a dependency, renders the same vendored Inter face as the post cards,
and can WRAP text, which drawtext cannot do at all.

Hook STYLES are an experiment, not a preference
-----------------------------------------------
Two presets ship, and they disagree with each other on purpose:

  cold  lowercase, no emoji, flat. Matches the brand voice doc ("volume drops
        when the pressure rises") and counter-positions against the category.
  hype  uppercase, emoji, loud. What essentially every PUBG short does.

The brand argument says `cold`; the observable evidence says `hype` works —
thousands of channels use it and get views. That disagreement is not resolvable
by argument, so both are implemented and the clip manifest records which style
each clip used, so performance can settle it.

Emoji
-----
Inter has no emoji glyphs, so an emoji in a hook renders as a "NO GLYPH" box —
worse than omitting it. Emoji runs are therefore drawn with the system colour
emoji font when one exists. ⚠️ Apple Color Emoji is macOS-only and a bitmap
face (160px strike only), so this works for local clip production and NOT in a
Linux deploy container. Where no emoji font exists the emoji are DROPPED rather
than drawn as boxes.
"""
from __future__ import annotations

import pathlib
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

_FONTS = pathlib.Path(__file__).resolve().parent / "fonts"
_BOLD = _FONTS / "Inter-700.ttf"
_REGULAR = _FONTS / "Inter-400.ttf"

# Bitmap colour face: only the 160px strike loads, everything else raises
# "invalid pixel size". Rendered at 160 and scaled to the run's cap height.
_EMOJI_FONT = pathlib.Path("/System/Library/Fonts/Apple Color Emoji.ttc")
_EMOJI_STRIKE = 160


STYLES: dict[str, dict] = {
    "cold": {
        "transform": str.lower,
        "fill": "#FFFFFF",
        "size": 74,
        "font": _BOLD,
    },
    "hype": {
        "transform": str.upper,
        # Yellow is the category convention and genuinely reads well on the
        # dark, desaturated backdrops PUBG produces.
        "fill": "#FFE01B",
        "size": 82,
        "font": _BOLD,
    },
}
DEFAULT_STYLE = "cold"


def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return (
        0x1F300 <= o <= 0x1FAFF      # pictographs, emoticons, symbols
        or 0x2600 <= o <= 0x27BF     # misc symbols + dingbats
        or o in (0x200D, 0xFE0F)     # ZWJ / variation selector ride along
    )


def _runs(text: str) -> list[tuple[bool, str]]:
    """Split into (is_emoji, chunk) runs so each is drawn with the right face."""
    out: list[tuple[bool, str]] = []
    for ch in text:
        emo = _is_emoji(ch)
        if out and out[-1][0] == emo:
            out[-1] = (emo, out[-1][1] + ch)
        else:
            out.append((emo, ch))
    return out


def _emoji_available() -> bool:
    return _EMOJI_FONT.exists()


def _strip_emoji(text: str) -> str:
    return "".join(c for c in text if not _is_emoji(c)).strip()


def _measure(draw: ImageDraw.ImageDraw, text: str,
             font: ImageFont.FreeTypeFont) -> float:
    """Width of a mixed run. Emoji are square at roughly the font's size."""
    w = 0.0
    for emo, chunk in _runs(text):
        if emo:
            w += sum(font.size * 1.12 for c in chunk if c not in ("‍", "️"))
        else:
            w += draw.textlength(chunk, font=font)
    return w


def _draw_mixed(img: Image.Image, draw: ImageDraw.ImageDraw, xy: tuple[float, float],
                text: str, font: ImageFont.FreeTypeFont, fill: str,
                stroke: int) -> None:
    """Draw text where emoji runs use the colour emoji face."""
    x, y = xy
    for emo, chunk in _runs(text):
        if not emo:
            draw.text((x, y), chunk, font=font, fill=fill,
                      stroke_width=stroke, stroke_fill=(0, 0, 0, 220))
            x += draw.textlength(chunk, font=font)
            continue
        ef = ImageFont.truetype(str(_EMOJI_FONT), _EMOJI_STRIKE)
        for ch in chunk:
            if ch in ("‍", "️"):
                continue
            side = _EMOJI_STRIKE + 40
            tile = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            ImageDraw.Draw(tile).text((0, 0), ch, font=ef, embedded_color=True)
            box = tile.getbbox()
            if box:
                target = int(font.size * 1.05)
                tile = tile.crop(box).resize((target, target), Image.LANCZOS)
                img.alpha_composite(tile, (int(x), int(y + font.size * 0.1)))
            x += font.size * 1.12


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
          max_w: int) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if _measure(draw, trial, font) <= max_w or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _fit(draw: ImageDraw.ImageDraw, text: str, max_w: int, max_lines: int,
         start: int, floor: int, font_path: pathlib.Path):
    """Shrink until the hook fits `max_lines`.

    Hooks are written per clip and vary in length. Fixed type would wrap a long
    hook into the gameplay strip or leave a short one looking lost, so the type
    adapts to the copy rather than the copy to the type.
    """
    size = start
    while size > floor:
        font = ImageFont.truetype(str(font_path), size)
        lines = _wrap(draw, text, font, max_w)
        if len(lines) <= max_lines:
            return font, lines
        size -= 4
    font = ImageFont.truetype(str(font_path), floor)
    return font, _wrap(draw, text, font, max_w)


def render_clip_overlay(
    headline: str = "",
    footer: str = "",
    width: int = 1080,
    height: int = 1920,
    style: str = DEFAULT_STYLE,
    footer_size: int = 34,
) -> bytes:
    """Render hook + branding as a transparent RGBA PNG.

    Placement is from the format, not taste:
      - the hook sits in the UPPER band, because the bottom ~10% is covered by
        platform UI (captions, follow button, progress bar) on TikTok, Reels
        and Shorts alike;
      - white/yellow with a dark stroke, because gameplay backdrops are
        arbitrary colours and an outline is the only treatment that survives
        both a bright desert and a dark building.
    """
    spec = STYLES.get(style, STYLES[DEFAULT_STYLE])
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = int(width * 0.08)
    max_w = width - margin * 2

    if headline:
        text = spec["transform"](headline.strip())
        if not _emoji_available():
            # Boxes are worse than nothing.
            text = _strip_emoji(text)
        font, lines = _fit(draw, text, max_w, 3, spec["size"], 40, spec["font"])
        leading = int(font.size * 1.16)
        y = int(height * 0.17) - (len(lines) * leading) // 2
        stroke = max(3, font.size // 14)
        for ln in lines:
            w = _measure(draw, ln, font)
            _draw_mixed(img, draw, ((width - w) / 2, y), ln, font,
                        spec["fill"], stroke)
            y += leading

    if footer:
        ffont = ImageFont.truetype(str(_REGULAR), footer_size)
        w = draw.textlength(footer, font=ffont)
        draw.text(((width - w) / 2, int(height * 0.88)), footer, font=ffont,
                  fill=(255, 255, 255, 235), stroke_width=2,
                  stroke_fill=(0, 0, 0, 200))

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
