from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from collab_agent.meeting import load_meeting_service
from collab_agent.memory_lexicon import memory_value
from collab_agent.models import canonical_json, parse_time
from collab_agent.store import Database


class P1MemoryContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        extraction = root / "extraction.json"
        transcript = root / "transcript.txt"
        extraction.write_text(
            json.dumps(
                {
                    "provider": "test",
                    "model": "none",
                    "input_sha256": "d" * 64,
                    "action_items": [
                        {
                            "title": "Prepare launch brief",
                            "deliverable": "A reviewed launch brief",
                            "owner_name": None,
                            "deadline_text": None,
                            "deadline_iso": None,
                            "source_timestamp": "00:01:00",
                            "source_quote": "Alice prepares the launch brief",
                            "confidence": 0.95,
                            "needs_confirmation": True,
                            "uncertainties": ["owner requires confirmation"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        transcript.write_text(
            "Host (00:01:00): Alice prepares the launch brief\n",
            encoding="utf-8",
        )
        self.db = Database(":memory:")
        self.db.initialize()
        self.service = load_meeting_service(
            self.db,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="P1 test team",
            coordinator_name="Coordinator",
            participant_names=["Alice", "Bob"],
        )
        self.action = next(iter(self.service.action_items()))
        metadata = self.service.proposal_metadata(self.action)
        deadline = (parse_time(self.service.now()) + timedelta(days=2)).isoformat()
        coordinator = self.service.aggregator_actor_id
        self.service.revise_action_proposal(
            self.action["action_item_id"],
            actor_id=coordinator,
            title=self.action["title"],
            deliverable=metadata["deliverable"],
            acceptance_criteria="Contains a launch decision and sources",
            priority="P1",
            team_required_by_sim_time=deadline,
            message_id="memory-prepare",
        )
        self.service.publish_action(
            self.action["action_item_id"],
            actor_id=coordinator,
            message_id="memory-publish",
        )
        claim = self.service.claim_action(
            self.action["action_item_id"],
            claimant_name="Alice",
            promised_deadline_sim_time=deadline,
            message_id="memory-claim",
        )
        self.alice_id = claim["owner_actor_id"]
        self.bob_id = self.db.one(
            "SELECT actor_id FROM actors WHERE display_name = 'Bob'"
        )["actor_id"]
        now = self.service.now()
        with self.db.transaction() as cursor:
            # Lexicon-shaped rows: the code is the fact, and the words shown to
            # a colleague are derived from it. A row carrying only a sentence
            # is a pre-lexicon leftover and is deliberately not projectable, so
            # using that shape here would test the wrong thing.
            for memory_id, actor_id, status, code in (
                ("memory_alice_confirmed", self.alice_id, "CONFIRMED", "CONCLUSION_AND_BOUNDS"),
                ("memory_alice_draft", self.alice_id, "PRIVATE_DRAFT", "CHECKLIST"),
                ("memory_bob_confirmed", self.bob_id, "CONFIRMED", "CONTEXT_FIRST"),
            ):
                cursor.execute(
                    "INSERT INTO collaboration_memories(memory_id, actor_id, topic, "
                    "value, visibility, status, evidence_refs, created_sim_time, "
                    "version, confirmed_by, confirmed_sim_time) "
                    "VALUES (?, ?, 'BRIEF_DETAIL', ?, 'PRIVATE', ?, ?, ?, 1, ?, ?)",
                    (
                        memory_id,
                        actor_id,
                        canonical_json(memory_value("BRIEF_DETAIL", code)),
                        status,
                        canonical_json([f"event:{memory_id}"]),
                        now,
                        actor_id if status == "CONFIRMED" else None,
                        now if status == "CONFIRMED" else None,
                    ),
                )

    def tearDown(self) -> None:
        self.db.close()
        self.directory.cleanup()

    def test_context_injects_only_own_confirmed_memory_and_is_idempotent(self) -> None:
        first = self.service.build_collaboration_hint_context(
            self.action["action_item_id"],
            actor_id=self.alice_id,
            message_id="memory-context-1",
        )
        second = self.service.build_collaboration_hint_context(
            self.action["action_item_id"],
            actor_id=self.alice_id,
            message_id="memory-context-1",
        )

        self.assertEqual(first, second)
        self.assertEqual(first["context"]["purpose"], "COLLABORATION_HINT")
        self.assertEqual(
            [item["memory_id"] for item in first["context"]["confirmed_memories"]],
            ["memory_alice_confirmed"],
        )
        self.assertIn(
            "decide acceptance", first["context"]["usage_rules"]["must_not"]
        )
        self.assertEqual(
            self.db.one(
                "SELECT COUNT(*) AS count FROM audit_events "
                "WHERE event_type = 'CollaborationHintContextBuilt'"
            )["count"],
            1,
        )

    def test_non_contributor_cannot_read_memory_context(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.build_collaboration_hint_context(
                self.action["action_item_id"],
                actor_id=self.bob_id,
                message_id="memory-context-bob",
            )


if __name__ == "__main__":
    unittest.main()
