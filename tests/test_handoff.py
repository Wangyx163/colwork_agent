from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from collab_agent.meeting import load_meeting_service
from collab_agent.models import parse_time
from collab_agent.service import NOTIFY_HANDOFF_DECIDED, NOTIFY_HANDOFF_PROPOSED
from collab_agent.store import Database


EXTRACTION = {
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
}


class HandoffTests(unittest.TestCase):
    """Giving your part of a task to somebody else, without dropping it.

    The system already knew this was needed: `不该由我负责，请重新指派` is one
    of the three preset reasons for returning a dispatch. But returning
    supersedes the whole dispatch -- everybody else's acceptance included --
    and sends it back to the coordinator with the thread broken.

    The load-bearing rule here is that proposing transfers nothing. Until the
    other person accepts, the original holder still owes the work; a commitment
    you can put down by naming somebody else is not a commitment.
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
            organization_name="转交测试团队",
            coordinator_name="会议负责人",
            participant_names=["同事甲", "同事乙", "同事丙"],
        )
        self.coordinator = self.service.aggregator_actor_id
        self.actors = {
            row["display_name"]: row["actor_id"]
            for row in self.db.all(
                "SELECT a.actor_id, a.display_name FROM actors a "
                "JOIN episode_participants ep ON ep.actor_id = a.actor_id "
                "WHERE ep.episode_id = ? AND ep.role = 'PARTICIPANT'",
                (self.service.episode_id,),
            )
        }
        self.action_id = next(iter(self.service.action_items()))["action_item_id"]
        self.later = (parse_time(self.service.now()) + timedelta(days=3)).isoformat()
        self.service.revise_action_proposal(
            self.action_id,
            actor_id=self.coordinator,
            title="整理会议纪要",
            deliverable="会议纪要",
            acceptance_criteria="结论清晰",
            priority="P1",
            team_required_by_sim_time=self.later,
            message_id="prepare",
        )

    # ---- helpers -------------------------------------------------------

    def dispatch_and_accept(self, collaborators: list[str] | None = None) -> None:
        self.service.dispatch_action(
            self.action_id,
            actor_id=self.coordinator,
            owner_actor_id=self.actors["同事甲"],
            collaborator_actor_ids=[
                self.actors[name] for name in (collaborators or ["同事乙"])
            ],
            assignment_message="",
            message_id="dispatch",
        )
        for name in ["同事甲", *(collaborators or ["同事乙"])]:
            self.service.respond_to_assignment(
                self.action_id,
                actor_id=self.actors[name],
                decision="ACCEPT",
                response_message="",
                message_id=f"accept-{name}",
            )

    def propose(self, frm: str, to: str, *, reason: str = "这块我不熟", tag: str = "h1"):
        return self.service.propose_handoff(
            self.action_id,
            actor_id=self.actors[frm],
            to_actor_id=self.actors[to],
            reason=reason,
            message_id=f"handoff-{tag}",
        )

    def answer(self, handoff_id: str, who: str, accept: bool, tag: str = "a1"):
        return self.service.respond_to_handoff(
            handoff_id,
            actor_id=self.actors[who],
            accept=accept,
            response_message="好的" if accept else "我这周也满了",
            message_id=f"handoff-answer-{tag}",
        )

    def action(self) -> dict:
        return dict(
            self.db.one(
                "SELECT * FROM action_items WHERE action_item_id = ?",
                (self.action_id,),
            )
        )

    def assignments(self) -> list[dict]:
        return [
            dict(row)
            for row in self.db.all(
                "SELECT actor_id, assignment_role, response_status "
                "FROM action_item_assignments WHERE action_item_id = ?",
                (self.action_id,),
            )
        ]

    def recipients_of(self, effect_type: str) -> set[str]:
        found: set[str] = set()
        for row in self.db.all(
            "SELECT payload FROM outbox_entries WHERE effect_type = ?",
            (effect_type,),
        ):
            found.update(json.loads(row["payload"]).get("recipient_actor_ids") or [])
        return found

    # ---- the rule that makes this a handoff and not a dodge ------------

    def test_proposing_transfers_nothing(self) -> None:
        """The whole point. Until the other person says yes, the work is still
        owed by the person who offered it."""

        self.dispatch_and_accept()
        before = self.action()["owner_actor_id"]

        result = self.propose("同事甲", "同事丙")

        self.assertEqual(result["still_owed_by"], self.actors["同事甲"])
        self.assertEqual(self.action()["owner_actor_id"], before)
        self.assertEqual(
            [a for a in self.assignments() if a["actor_id"] == self.actors["同事甲"]][
                0
            ]["response_status"],
            "ACCEPTED",
        )

    def test_accepting_moves_the_ownership(self) -> None:
        self.dispatch_and_accept()
        handoff = self.propose("同事甲", "同事丙")

        self.answer(handoff["handoff_id"], "同事丙", True)

        self.assertEqual(self.action()["owner_actor_id"], self.actors["同事丙"])
        rows = {a["actor_id"]: a for a in self.assignments()}
        self.assertEqual(rows[self.actors["同事甲"]]["response_status"], "SUPERSEDED")
        self.assertEqual(rows[self.actors["同事丙"]]["response_status"], "ACCEPTED")

    def test_the_receiver_does_not_have_to_accept_twice(self) -> None:
        """Agreeing to the handoff is the acceptance; asking again is theatre
        and would leave the task stalled behind a second confirmation."""

        self.dispatch_and_accept()
        handoff = self.propose("同事甲", "同事丙")
        self.answer(handoff["handoff_id"], "同事丙", True)

        rows = {a["actor_id"]: a for a in self.assignments()}
        self.assertEqual(rows[self.actors["同事丙"]]["response_status"], "ACCEPTED")

    def test_declining_changes_nothing(self) -> None:
        self.dispatch_and_accept()
        before = self.action()["owner_actor_id"]
        handoff = self.propose("同事甲", "同事丙")

        self.answer(handoff["handoff_id"], "同事丙", False)

        self.assertEqual(self.action()["owner_actor_id"], before)
        rows = {a["actor_id"]: a for a in self.assignments()}
        self.assertEqual(rows[self.actors["同事甲"]]["response_status"], "ACCEPTED")

    def test_a_collaborator_may_hand_on_their_own_part(self) -> None:
        """And doing so must not touch who owns the task."""

        self.dispatch_and_accept()
        owner_before = self.action()["owner_actor_id"]
        handoff = self.propose("同事乙", "同事丙")

        self.answer(handoff["handoff_id"], "同事丙", True)

        self.assertEqual(self.action()["owner_actor_id"], owner_before)
        rows = {a["actor_id"]: a for a in self.assignments()}
        self.assertEqual(rows[self.actors["同事丙"]]["assignment_role"], "COLLABORATOR")

    # ---- who may, and to whom -------------------------------------------

    def test_somebody_not_on_the_task_cannot_hand_it_on(self) -> None:
        self.dispatch_and_accept()

        with self.assertRaises(PermissionError):
            self.propose("同事丙", "同事乙")

    def test_it_cannot_leave_the_roster(self) -> None:
        """The roster is the authorization boundary; a handoff that could
        enrol somebody would make the boundary self-service."""

        self.dispatch_and_accept()
        with self.db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO actors(actor_id, organization_id, display_name, "
                "actor_type, status) SELECT 'actor_outsider', organization_id, "
                "'局外人', 'HUMAN', 'ACTIVE' FROM episodes WHERE episode_id = ?",
                (self.service.episode_id,),
            )

        with self.assertRaises(ValueError):
            self.service.propose_handoff(
                self.action_id,
                actor_id=self.actors["同事甲"],
                to_actor_id="actor_outsider",
                reason="给他吧",
                message_id="handoff-outsider",
            )

    def test_it_cannot_go_to_somebody_already_on_the_task(self) -> None:
        self.dispatch_and_accept()

        with self.assertRaises(ValueError):
            self.propose("同事甲", "同事乙")

    def test_a_reason_is_required(self) -> None:
        self.dispatch_and_accept()

        with self.assertRaises(ValueError):
            self.propose("同事甲", "同事丙", reason="   ")

    def test_only_one_open_handoff_per_person(self) -> None:
        """Offering the same part to two people at once means two of them can
        accept, and one of those acceptances has to be undone."""

        self.dispatch_and_accept(collaborators=["同事乙"])
        self.propose("同事甲", "同事丙", tag="first")

        with self.assertRaises(ValueError):
            self.propose("同事甲", "同事乙", tag="second")

    def test_only_the_offered_person_may_answer(self) -> None:
        self.dispatch_and_accept()
        handoff = self.propose("同事甲", "同事丙")

        with self.assertRaises(PermissionError):
            self.answer(handoff["handoff_id"], "同事乙", True)

    def test_a_task_still_being_decided_cannot_be_handed_off(self) -> None:
        """During PENDING_ASSIGNMENT the right move is to return it, which
        lets the coordinator choose again rather than routing around them."""

        self.service.dispatch_action(
            self.action_id,
            actor_id=self.coordinator,
            owner_actor_id=self.actors["同事甲"],
            collaborator_actor_ids=[self.actors["同事乙"]],
            assignment_message="",
            message_id="dispatch",
        )
        self.service.respond_to_assignment(
            self.action_id,
            actor_id=self.actors["同事甲"],
            decision="ACCEPT",
            response_message="",
            message_id="accept-owner",
        )

        with self.assertRaises(ValueError):
            self.propose("同事甲", "同事丙")

    # ---- who hears about it ---------------------------------------------

    def test_the_receiver_and_the_coordinator_both_hear_the_proposal(self) -> None:
        self.dispatch_and_accept()

        self.propose("同事甲", "同事丙")

        told = self.recipients_of(NOTIFY_HANDOFF_PROPOSED)
        self.assertIn(self.actors["同事丙"], told)
        self.assertIn(self.coordinator, told)

    def test_the_proposer_hears_the_answer(self) -> None:
        """The interesting case is the one where they assumed it was dealt
        with."""

        self.dispatch_and_accept()
        handoff = self.propose("同事甲", "同事丙")

        self.answer(handoff["handoff_id"], "同事丙", False)

        self.assertIn(self.actors["同事甲"], self.recipients_of(NOTIFY_HANDOFF_DECIDED))

    def test_a_retry_proposes_once(self) -> None:
        self.dispatch_and_accept()
        first = self.propose("同事甲", "同事丙", tag="same")
        second = self.propose("同事甲", "同事丙", tag="same")

        self.assertEqual(first["handoff_id"], second["handoff_id"])
        self.assertEqual(
            self.db.one(
                "SELECT COUNT(*) AS n FROM assignment_handoffs", ()
            )["n"],
            1,
        )

    def test_it_is_written_to_the_audit_trail(self) -> None:
        self.dispatch_and_accept()
        handoff = self.propose("同事甲", "同事丙")
        self.answer(handoff["handoff_id"], "同事丙", True)

        events = {
            row["event_type"]
            for row in self.db.all("SELECT event_type FROM audit_events", ())
        }
        self.assertIn("AssignmentHandoffProposed", events)
        self.assertIn("AssignmentHandoffAccepted", events)


if __name__ == "__main__":
    unittest.main()
