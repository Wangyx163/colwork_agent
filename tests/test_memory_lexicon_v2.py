from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from collab_agent.meeting import load_meeting_service
from collab_agent.memory_lexicon import (
    MEMORY_TOPICS,
    SELF_DECLARED,
    SYSTEM_OBSERVED,
    assert_lexicon_is_reversible,
    canonical_topic,
    memory_value,
    memory_value_is_current,
    topic_origin,
)
from collab_agent.store import Database


class LexiconContractTests(unittest.TestCase):
    def test_every_topic_passes_the_reversibility_preconditions(self) -> None:
        assert_lexicon_is_reversible()

    def test_no_value_reads_as_a_deficit(self) -> None:
        """Reversibility is the constitutive rule: swapping two values within a
        topic must read as different, never as worse."""

        for topic, spec in MEMORY_TOPICS.items():
            for _, label, hint in spec["values"]:
                self.assertIn("我", label, topic)
                self.assertTrue(hint, topic)

    def test_each_topic_declares_who_may_author_it(self) -> None:
        origins = {topic: topic_origin(topic) for topic in MEMORY_TOPICS}
        self.assertEqual(
            {value for value in origins.values()},
            {SYSTEM_OBSERVED, SELF_DECLARED},
        )
        # Wanting something from others cannot be observed without guessing.
        self.assertEqual(topic_origin("FEEDBACK_STYLE"), SELF_DECLARED)
        self.assertEqual(topic_origin("DELIVERY_RHYTHM"), SYSTEM_OBSERVED)

    def test_v1_entries_still_resolve_after_the_rename(self) -> None:
        self.assertEqual(canonical_topic("UPDATE_STYLE"), "PROGRESS_SIGNAL")
        self.assertEqual(
            memory_value("DELIVERY_STYLE", "ITERATIVE_REVIEW")["code"],
            "ITERATIVE_REVIEW",
        )
        self.assertEqual(
            memory_value("ASSISTANCE_STYLE", "CONTEXT_INCLUDED")["code"],
            "OPTIONS_INCLUDED",
        )

    def test_a_value_outside_the_lexicon_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            memory_value("DELIVERY_RHYTHM", "UNRELIABLE_PERSON")
        with self.assertRaises(ValueError):
            memory_value("NOT_A_TOPIC", "DRAFT_FIRST")

    def test_stale_values_are_detected(self) -> None:
        current = memory_value("HELP_SEEKING", "ASK_WHEN_BLOCKED")
        self.assertTrue(memory_value_is_current("HELP_SEEKING", current))
        self.assertFalse(
            memory_value_is_current(
                "HELP_SEEKING", {**current, "lexicon_version": "v0"}
            )
        )


class SelfDeclaredMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        extraction = root / "extraction.json"
        transcript = root / "transcript.txt"
        extraction.write_text(
            json.dumps(
                {
                    "provider": "fixture",
                    "model": "deterministic",
                    "input_sha256": "e" * 64,
                    "action_items": [
                        {
                            "title": "整理会议纪要",
                            "deliverable": "会议纪要",
                            "owner_name": None,
                            "deadline_text": None,
                            "deadline_iso": None,
                            "source_timestamp": "00:01:00",
                            "source_quote": "请整理会议纪要",
                            "confidence": 0.9,
                            "needs_confirmation": True,
                            "uncertainties": [],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        transcript.write_text("主持人(00:01:00): 请整理会议纪要\n", encoding="utf-8")
        self.db = Database(":memory:")
        self.db.initialize()
        self.service = load_meeting_service(
            self.db,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="Memory 测试团队",
            coordinator_name="会议负责人",
            participant_names=["同事甲"],
        )
        self.actor = self.db.one(
            "SELECT a.actor_id FROM actors a JOIN episode_participants ep "
            "ON ep.actor_id = a.actor_id WHERE ep.episode_id = ? "
            "AND ep.role = 'PARTICIPANT'",
            (self.service.episode_id,),
        )["actor_id"]

    def tearDown(self) -> None:
        self.db.close()
        self.directory.cleanup()

    def stored(self, topic: str) -> list[dict]:
        return [
            dict(row)
            for row in self.db.all(
                "SELECT * FROM collaboration_memories WHERE actor_id = ? "
                "AND topic = ? ORDER BY version",
                (self.actor, topic),
            )
        ]

    def test_a_self_declared_entry_needs_no_draft_stage(self) -> None:
        """These topics exist so a newcomer has a manual on day one, before any
        task history exists to observe."""

        result = self.service.declare_collaboration_memory(
            actor_id=self.actor,
            topic="BRIEF_DETAIL",
            code="CHECKLIST",
            message_id="declare-brief",
        )
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(result["origin"], SELF_DECLARED)
        rows = self.stored("BRIEF_DETAIL")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["origin"], SELF_DECLARED)
        self.assertEqual(rows[0]["confirmed_by"], self.actor)

    def test_redeclaring_supersedes_rather_than_overwrites(self) -> None:
        self.service.declare_collaboration_memory(
            actor_id=self.actor,
            topic="SYNC_PREFERENCE",
            code="ASYNC_TEXT",
            message_id="declare-sync-1",
        )
        self.service.declare_collaboration_memory(
            actor_id=self.actor,
            topic="SYNC_PREFERENCE",
            code="INTERRUPTIBLE",
            message_id="declare-sync-2",
        )
        rows = self.stored("SYNC_PREFERENCE")
        self.assertEqual(len(rows), 2)
        statuses = {row["status"] for row in rows}
        self.assertEqual(statuses, {"SUPERSEDED", "CONFIRMED"})
        current = next(row for row in rows if row["status"] == "CONFIRMED")
        self.assertEqual(current["version"], 2)
        self.assertIsNotNone(current["supersedes_memory_id"])

    def test_an_observed_topic_cannot_be_self_declared(self) -> None:
        """Group A entries must come from evidence; letting someone assert them
        directly would turn the manual into self-reported performance."""

        with self.assertRaisesRegex(ValueError, "must be confirmed"):
            self.service.declare_collaboration_memory(
                actor_id=self.actor,
                topic="DELIVERY_RHYTHM",
                code="DRAFT_FIRST",
                message_id="declare-observed",
            )

    def test_declaration_is_idempotent_per_message(self) -> None:
        first = self.service.declare_collaboration_memory(
            actor_id=self.actor,
            topic="FEEDBACK_STYLE",
            code="DIRECT",
            message_id="declare-once",
        )
        second = self.service.declare_collaboration_memory(
            actor_id=self.actor,
            topic="FEEDBACK_STYLE",
            code="DIRECT",
            message_id="declare-once",
        )
        self.assertEqual(first, second)
        self.assertEqual(len(self.stored("FEEDBACK_STYLE")), 1)

    def test_a_value_outside_the_topic_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.service.declare_collaboration_memory(
                actor_id=self.actor,
                topic="FEEDBACK_STYLE",
                code="DRAFT_FIRST",
                message_id="declare-wrong-code",
            )


if __name__ == "__main__":
    unittest.main()
