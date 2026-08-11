from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from collab_agent import episode_registry
from collab_agent.meeting import load_meeting_service
from collab_agent.store import Database


def extraction_for(quote: str) -> dict:
    return {
        "provider": "fixture",
        "model": "deterministic",
        "input_sha256": hashlib.sha256(quote.encode()).hexdigest(),
        "action_items": [
            {
                "title": f"任务：{quote}",
                "deliverable": "一页说明",
                "owner_name": None,
                "deadline_text": None,
                "deadline_iso": None,
                "source_timestamp": "00:01:00",
                "source_quote": quote,
                "confidence": 0.9,
                "needs_confirmation": True,
                "uncertainties": [],
            }
        ],
    }


class EpisodeRegistryTests(unittest.TestCase):
    """What it takes to bring a meeting back up, written down once.

    The alternative was reconstructing a fixture out of the database, which
    would be a second definition of what a meeting is -- and the day the two
    disagreed, the served meeting would stop matching the audited one without
    anything failing.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.initialize()

    def make_meeting(self, coordinator: str, quote: str):
        extraction = self.root / f"{quote}.json"
        transcript = self.root / f"{quote}.txt"
        extraction.write_text(
            json.dumps(extraction_for(quote), ensure_ascii=False), encoding="utf-8"
        )
        transcript.write_text(f"主持人(00:01:00): {quote}\n", encoding="utf-8")
        service = load_meeting_service(
            self.db,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="注册表测试团队",
            coordinator_name=coordinator,
            participant_names=[coordinator, "同事甲"],
        )
        return service, extraction, transcript

    def register(self, coordinator: str, quote: str):
        service, extraction, transcript = self.make_meeting(coordinator, quote)
        return episode_registry.register(
            self.db,
            episode_id=service.episode_id,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="注册表测试团队",
            coordinator_name=coordinator,
            participant_names=[coordinator, "同事甲"],
            timezone="Australia/Sydney",
            sim_time=service.now(),
        )

    def test_a_registered_meeting_can_be_brought_back_from_its_row(self) -> None:
        source = self.register("Jasmine", "请把周报整理出来")

        revived = load_meeting_service(
            self.db,
            extraction_path=source.extraction_path,
            transcript_path=source.transcript_path,
            organization_name=source.organization_name,
            coordinator_name=source.coordinator_name,
            participant_names=source.participant_names,
            timezone=source.timezone,
        )

        self.assertEqual(revived.episode_id, source.episode_id)

    def test_one_coordinator_numbers_their_meetings(self) -> None:
        first = self.register("Jasmine", "请把周报整理出来")
        second = self.register("Jasmine", "请把预算表核一遍")

        self.assertEqual(first.slug, "jasmine01")
        self.assertEqual(second.slug, "jasmine02")

    def test_a_slug_never_moves_once_given(self) -> None:
        """The whole reason it is stored rather than recomputed: the link was
        already sent, and a URL that moves is a URL that breaks."""

        first = self.register("Jasmine", "请把周报整理出来")
        self.register("Jasmine", "请把预算表核一遍")
        self.register("Jasmine", "请把客户名单更新一下")

        again = episode_registry.source_for(self.db, first.episode_id)
        assert again is not None
        self.assertEqual(again.slug, first.slug)

    def test_re_registering_updates_the_paths_and_keeps_the_slug(self) -> None:
        """Serving the same meeting from a moved file is ordinary; being given
        a new URL for it is not."""

        first = self.register("Jasmine", "请把周报整理出来")
        moved = self.root / "elsewhere.json"
        moved.write_text(
            (self.root / "请把周报整理出来.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        again = episode_registry.register(
            self.db,
            episode_id=first.episode_id,
            extraction_path=moved,
            transcript_path=first.transcript_path,
            organization_name=first.organization_name,
            coordinator_name=first.coordinator_name,
            participant_names=first.participant_names,
            timezone=first.timezone,
            sim_time="2026-08-12T09:00:00+10:00",
        )

        self.assertEqual(again.slug, first.slug)
        self.assertEqual(again.extraction_path, str(moved.resolve()))

    def test_paths_are_stored_resolved(self) -> None:
        """A relative path recorded from one working directory and read from
        another points at nothing, and the failure reads as a missing meeting
        rather than a missing file."""

        source = self.register("Jasmine", "请把周报整理出来")

        self.assertTrue(Path(source.extraction_path).is_absolute())
        self.assertTrue(source.files_present)

    def test_a_meeting_whose_files_left_reports_it_rather_than_vanishing(
        self,
    ) -> None:
        source = self.register("Jasmine", "请把周报整理出来")
        Path(source.extraction_path).unlink()

        again = episode_registry.source_for(self.db, source.episode_id)
        assert again is not None
        self.assertFalse(again.files_present)
        self.assertIn(again.slug, [row.slug for row in episode_registry.list_sources(self.db)])

    def test_two_coordinators_get_two_slugs(self) -> None:
        first = self.register("Jasmine", "请把周报整理出来")
        second = self.register("Rachel", "请把预算表核一遍")

        self.assertNotEqual(first.slug, second.slug)

    def test_the_title_comes_from_what_the_meeting_was_called(self) -> None:
        """Not from the coordinator plus the import date: five meetings
        imported on one afternoon all read the same that way, and the date
        shown was the day the file was read rather than the day people met."""

        source = self.root / "named.json"
        source.write_text(
            json.dumps(
                {
                    **extraction_for("请把周报整理出来"),
                    "source": {
                        "filename": "20260309214014-王昱翔的快速会议-逐字稿文本-1.txt"
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            episode_registry.title_from_extraction(source),
            "王昱翔的快速会议 · 2026-03-09",
        )

    def test_an_extraction_with_no_source_name_yields_no_title(self) -> None:
        """Empty rather than invented, so the caller falls back to something
        it can defend rather than to a name nobody chose."""

        source = self.root / "anonymous.json"
        source.write_text(
            json.dumps(extraction_for("请把周报整理出来"), ensure_ascii=False),
            encoding="utf-8",
        )

        self.assertEqual(episode_registry.title_from_extraction(source), "")

    def test_lookup_by_slug_finds_the_meeting(self) -> None:
        source = self.register("Jasmine", "请把周报整理出来")

        found = episode_registry.source_for_slug(self.db, source.slug)

        assert found is not None
        self.assertEqual(found.episode_id, source.episode_id)


if __name__ == "__main__":
    unittest.main()
