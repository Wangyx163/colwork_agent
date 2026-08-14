from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from collab_agent.meeting import load_meeting_service
from collab_agent.models import parse_time
from collab_agent.service import NOTIFY_INTERIM_DELIVERY
from collab_agent.store import Database


EXTRACTION = {
    "provider": "fixture",
    "model": "deterministic",
    "input_sha256": "f" * 63 + "0",
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


class InterimDeliveryTests(unittest.TestCase):
    """Handing over part of the work without calling it finished.

    Submitting used to be all or nothing: whatever the owner sent moved the
    task into acceptance. So somebody with a usable half either sat on it until
    the rest was done, or handed it in and had it rejected for being partial.

    No new state carries this. A collaborator's contribution already means
    "content that does not move the task into acceptance"; an interim delivery
    is the same shape with a different author, and takes the same path.
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
            organization_name="部分交付测试团队",
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
        later = (parse_time(self.service.now()) + timedelta(days=3)).isoformat()
        self.service.revise_action_proposal(
            self.action_id,
            actor_id=self.coordinator,
            title="整理会议纪要",
            deliverable="会议纪要",
            acceptance_criteria="结论清晰",
            priority="P1",
            team_required_by_sim_time=later,
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

    def submit(self, *, interim: bool, tag: str = "s1", by: str = "同事甲"):
        return self.service.submit_artifact(
            self.action_id,
            actor_id=self.actors[by],
            message_id=f"submit-{tag}",
            payload={"summary": "先给能用的一半", "content": "第一部分结论"},
            interim=interim,
        )

    def status(self) -> str:
        return self.db.one(
            "SELECT status FROM action_items WHERE action_item_id = ?",
            (self.action_id,),
        )["status"]

    def notices(self) -> list[dict]:
        return [
            json.loads(row["payload"])
            for row in self.db.all(
                "SELECT payload FROM outbox_entries WHERE effect_type = ?",
                (NOTIFY_INTERIM_DELIVERY,),
            )
        ]

    # ---- what it does ---------------------------------------------------

    def test_an_interim_delivery_does_not_move_the_task_into_acceptance(
        self,
    ) -> None:
        self.submit(interim=True)

        self.assertEqual(self.status(), "TRACKING")

    def test_a_normal_submission_still_does(self) -> None:
        """The default has to stay what it was, or every existing caller
        quietly changes meaning."""

        self.submit(interim=False)

        self.assertEqual(self.status(), "PENDING_ACCEPTANCE")

    def test_the_content_is_stored_and_readable(self) -> None:
        """The point of an interim delivery over a progress note is that there
        is something to look at."""

        result = self.submit(interim=True)

        stored = self.db.one(
            "SELECT payload, review_status FROM artifact_versions "
            "WHERE version_id = ?",
            (result["version_id"],),
        )
        self.assertIn("第一部分结论", stored["payload"])
        self.assertEqual(stored["review_status"], "NOT_REQUIRED")

    def test_it_is_recorded_as_its_own_kind_of_event(self) -> None:
        """A reader auditing the trail must be able to tell an interim
        delivery from a collaborator's contribution; they take the same path
        through the state machine and mean different things."""

        self.submit(interim=True)

        events = {
            row["event_type"]
            for row in self.db.all("SELECT event_type FROM audit_events", ())
        }
        self.assertIn("ArtifactInterimDelivered", events)
        self.assertNotIn("ArtifactContributionAwaitingOwner", events)

    # ---- who hears -------------------------------------------------------

    def test_the_coordinator_and_the_collaborators_hear_it(self) -> None:
        """Progress that only the person who made it can see is a progress
        note nobody reads."""

        self.submit(interim=True)

        told: set[str] = set()
        for payload in self.notices():
            told.update(payload.get("recipient_actor_ids") or [])
        self.assertIn(self.coordinator, told)
        self.assertIn(self.actors["同事乙"], told)
        self.assertNotIn(self.actors["同事甲"], told)

    def test_a_collaborator_contribution_is_not_an_interim_delivery(self) -> None:
        """It goes to the owner to decide on, not to the coordinator as
        progress -- which is why the two keep separate events."""

        self.submit(interim=False, by="同事乙", tag="contrib")

        self.assertEqual(self.notices(), [])
        events = {
            row["event_type"]
            for row in self.db.all("SELECT event_type FROM audit_events", ())
        }
        self.assertIn("ArtifactContributionAwaitingOwner", events)

    # ---- and then the real one -------------------------------------------

    def test_a_final_submission_after_an_interim_still_enters_acceptance(
        self,
    ) -> None:
        self.submit(interim=True, tag="half")

        self.submit(interim=False, tag="whole")

        self.assertEqual(self.status(), "PENDING_ACCEPTANCE")


if __name__ == "__main__":
    unittest.main()
