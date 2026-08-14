from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from collab_agent.meeting import load_meeting_service
from collab_agent.models import parse_time
from collab_agent.service import (
    NOTIFY_SCOPE_CHANGE_DECIDED,
    NOTIFY_SCOPE_CHANGE_PROPOSED,
)
from collab_agent.store import Database


EXTRACTION = {
    "provider": "fixture",
    "model": "deterministic",
    "input_sha256": "a" * 63 + "1",
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


class ScopeChangeTests(unittest.TestCase):
    """Owing something different from what was dispatched.

    There were two ways to change a task and neither fit. Amending lets the
    owner reword what the task *says* -- unilateral, and deliberately not a
    change to what is owed. Returning sends the whole dispatch back. So
    somebody who could do most of a task but not all of it had to either
    quietly under-deliver and argue at acceptance, or hand back work they were
    most of the way through.
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
            organization_name="改范围测试团队",
            coordinator_name="会议负责人",
            participant_names=["同事甲", "同事乙"],
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
            deliverable="会议纪要，含三个部门的口径",
            acceptance_criteria="结论清晰",
            priority="P1",
            team_required_by_sim_time=self.later,
            message_id="prepare",
        )
        self.service.dispatch_action(
            self.action_id,
            actor_id=self.coordinator,
            owner_actor_id=self.actors["同事甲"],
            collaborator_actor_ids=[self.actors["同事乙"]],
            assignment_message="",
            message_id="dispatch",
        )
        for name in ("同事甲", "同事乙"):
            self.service.respond_to_assignment(
                self.action_id,
                actor_id=self.actors[name],
                decision="ACCEPT",
                response_message="",
                message_id=f"accept-{name}",
            )

    # ---- helpers -------------------------------------------------------

    def propose(
        self,
        *,
        by: str = "同事甲",
        deliverable: str = "会议纪要，含两个部门的口径",
        reason: str = "第三个部门这周联系不上",
        tag: str = "p1",
    ):
        return self.service.propose_scope_change(
            self.action_id,
            actor_id=self.actors[by],
            proposed_deliverable=deliverable,
            reason=reason,
            message_id=f"scope-{tag}",
        )

    def decide(self, request_id: str, accept: bool, *, comment: str = "行", tag: str = "d1"):
        return self.service.decide_scope_change(
            request_id,
            actor_id=self.coordinator,
            accept=accept,
            comment=comment,
            message_id=f"scope-decide-{tag}",
        )

    def deliverable(self) -> str:
        action = self.db.one(
            "SELECT proposal_metadata FROM action_items WHERE action_item_id = ?",
            (self.action_id,),
        )
        return json.loads(action["proposal_metadata"]).get("deliverable", "")

    def recipients_of(self, effect_type: str) -> set[str]:
        found: set[str] = set()
        for row in self.db.all(
            "SELECT payload FROM outbox_entries WHERE effect_type = ?",
            (effect_type,),
        ):
            found.update(json.loads(row["payload"]).get("recipient_actor_ids") or [])
        return found

    # ---- nothing moves while it is pending ------------------------------

    def test_proposing_changes_nothing(self) -> None:
        """A proposal that silently reduced what was owed would let anybody
        rewrite their own commitment by asking."""

        before = self.deliverable()

        self.propose()

        self.assertEqual(self.deliverable(), before)

    def test_accepting_rewrites_what_is_owed(self) -> None:
        request = self.propose()

        self.decide(request["request_id"], True)

        self.assertEqual(self.deliverable(), "会议纪要，含两个部门的口径")

    def test_accepting_also_moves_the_work_requirements(self) -> None:
        """That field is what the dispatch card actually put in front of
        somebody; leaving it on the old wording makes the task read one way
        here and another way where it was accepted."""

        request = self.propose()

        self.decide(request["request_id"], True)

        metadata = json.loads(
            self.db.one(
                "SELECT proposal_metadata FROM action_items WHERE action_item_id = ?",
                (self.action_id,),
            )["proposal_metadata"]
        )
        self.assertEqual(
            metadata["work_requirements"], "会议纪要，含两个部门的口径"
        )

    def test_declining_leaves_it_alone(self) -> None:
        before = self.deliverable()
        request = self.propose()

        self.decide(request["request_id"], False, comment="第三个部门是重点")

        self.assertEqual(self.deliverable(), before)

    def test_accepting_does_not_supersede_the_dispatch(self) -> None:
        """`definition_version` means "everybody has to answer again". Agreeing
        to a smaller scope with the same people is the opposite of that."""

        before = self.db.one(
            "SELECT definition_version FROM action_items WHERE action_item_id = ?",
            (self.action_id,),
        )["definition_version"]
        request = self.propose()

        self.decide(request["request_id"], True)

        after = self.db.one(
            "SELECT definition_version FROM action_items WHERE action_item_id = ?",
            (self.action_id,),
        )["definition_version"]
        self.assertEqual(after, before)
        rows = self.db.all(
            "SELECT response_status FROM action_item_assignments "
            "WHERE action_item_id = ?",
            (self.action_id,),
        )
        self.assertTrue(all(row["response_status"] == "ACCEPTED" for row in rows))

    # ---- who may ---------------------------------------------------------

    def test_a_collaborator_may_propose(self) -> None:
        """They are doing part of the work, so they meet part of the wall."""

        request = self.propose(by="同事乙", tag="collab")

        self.assertEqual(request["status"], "PENDING")

    def test_somebody_not_on_the_task_may_not(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.propose_scope_change(
                self.action_id,
                actor_id=self.coordinator,
                proposed_deliverable="随便改",
                reason="我想改",
                message_id="scope-outsider",
            )

    def test_only_the_coordinator_decides(self) -> None:
        """Scope is what they dispatched; wording is the owner's, the debt is
        not."""

        request = self.propose()

        with self.assertRaises(PermissionError):
            self.service.decide_scope_change(
                request["request_id"],
                actor_id=self.actors["同事甲"],
                accept=True,
                comment="我自己同意",
                message_id="scope-self-approve",
            )

    # ---- refusals --------------------------------------------------------

    def test_a_reason_is_required_to_propose(self) -> None:
        with self.assertRaises(ValueError):
            self.propose(reason="   ")

    def test_a_reason_is_required_to_decline(self) -> None:
        """A refusal without a reason leaves the proposer with nothing to act
        on, which is how a negotiation becomes a wall."""

        request = self.propose()

        with self.assertRaises(ValueError):
            self.decide(request["request_id"], False, comment="")

    def test_agreeing_needs_no_reason(self) -> None:
        request = self.propose()

        result = self.decide(request["request_id"], True, comment="")

        self.assertEqual(result["status"], "ACCEPTED")

    def test_proposing_the_current_deliverable_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.propose(deliverable="会议纪要，含三个部门的口径")

    def test_only_one_open_request_per_task(self) -> None:
        self.propose(tag="first")

        with self.assertRaises(ValueError):
            self.propose(deliverable="再少一点", tag="second")

    def test_answering_twice_is_refused(self) -> None:
        request = self.propose()
        self.decide(request["request_id"], True, tag="once")

        with self.assertRaises(ValueError):
            self.decide(request["request_id"], False, comment="改主意了", tag="twice")

    # ---- who hears -------------------------------------------------------

    def test_the_coordinator_hears_the_proposal(self) -> None:
        self.propose()

        self.assertEqual(
            self.recipients_of(NOTIFY_SCOPE_CHANGE_PROPOSED), {self.coordinator}
        )

    def test_everybody_on_the_task_hears_the_answer(self) -> None:
        """Including the collaborator, whose work just changed shape."""

        request = self.propose()

        self.decide(request["request_id"], True)

        told = self.recipients_of(NOTIFY_SCOPE_CHANGE_DECIDED)
        self.assertIn(self.actors["同事甲"], told)
        self.assertIn(self.actors["同事乙"], told)

    def test_the_trail_records_both_sides_of_the_change(self) -> None:
        request = self.propose()
        self.decide(request["request_id"], True)

        events = {
            row["event_type"]
            for row in self.db.all("SELECT event_type FROM audit_events", ())
        }
        self.assertIn("ScopeChangeProposed", events)
        self.assertIn("ScopeChangeAccepted", events)

    def test_a_retry_proposes_once(self) -> None:
        first = self.propose(tag="same")
        second = self.propose(tag="same")

        self.assertEqual(first["request_id"], second["request_id"])


if __name__ == "__main__":
    unittest.main()
