"""Find the good thirty seconds WITHOUT an LLM, by reading what the audience already said.

The idea
--------
`docs/plans/2026-09-20-clips-to-social.md` § 2 settled that selection is human, because
"kill-feed OCR, audio-peak detection and chat-spike analysis are all fragile and all
produce clips nobody wants". This is a different signal from all three: when hundreds of
viewers type "2:34 😂" in the comments, they are not being inferred about — they are
telling you directly which moment landed.

That makes it DETERMINISTIC and free: no transcription, no model, no tokens. It is also
the only signal here with a human in the loop by construction, which is exactly the
property the § 2 decision was protecting.

⚠️ It does NOT replace the replay buffer for your own streams. It needs an audience that
has already watched and commented, so it can only work on content that is already public
and already has traction. A stream that went out last night has no signal at all.

⚠️ YouTube's own "most replayed" heatmap would be the better source, and is deliberately
NOT used: measured 2026-09-24 on a 61.8M-view video, yt-dlp returned zero heatmap points.
It is not reliably extractable, so this reads comments instead.

⚠️⚠️ MEASURED RESULT, 2026-09-24 — READ THIS BEFORE BUILDING ON IT.
Run against a 2h40m podcast, 800 top-sorted comments:

    comments with any timestamp   18  (2.2%)
      index/chapter comments       3
      genuine reaction stamps     15
    moments reaching 2 mentions    0   (after excluding index comments)

FIFTEEN reaction stamps scattered over 160 minutes do not converge on anything. The
ranking it produced without that exclusion was carried entirely by ONE chapter-index
comment pairing with stray singles — it looked like signal and was an artifact.

So this is an instrument, not an oracle. It is worth running on content where the
audience demonstrably timestamps (music, compilations, fails, "skip to 3:20" reply
culture), and it is NOT a basis for an automated clip channel. That matches what
docs/plans/2026-09-20-clips-to-social.md § 2 already concluded from the other direction.

What would have to be true for it to work: thousands of comments (not 800), on content
whose audience reacts with times rather than indexes it with chapters.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# h:mm:ss or m:ss, not preceded/followed by more digits or colons — so a "16:9" aspect
# ratio inside a sentence does not read as 16 minutes 9 seconds when a third group is
# absent, and a "1:2:3:4" list never parses.
_TS = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)(?![\d:])")

# A comment carrying this many timestamps is a CHAPTER LIST, not a reaction. Those are
# written by one person indexing the whole video and would otherwise dominate the
# histogram with evenly-spaced marks that point at nothing interesting.
CHAPTER_LIST_AT = 4


@dataclass(slots=True)
class Moment:
    at_s: int
    weight: float
    mentions: int
    samples: list[str] = field(default_factory=list)

    @property
    def stamp(self) -> str:
        h, rem = divmod(self.at_s, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_timestamps(text: str) -> list[int]:
    """Every timestamp in a comment, as seconds."""
    out = []
    for h, m, s in _TS.findall(text or ""):
        out.append(int(h or 0) * 3600 + int(m) * 60 + int(s))
    return out


def _weight(like_count: int | None, n_in_comment: int) -> float:
    """One mention is one vote; a well-liked comment is many people agreeing with it.

    log1p rather than raw likes: a 5,000-like comment is stronger evidence than a
    50-like one, but not a hundred times stronger, and linear weighting lets a single
    viral comment bury everything else.
    """
    w = 1.0 + math.log1p(max(0, like_count or 0))
    if n_in_comment >= CHAPTER_LIST_AT:
        w *= 0.15          # keep it, discount it — occasionally a chapter list IS right
    return w


def mine(
    comments: list[dict],
    duration_s: int,
    *,
    window_s: int = 15,
    skip_head_s: int = 10,
    min_mentions: int = 2,
) -> list[Moment]:
    """Rank moments by how much audience attention points at them.

    `window_s` buckets nearby stamps together: people type 2:31, 2:33 and 2:35 for the
    same laugh, and treating those as three separate moments finds nothing.

    `skip_head_s` drops the opening seconds — "0:00" is the single most common timestamp
    in any comment section and it never means "this bit was good".

    `min_mentions` is the honesty filter: one person typing a time is noise, not signal.
    """
    if duration_s <= 0:
        raise ValueError("duration_s must be positive — it bounds which stamps are real")

    buckets: dict[int, Moment] = {}
    for c in comments:
        text = (c or {}).get("text") or ""
        stamps = parse_timestamps(text)
        if not stamps:
            continue
        w = _weight((c or {}).get("like_count"), len(stamps))
        for t in set(stamps):                     # one comment, one vote per moment
            # A stamp past the end belongs to a different video (people paste links and
            # reply about other uploads); a stamp in the first seconds is intro noise.
            if t > duration_s or t < skip_head_s:
                continue
            key = (t // window_s) * window_s
            m = buckets.get(key)
            if m is None:
                m = buckets[key] = Moment(at_s=key, weight=0.0, mentions=0)
            m.weight += w
            m.mentions += 1
            if len(m.samples) < 3:
                m.samples.append(" ".join(text.split())[:120])

    found = [m for m in buckets.values() if m.mentions >= min_mentions]
    # Weight first, then earlier-wins so the order is stable across runs — a ranking that
    # reshuffles on identical input is impossible to review or diff.
    found.sort(key=lambda m: (-m.weight, m.at_s))
    return found


def to_ranges(moments: list[Moment], *, lead_s: int = 8, length_s: int = 30,
              duration_s: int | None = None) -> list[str]:
    """Turn moments into clip_vod.py range strings (START-END).

    `lead_s` starts the clip BEFORE the timestamp: viewers stamp the punchline, not the
    setup, and a clip that opens on the payoff has no context and no hook.
    """
    out = []
    for m in moments:
        start = max(0, m.at_s - lead_s)
        end = start + length_s
        if duration_s:
            end = min(end, duration_s)
        def fmt(x: int) -> str:
            h, rem = divmod(x, 3600)
            mm, ss = divmod(rem, 60)
            return f"{h}:{mm:02d}:{ss:02d}" if h else f"{mm}:{ss:02d}"
        out.append(f"{fmt(start)}-{fmt(end)}")
    return out
