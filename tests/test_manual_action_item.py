from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from collab_agent.meeting import load_meeting_service
from collab_agent.models import ActionItemStatus, parse_time
from collab_agent.store import Database


EXTRACTION = {
    "provider": "fixture",
    "model": "deterministic",
    "input_sha256": "f" * 64,
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
}


class ManualActionItemTests(unittest.TestCase):
    """Extraction proposes what it recognised; a meeting always says more.

    The gap used to be closed by hand in the worst possible way -- editing a
    neighbouring task until it covered two things -- because there was no way
    to enter a task the model had not proposed. What matters as much as the
    entry existing is that it stays distinguishable from an extracted one: the
    extracted item's quote has been checked against the transcript, and this
    one's has not, so a reader auditing provenance must be able to tell.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        extraction = root / "extraction.json"
        transcript = root / "transcript.txt"
        extraction.write_text(
            json.dumps(EXTRACTION, ensure_ascii=False), encoding="utf-8"
        )
        transcript.write_text("主持人(00:01:00): 请整理会议纪要\n", encoding="utf-8")
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.initialize()
        self.service = load_meeting_service(
            self.db,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="手工补录测试团队",
            coordinator_name="会议负责人",
            participant_names=["同事甲", "同事乙"],
        )
        self.coordinator = self.service.aggregator_actor_id
        self.member = next(
            row["actor_id"]
            for row in self.db.all(
                "SELECT a.actor_id FROM actors a "
                "JOIN episode_participants ep ON ep.actor_id = a.actor_id "
                "WHERE ep.episode_id = ? AND ep.role = 'PARTICIPANT'",
                (self.service.episode_id,),
            )
        )
        self.later = (parse_time(self.service.now()) + timedelta(days=3)).isoformat()

    def add(self, **overrides):
        payload = {
            "actor_id": self.coordinator,
            "title": "补一条会上说到但没抽出来的",
            "deliverable": "一页说明",
            "source_note": "散会前口头提的，逐字稿没录进去",
            "message_id": "manual-1",
        }
        payload.update(overrides)
        return self.service.add_action_item(**payload)

    def test_it_lands_where_an_extracted_candidate_lands(self) -> None:
        """No owner and PENDING_CONFIRMATION, so it goes through dispatch like
        any other candidate rather than skipping to the front of the queue."""

        result = self.add()
        row = self.service.db.one(
            "SELECT status, owner_actor_id FROM action_items WHERE action_item_id = ?",
            (result["action_item_id"],),
        )

        self.assertEqual(row["status"], ActionItemStatus.PENDING_CONFIRMATION)
        self.assertIsNone(row["owner_actor_id"])

    def test_it_says_a_person_added_it(self) -> None:
        """The one thing that must never blur: this note was not verified
        against the transcript the way an extracted quote was."""

        result = self.add()
        row = self.service.db.one(
            "SELECT proposal_metadata FROM action_items WHERE action_item_id = ?",
            (result["action_item_id"],),
        )
        metadata = json.loads(row["proposal_metadata"])

        self.assertEqual(metadata["origin"], "COORDINATOR_ADDED")
        self.assertEqual(metadata["added_by_actor_id"], self.coordinator)
        self.assertIsNone(metadata["source_timestamp"])

    def test_only_the_coordinator_may_add_one(self) -> None:
        with self.assertRaises(PermissionError):
            self.add(actor_id=self.member, message_id="manual-2")

    def test_it_refuses_a_task_with_no_stated_source(self) -> None:
        """Without this the trail says only that somebody typed something."""

        with self.assertRaises(ValueError):
            self.add(source_note="   ", message_id="manual-3")

    def test_it_refuses_an_empty_title_or_deliverable(self) -> None:
        with self.assertRaises(ValueError):
            self.add(title="  ", message_id="manual-4")
        with self.assertRaises(ValueError):
            self.add(deliverable="", message_id="manual-5")

    def test_it_refuses_a_team_time_already_past(self) -> None:
        with self.assertRaises(ValueError):
            self.add(
                team_required_by_sim_time="2000-01-01T09:00:00+10:00",
                message_id="manual-6",
            )

    def test_a_retry_adds_one_task_not_two(self) -> None:
        """A double-clicked button is the ordinary case, not the exotic one."""

        first = self.add()
        second = self.add()

        self.assertEqual(first["action_item_id"], second["action_item_id"])
        self.assertEqual(
            self.service.db.one(
                "SELECT COUNT(*) AS n FROM action_items WHERE source_message_id = ?",
                ("manual-1",),
            )["n"],
            1,
        )

    def test_adding_one_is_written_to_the_audit_trail(self) -> None:
        result = self.add()
        events = self.service.db.all(
            "SELECT event_type FROM audit_events WHERE aggregate_id = ?",
            (result["action_item_id"],),
        )

        self.assertIn(
            "ActionItemAddedByCoordinator", [event["event_type"] for event in events]
        )

    def test_it_can_then_be_dispatched_like_any_other(self) -> None:
        """The point of adding one is that it joins the same pipeline; a task
        that cannot be dispatched would be a note, not a task."""

        result = self.add()
        self.service.revise_action_proposal(
            result["action_item_id"],
            actor_id=self.coordinator,
            title="补一条会上说到但没抽出来的",
            deliverable="一页说明",
            acceptance_criteria="写清楚就行",
            priority="P1",
            message_id="manual-revise",
            team_required_by_sim_time=self.later,
        )
        self.service.dispatch_action(
            result["action_item_id"],
            actor_id=self.coordinator,
            owner_actor_id=self.member,
            collaborator_actor_ids=[],
            assignment_message="",
            message_id="manual-dispatch",
        )

        self.assertEqual(
            self.service.db.one(
                "SELECT status FROM action_items WHERE action_item_id = ?",
                (result["action_item_id"],),
            )["status"],
            ActionItemStatus.PENDING_ASSIGNMENT,
        )


if __name__ == "__main__":
    unittest.main()
