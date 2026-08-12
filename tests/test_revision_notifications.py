from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from collab_agent.meeting import load_meeting_service
from collab_agent.models import parse_time
from collab_agent.service import NOTIFY_TASK_AMENDED, NOTIFY_TASK_REVISED
from collab_agent.store import Database


def extraction_payload() -> dict:
    return {
        "provider": "fixture",
        "model": "deterministic",
        "input_sha256": "c" * 64,
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


class RevisionNotificationTests(unittest.TestCase):
    """Somebody returned a task asking for a change. Did the change happen?

    Until this, nothing answered that. The return superseded every assignment
    and cleared the owner, the coordinator rewrote the task, and the audit trail
    recorded both -- while the people who had been working on it were told
    nothing at all. They found out when a new dispatch card arrived, if one
    ever did, and could not tell a rewrite from a silent drop.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        extraction = root / "extraction.json"
        transcript = root / "transcript.txt"
        extraction.write_text(
            json.dumps(extraction_payload(), ensure_ascii=False), encoding="utf-8"
        )
        transcript.write_text("主持人(00:01:00): 请整理会议纪要\n", encoding="utf-8")
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.initialize()
        self.service = load_meeting_service(
            self.db,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="改动通知测试团队",
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
        self.later = (
            parse_time(self.service.now()) + timedelta(days=3)
        ).isoformat()

    # ---- helpers -------------------------------------------------------

    def revise(self, title: str = "整理会议纪要", tag: str = "r1") -> dict:
        return self.service.revise_action_proposal(
            self.action_id,
            actor_id=self.coordinator,
            title=title,
            deliverable="会议纪要",
            acceptance_criteria="结论清晰",
            priority="P1",
            team_required_by_sim_time=self.later,
            message_id=f"revise-{tag}",
        )

    def dispatch(self, tag: str = "d1", collaborators: list[str] | None = None) -> None:
        self.service.dispatch_action(
            self.action_id,
            actor_id=self.coordinator,
            owner_actor_id=self.actors["同事甲"],
            collaborator_actor_ids=collaborators
            if collaborators is not None
            else [self.actors["同事乙"]],
            assignment_message="请在周五前完成",
            message_id=f"dispatch-{tag}",
        )

    def notifications(self, effect_type: str) -> list[dict]:
        rows = self.db.all(
            "SELECT payload FROM outbox_entries WHERE effect_type = ? "
            "ORDER BY created_sim_time",
            (effect_type,),
        )
        return [json.loads(row["payload"]) for row in rows]

    def recipients_of(self, effect_type: str) -> set[str]:
        found: set[str] = set()
        for payload in self.notifications(effect_type):
            found.update(payload.get("recipient_actor_ids") or [])
        return found

    # ---- the gap -------------------------------------------------------

    def test_revising_a_fresh_candidate_tells_nobody(self) -> None:
        """Nobody has seen it, so there is nothing to be told about.

        Pinned so the fix for the returned case does not turn every routine
        edit of an undispatched draft into a message to the whole meeting.
        """

        self.revise()

        self.assertEqual(self.recipients_of(NOTIFY_TASK_REVISED), set())

    def test_revising_a_returned_task_tells_everyone_who_was_on_it(self) -> None:
        self.revise(tag="prepare")
        self.dispatch()
        self.service.respond_to_assignment(
            self.action_id,
            actor_id=self.actors["同事甲"],
            decision="RETURN_FOR_REVISION",
            response_message="验收标准不清楚",
            message_id="return-1",
        )

        self.revise(title="整理会议纪要（补验收标准）", tag="after-return")

        self.assertEqual(
            self.recipients_of(NOTIFY_TASK_REVISED),
            {self.actors["同事甲"], self.actors["同事乙"]},
        )

    def test_the_person_who_returned_it_is_told_what_changed(self) -> None:
        """The whole reason they returned it. A notice that says only "this
        task was edited" leaves them to go and diff it themselves."""

        self.revise(tag="prepare")
        self.dispatch()
        self.service.respond_to_assignment(
            self.action_id,
            actor_id=self.actors["同事甲"],
            decision="RETURN_FOR_REVISION",
            response_message="验收标准不清楚",
            message_id="return-1",
        )

        self.revise(title="整理会议纪要（补验收标准）", tag="after-return")
        body = json.dumps(self.notifications(NOTIFY_TASK_REVISED), ensure_ascii=False)

        self.assertIn("补验收标准", body)

    def test_a_revision_that_changes_nothing_tells_nobody(self) -> None:
        """Opening the form and saving it unchanged is not news."""

        self.revise(tag="prepare")
        self.dispatch()
        self.service.respond_to_assignment(
            self.action_id,
            actor_id=self.actors["同事甲"],
            decision="RETURN_FOR_REVISION",
            response_message="验收标准不清楚",
            message_id="return-1",
        )
        self.revise(title="整理会议纪要（补验收标准）", tag="first")
        before = len(self.notifications(NOTIFY_TASK_REVISED))

        self.revise(title="整理会议纪要（补验收标准）", tag="second")

        self.assertEqual(len(self.notifications(NOTIFY_TASK_REVISED)), before)

    def test_the_coordinator_is_not_told_about_their_own_edit(self) -> None:
        self.revise(tag="prepare")
        self.dispatch()
        self.service.respond_to_assignment(
            self.action_id,
            actor_id=self.actors["同事甲"],
            decision="RETURN_FOR_REVISION",
            response_message="验收标准不清楚",
            message_id="return-1",
        )

        self.revise(title="换个名字", tag="after-return")

        self.assertNotIn(self.coordinator, self.recipients_of(NOTIFY_TASK_REVISED))

    def test_somebody_never_on_the_task_is_not_told(self) -> None:
        """Not a broadcast to the meeting: only the people whose work the
        change actually lands on."""

        self.revise(tag="prepare")
        self.dispatch()
        self.service.respond_to_assignment(
            self.action_id,
            actor_id=self.actors["同事甲"],
            decision="RETURN_FOR_REVISION",
            response_message="验收标准不清楚",
            message_id="return-1",
        )

        self.revise(title="换个名字", tag="after-return")

        self.assertNotIn(self.actors["同事丙"], self.recipients_of(NOTIFY_TASK_REVISED))

    # ---- the amendment path, which reads the wrong list ------------------

    def test_amending_tells_the_people_actually_dispatched_to(self) -> None:
        """The audience came from a copy of the collaborator list kept in the
        proposal metadata. The assignments table is the record of who is on the
        task; reading anything else risks telling a stale list."""

        self.revise(tag="prepare")
        self.dispatch(collaborators=[self.actors["同事乙"], self.actors["同事丙"]])
        for name in ("同事甲", "同事乙", "同事丙"):
            self.service.respond_to_assignment(
                self.action_id,
                actor_id=self.actors[name],
                decision="ACCEPT",
                response_message="",
                message_id=f"accept-{name}",
            )

        self.service.amend_task_description(
            self.action_id,
            actor_id=self.actors["同事甲"],
            title="整理会议纪要（改过）",
            deliverable="会议纪要，含结论清单",
            message_id="amend-1",
        )

        told = self.recipients_of(NOTIFY_TASK_AMENDED)
        self.assertIn(self.actors["同事乙"], told)
        self.assertIn(self.actors["同事丙"], told)
        self.assertIn(self.coordinator, told)

    def test_nobody_may_amend_while_people_are_still_deciding(self) -> None:
        """Not an oversight, and the reason is worth keeping written down.

        `owner_actor_id` is set only once every assignee has accepted, so
        during PENDING_ASSIGNMENT there is no owner and the amendment is
        refused. That is the right rule rather than a gap to widen: people
        deciding whether to accept must be deciding on what they were shown.
        Changing it then is a re-dispatch, which supersedes and asks again.
        """

        self.revise(tag="prepare")
        self.dispatch(collaborators=[self.actors["同事乙"]])
        self.service.respond_to_assignment(
            self.action_id,
            actor_id=self.actors["同事甲"],
            decision="ACCEPT",
            response_message="",
            message_id="accept-owner",
        )

        with self.assertRaises(PermissionError):
            self.service.amend_task_description(
                self.action_id,
                actor_id=self.actors["同事甲"],
                title="整理会议纪要（改过）",
                deliverable="会议纪要，含结论清单",
                message_id="amend-2",
            )


if __name__ == "__main__":
    unittest.main()
