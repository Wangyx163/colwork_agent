from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from collab_agent.auth import Principal, PrincipalRole
from collab_agent.meeting import load_meeting_service
from collab_agent.models import parse_time
from collab_agent.store import Database
from collab_agent.web import workbench_state


ROOT = Path(__file__).resolve().parents[1]


def extraction_payload() -> dict:
    return {
        "provider": "fixture",
        "model": "deterministic",
        "input_sha256": "d" * 64,
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


#: What the pages read, per projected object. Kept as plain strings because
#: that is what actually breaks: the page reaches for a key, gets undefined,
#: renders a blank or builds a URL with "undefined" in it, and nothing raises.
#:
#: Six separate defects came from guessing these instead of copying them out
#: of a payload -- an empty delivery panel, blank assignee names, a filter that
#: matched nothing, and a request to /api/assistance/undefined/acknowledge.
READ_BY_PAGES = {
    "task": (
        "action_item_id",
        "title",
        "status",
        "required",
        "owner_actor_id",
        "owner_display_name",
        "team_required_by_sim_time",
        "promised_by_sim_time",
        "schedule_risk_reason",
        "source_span",
        "proposal_metadata",
        "activity",
        "assignments",
        "current_assignments",
        "assistance_requests",
        "active_assistance",
        "collaborators",
        "contribution_versions",
        "latest_version",
        "current_version",
        "is_mine",
        "is_collaborator",
        "can_contribute",
        "my_assignment",
        "last_owner_signal",
    ),
    "assignment": (
        "assignment_id",
        "assignment_role",
        "response_status",
        "display_name",
    ),
    "assistance_request": (
        "assistance_request_id",
        "status",
        "summary",
        "requester_actor_id",
        "requester_display_name",
        "target_actor_id",
        "target_display_name",
    ),
    "version": (
        "version_id",
        "payload",
        "received_sequence",
        "received_sim_time",
        "review_comment",
        "attachment_extractions",
        "processing_status",
        "submitted_by_display_name",
    ),
    "activity": ("kind", "title", "detail", "status", "actor", "sim_time"),
    "state": (
        "episode",
        "vocabulary",
        "tasks",
        "notices",
        "pending_approvals",
        "final",
        "participants",
        "principal",
        "allowed_surfaces",
        "memories",
        "memory_lexicon",
        "meeting_progress",
    ),
}


class ProjectionFieldNamesTests(unittest.TestCase):
    """The names the pages reach for have to be the names the server sends.

    TypeScript cannot catch this: a declared field that the payload never
    carries typechecks perfectly and evaluates to undefined at runtime. So the
    contract is checked here, against a projection built from a real meeting,
    and a rename on the server names the page it breaks.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        extraction = root / "extraction.json"
        transcript = root / "transcript.txt"
        extraction.write_text(
            json.dumps(extraction_payload(), ensure_ascii=False), encoding="utf-8"
        )
        transcript.write_text("主持人(00:01:00): 请整理会议纪要\n", encoding="utf-8")
        cls.db = Database(":memory:")
        cls.db.initialize()
        cls.service = load_meeting_service(
            cls.db,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="字段名测试团队",
            coordinator_name="会议负责人",
            participant_names=["同事甲", "同事乙"],
        )
        coordinator = cls.service.aggregator_actor_id
        actors = {
            row["display_name"]: row["actor_id"]
            for row in cls.db.all(
                "SELECT a.actor_id, a.display_name FROM actors a "
                "JOIN episode_participants ep ON ep.actor_id = a.actor_id "
                "WHERE ep.episode_id = ? AND ep.role = 'PARTICIPANT'",
                (cls.service.episode_id,),
            )
        }
        action = next(iter(cls.service.action_items()))
        action_id = action["action_item_id"]
        from datetime import timedelta

        cls.service.revise_action_proposal(
            action_id,
            actor_id=coordinator,
            title=action["title"],
            deliverable="会议纪要",
            acceptance_criteria="结论清晰",
            priority="P1",
            team_required_by_sim_time=(
                parse_time(cls.service.now()) + timedelta(days=3)
            ).isoformat(),
            message_id="fields-revise",
        )
        cls.service.dispatch_action(
            action_id,
            actor_id=coordinator,
            owner_actor_id=actors["同事甲"],
            collaborator_actor_ids=[actors["同事乙"]],
            assignment_message="请这周",
            message_id="fields-dispatch",
        )
        for index, name in enumerate(("同事甲", "同事乙")):
            cls.service.respond_to_assignment(
                action_id,
                actor_id=actors[name],
                decision="ACCEPT",
                response_message="",
                message_id=f"fields-accept-{index}",
            )
        cls.service.request_assistance(
            action_id,
            actor_id=actors["同事甲"],
            target_actor_id=actors["同事乙"],
            category="EXPERTISE",
            summary="这块要你看一下",
            message_id="fields-help",
        )
        cls.service.submit_artifact(
            action_id,
            actor_id=actors["同事甲"],
            message_id="fields-submit",
            payload={"summary": "初稿", "content": "三条结论"},
        )
        cls.state = workbench_state(
            cls.service,
            result_processing_mode="local",
            principal=Principal(
                actor_id=coordinator,
                episode_id=cls.service.episode_id,
                roles=frozenset({PrincipalRole.COORDINATOR}),
                auth_source="test",
                session_id="s",
            ),
        )
        cls.task = cls.state["tasks"][0]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()
        cls.directory.cleanup()

    def assertCarries(self, obj: dict, kind: str) -> None:
        missing = [name for name in READ_BY_PAGES[kind] if name not in obj]
        self.assertEqual(
            missing,
            [],
            f"the pages read these off a {kind} and the server does not send them",
        )

    def test_the_state_carries_what_the_pages_read(self) -> None:
        self.assertCarries(self.state, "state")

    def test_a_task_carries_what_the_pages_read(self) -> None:
        self.assertCarries(self.task, "task")

    def test_an_assignment_carries_what_the_pages_read(self) -> None:
        assignments = self.task["current_assignments"] or self.task["assignments"]
        self.assertTrue(assignments, "the fixture should have produced one")
        self.assertCarries(assignments[0], "assignment")

    def test_an_assistance_request_carries_what_the_pages_read(self) -> None:
        requests = self.task["assistance_requests"]
        self.assertTrue(requests, "the fixture should have produced one")
        self.assertCarries(requests[0], "assistance_request")

    def test_a_version_carries_what_the_pages_read(self) -> None:
        version = self.task["latest_version"]
        self.assertIsNotNone(version)
        self.assertCarries(version, "version")

    def test_a_partner_hint_reaches_the_people_working_together(self) -> None:
        """Not the coordinator: the projection strips it for them on purpose,
        so checking it there would assert the opposite of the rule."""

        owner = self.task["owner_actor_id"]
        state = workbench_state(
            self.service,
            result_processing_mode="local",
            principal=Principal(
                actor_id=owner,
                episode_id=self.service.episode_id,
                roles=frozenset({PrincipalRole.PARTICIPANT}),
                auth_source="test",
                session_id="s",
            ),
        )

        self.assertIn("collaboration_hints", state["tasks"][0])

    def test_an_activity_entry_carries_what_the_pages_read(self) -> None:
        self.assertTrue(self.task["activity"])
        self.assertCarries(self.task["activity"][0], "activity")


if __name__ == "__main__":
    unittest.main()
