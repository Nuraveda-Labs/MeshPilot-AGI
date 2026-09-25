"""Mining clip moments from comment timestamps.

The failures worth pinning are the ones that produce a CONFIDENT WRONG ANSWER: a chapter
list dominating the ranking, "0:00" winning every video, or a stamp from some other
video pointing past the end of this one. A miner that returns junk with high weight is
worse than one that returns nothing.
"""
from __future__ import annotations

import pytest

from meshpilot.media import clip_signals as cs


def c(text: str, likes: int = 0) -> dict:
    return {"text": text, "like_count": likes}


class TestParsing:
    def test_it_reads_both_clock_forms(self):
        assert cs.parse_timestamps("2:34") == [154]
        assert cs.parse_timestamps("1:02:05") == [3725]

    def test_it_ignores_things_that_merely_look_like_clocks(self):
        """16:9 is an aspect ratio and 1:2:3:4 is a list. Both appear in comments and
        both would otherwise become fake moments."""
        assert cs.parse_timestamps("shot in 16:9 ratio") == []
        assert cs.parse_timestamps("ranked 1:2:3:4") == []
        assert cs.parse_timestamps("no digits here") == []


class TestRanking:
    def test_nearby_stamps_collapse_into_one_moment(self):
        """People type 2:31, 2:33 and 2:35 for the same laugh. Three moments is no
        moment at all."""
        out = cs.mine([c("2:31"), c("2:33"), c("2:35")], duration_s=600, window_s=15)
        assert len(out) == 1
        assert out[0].mentions == 3

    def test_likes_raise_weight_but_do_not_let_one_comment_dominate(self):
        out = cs.mine([c("5:00", likes=5000), c("9:00"), c("9:00"), c("9:00")],
                      duration_s=1200, min_mentions=1)
        by = {m.stamp: m.weight for m in out}
        assert by["5:00"] > by["9:00"] / 2      # a viral comment counts for a lot
        assert by["5:00"] < by["9:00"] * 10     # ...but not unboundedly

    def test_a_chapter_list_is_discounted_not_trusted(self):
        """One person indexing the whole video would otherwise plant evenly-spaced
        'moments' that point at nothing."""
        # HIGHLY LIKED on purpose: without the discount each of its stamps carries
        # ~9.5 weight and buries a genuine moment that two ordinary viewers named.
        # An earlier version of this test used an unliked chapter list, so the real
        # moment won on mention count alone and the discount was never exercised —
        # it passed even with the discount deleted.
        # The genuine moment (4:00) is deliberately NOT one of the chapter marks. An
        # earlier version listed 4:00 among them, so the list's own weight propped up
        # the moment it was competing with and the test passed with the discount
        # deleted — it measured nothing.
        chapters = c("0:30 intro 2:00 part one 6:00 part three 8:00 part four 10:00 end",
                     likes=5000)
        out = cs.mine([chapters, c("4:00"), c("4:02")], duration_s=900, min_mentions=1)
        assert out[0].stamp == "4:00", [(m.stamp, round(m.weight, 2)) for m in out]
        # and the list's own evenly-spaced marks must not outrank it
        others = [m for m in out if m.stamp != "4:00"]
        assert all(m.weight < out[0].weight for m in others)

    def test_stamps_past_the_end_are_dropped(self):
        """Commenters paste times from other videos; a 40:00 stamp on a 10-minute video
        is not a moment in THIS one."""
        out = cs.mine([c("40:00"), c("40:00")], duration_s=600, min_mentions=1)
        assert out == []

    def test_the_intro_is_skipped(self):
        """'0:00' is the most common timestamp in any comment section and never means
        'this bit was good'."""
        out = cs.mine([c("0:00"), c("0:00"), c("0:03")], duration_s=600, min_mentions=1)
        assert out == []

    def test_a_single_mention_is_noise_not_signal(self):
        assert cs.mine([c("3:00")], duration_s=600) == []
        assert len(cs.mine([c("3:00"), c("3:01")], duration_s=600)) == 1

    def test_one_comment_votes_once_per_moment(self):
        """A comment repeating '2:00 ... 2:00' must not count twice."""
        out = cs.mine([c("2:00 lol 2:00 again"), c("2:00")], duration_s=600, min_mentions=1)
        assert out[0].mentions == 2

    def test_ordering_is_stable_for_identical_input(self):
        comments = [c("2:00"), c("2:00"), c("5:00"), c("5:00")]
        a = [m.stamp for m in cs.mine(comments, duration_s=600)]
        b = [m.stamp for m in cs.mine(comments, duration_s=600)]
        assert a == b

    def test_a_zero_duration_is_refused(self):
        with pytest.raises(ValueError, match="duration_s must be positive"):
            cs.mine([c("1:00")], duration_s=0)


class TestRanges:
    def test_the_clip_starts_before_the_stamp(self):
        """Viewers stamp the punchline, not the setup. Opening on the payoff has no hook."""
        m = cs.Moment(at_s=120, weight=1.0, mentions=2)
        assert cs.to_ranges([m], lead_s=8, length_s=30) == ["1:52-2:22"]

    def test_a_range_never_runs_past_the_video(self):
        m = cs.Moment(at_s=590, weight=1.0, mentions=2)
        assert cs.to_ranges([m], lead_s=8, length_s=30, duration_s=600) == ["9:42-10:00"]
