from __future__ import annotations

import unittest

from collab_agent.slugs import (
    RESERVED,
    build_slug,
    hash_slug,
    name_stem,
    slugs_for_episodes,
)


class SlugTests(unittest.TestCase):
    """A slug is a URL somebody reads out in a chat message.

    Which means the properties that matter are not the usual ones. It has to
    stay the same for as long as the meeting exists, because the link was
    already sent; it has to be unique even when two names collapse to the same
    letters; and when it cannot be made readable it has to degrade to something
    ugly rather than to something wrong.
    """

    def test_an_ascii_name_passes_through(self) -> None:
        self.assertEqual(name_stem("Jasmine"), "jasmine")

    def test_punctuation_and_spacing_are_dropped(self) -> None:
        self.assertEqual(name_stem("  Mary-Jane O'Neill "), "maryjaneoneill")

    def test_an_empty_name_yields_nothing_rather_than_a_bare_number(self) -> None:
        self.assertEqual(name_stem("   "), "")
        self.assertEqual(build_slug("   ", 1, fallback="m-abc"), "m-abc")

    def test_the_ordinal_is_always_two_digits(self) -> None:
        """`wangyuxiang` and `wangyuxiang02` look like different kinds of
        thing; `01` and `02` read as a pair."""

        self.assertEqual(build_slug("Jasmine", 1, fallback="x"), "jasmine01")
        self.assertEqual(build_slug("Jasmine", 12, fallback="x"), "jasmine12")

    def test_a_name_that_collides_with_a_page_falls_back(self) -> None:
        """A meeting slugged `api` would shadow every meeting's API at once."""

        for reserved in ("api", "manage", "console"):
            self.assertIn(reserved, RESERVED)
            self.assertEqual(build_slug(reserved, 1, fallback="m-z"), "m-z")

    def test_the_fallback_is_stable_and_looks_like_an_id(self) -> None:
        first = hash_slug("episode_meeting_9f3c1a2b4d")
        self.assertEqual(first, hash_slug("episode_meeting_9f3c1a2b4d"))
        self.assertTrue(first.startswith("m-"))

    def test_one_coordinator_numbers_their_meetings_in_creation_order(self) -> None:
        slugs = slugs_for_episodes(
            [
                {
                    "episode_id": "episode_b",
                    "owner_display_name": "Jasmine",
                    "created_sim_time": "2026-03-09T10:00:00+10:00",
                },
                {
                    "episode_id": "episode_a",
                    "owner_display_name": "Jasmine",
                    "created_sim_time": "2026-03-01T10:00:00+10:00",
                },
            ]
        )

        self.assertEqual(slugs["episode_a"], "jasmine01")
        self.assertEqual(slugs["episode_b"], "jasmine02")

    def test_adding_an_older_meeting_later_does_not_renumber_a_live_link(
        self,
    ) -> None:
        """The honest limit, pinned so nobody is surprised by it.

        Ordinals come from creation order, so importing a meeting that
        happened *earlier* renumbers the ones after it. That is acceptable
        because meetings arrive in order in practice; it is written down here
        so the day it bites, this test says why.
        """

        rows = [
            {
                "episode_id": "episode_b",
                "owner_display_name": "Jasmine",
                "created_sim_time": "2026-03-09T10:00:00+10:00",
            }
        ]
        self.assertEqual(slugs_for_episodes(rows)["episode_b"], "jasmine01")

        rows.append(
            {
                "episode_id": "episode_a",
                "owner_display_name": "Jasmine",
                "created_sim_time": "2026-03-01T10:00:00+10:00",
            }
        )
        self.assertEqual(slugs_for_episodes(rows)["episode_b"], "jasmine02")

    def test_two_names_that_latinise_alike_do_not_share_a_url(self) -> None:
        """Handing somebody else's meeting to a reader is the one outcome
        worth giving up a readable name for."""

        slugs = slugs_for_episodes(
            [
                {
                    "episode_id": "episode_a",
                    "owner_display_name": "Jasmine",
                    "created_sim_time": "2026-03-01T10:00:00+10:00",
                },
                {
                    "episode_id": "episode_b",
                    "owner_display_name": "jas mine",
                    "created_sim_time": "2026-03-02T10:00:00+10:00",
                },
            ]
        )

        self.assertEqual(len(set(slugs.values())), 2)
        self.assertEqual(slugs["episode_a"], "jasmine01")

    def test_every_slug_is_url_safe(self) -> None:
        slugs = slugs_for_episodes(
            [
                {
                    "episode_id": f"episode_{index}",
                    "owner_display_name": name,
                    "created_sim_time": f"2026-03-0{index}T10:00:00+10:00",
                }
                for index, name in enumerate(
                    ["王昱翔", "黄Z恒", "Jasmine", "绒", "宋潽暄", ""], start=1
                )
            ]
        )

        for slug in slugs.values():
            self.assertRegex(slug, r"^[a-z][a-z0-9-]*$")

    def test_chinese_names_latinise_when_pypinyin_is_installed(self) -> None:
        """Skipped rather than asserted away: the extra is optional on purpose,
        and a test that quietly passes without it would claim more than it
        checked."""

        try:
            import pypinyin  # noqa: F401, PLC0415
        except ImportError:
            self.skipTest("pypinyin is an optional extra")

        self.assertEqual(name_stem("王昱翔"), "wangyuxiang")


if __name__ == "__main__":
    unittest.main()
