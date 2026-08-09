from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from collab_agent.auth import VirtualSessionPrincipalProvider
from collab_agent.memory_lexicon import memory_value
from collab_agent.meeting import load_meeting_service
from collab_agent.models import canonical_json, parse_time
from collab_agent.store import Database
from collab_agent.web import workbench_state


def extraction_payload() -> dict:
    return {
        "provider": "fixture",
        "model": "deterministic",
        "input_sha256": "d" * 64,
        "action_items": [
            {
                "title": "整理发布计划",
                "deliverable": "发布计划",
                "owner_name": None,
                "deadline_text": None,
                "deadline_iso": None,
                "source_timestamp": "00:01:00",
                "source_quote": "需要整理发布计划",
                "confidence": 0.95,
                "needs_confirmation": True,
                "uncertainties": ["负责人未明确"],
            }
        ],
    }


class AssignmentFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        extraction = root / "extraction.json"
        transcript = root / "transcript.txt"
        extraction.write_text(
            json.dumps(extraction_payload(), ensure_ascii=False), encoding="utf-8"
        )
        transcript.write_text(
            "主持人(00:01:00): 需要整理发布计划\n", encoding="utf-8"
        )
        self.database = Database(":memory:")
        self.database.initialize()
        self.service = load_meeting_service(
            self.database,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="测试团队",
            coordinator_name="协调人",
            participant_names=["主负责人", "协作者", "旁观参会者"],
        )
        self.action = dict(
            self.database.one(
                "SELECT * FROM action_items WHERE episode_id = ?",
                (self.service.episode_id,),
            )
        )
        self.coordinator_id = self.service.aggregator_actor_id
        self.owner_id = self._actor_id("主负责人")
        self.collaborator_id = self._actor_id("协作者")
        self.other_id = self._actor_id("旁观参会者")
        metadata = self.service.proposal_metadata(self.action)
        self.team_required_by = (
            parse_time(self.service.now()) + timedelta(days=3)
        ).isoformat()
        self.service.revise_action_proposal(
            self.action["action_item_id"],
            actor_id=self.coordinator_id,
            title=self.action["title"],
            deliverable=metadata["deliverable"],
            acceptance_criteria=metadata.get("acceptance_criteria", ""),
            work_requirements="提交可执行的发布计划",
            management_review_policy="包含时间和责任人",
            priority="P0",
            team_required_by_sim_time=self.team_required_by,
            message_id="revise-v1",
        )

    def tearDown(self) -> None:
        self.database.close()
        self.directory.cleanup()

    def _actor_id(self, display_name: str) -> str:
        row = self.database.one(
            "SELECT actor_id FROM actors WHERE display_name = ?", (display_name,)
        )
        assert row
        return row["actor_id"]

    def _dispatch(self, message_id: str = "dispatch-v1") -> dict:
        return self.service.dispatch_action(
            self.action["action_item_id"],
            actor_id=self.coordinator_id,
            owner_actor_id=self.owner_id,
            collaborator_actor_ids=[self.collaborator_id],
            assignment_message="请共同完成，主负责人整理终稿",
            message_id=message_id,
        )

    def test_all_recipients_must_accept_before_tracking(self) -> None:
        dispatched = self._dispatch()
        self.assertEqual(dispatched["status"], "PENDING_ASSIGNMENT")
        self.assertEqual(len(dispatched["assignments"]), 2)
        action = self.database.one(
            "SELECT * FROM action_items WHERE action_item_id = ?",
            (self.action["action_item_id"],),
        )
        self.assertIsNone(action["owner_actor_id"])
        self.assertIsNone(
            self.service.task_contributor_role(action, self.collaborator_id)
        )
        with self.assertRaises(PermissionError):
            self.service.respond_to_assignment(
                self.action["action_item_id"],
                actor_id=self.other_id,
                decision="ACCEPT",
                response_message="",
                message_id="unassigned-accept",
            )

        first = self.service.respond_to_assignment(
            self.action["action_item_id"],
            actor_id=self.owner_id,
            decision="ACCEPT",
            response_message="收到",
            message_id="owner-accept",
        )
        self.assertEqual(first["status"], "PENDING_ASSIGNMENT")
        self.assertEqual(first["pending_count"], 1)
        activated = self.service.respond_to_assignment(
            self.action["action_item_id"],
            actor_id=self.collaborator_id,
            decision="ACCEPT",
            response_message="可以配合",
            message_id="collaborator-accept",
        )
        self.assertEqual(activated["status"], "TRACKING")
        self.assertEqual(activated["owner_actor_id"], self.owner_id)
        self.assertEqual(activated["promised_deadline_sim_time"], self.team_required_by)
        action = self.database.one(
            "SELECT * FROM action_items WHERE action_item_id = ?",
            (self.action["action_item_id"],),
        )
        self.assertEqual(
            self.service.task_contributor_role(action, self.collaborator_id),
            "MEETING_COLLABORATOR",
        )
        self.assertIsNotNone(action["active_commitment_revision_id"])

    def test_one_return_supersedes_the_whole_round_and_requires_everyone_again(self) -> None:
        self._dispatch()
        self.service.respond_to_assignment(
            self.action["action_item_id"],
            actor_id=self.owner_id,
            decision="ACCEPT",
            response_message="收到",
            message_id="owner-accept-before-return",
        )
        returned = self.service.respond_to_assignment(
            self.action["action_item_id"],
            actor_id=self.collaborator_id,
            decision="RETURN_FOR_REVISION",
            response_message="交付范围不清楚，请补充渠道和格式",
            message_id="collaborator-return",
        )
        self.assertEqual(returned["status"], "NEEDS_REVISION")
        first_round = self.database.all(
            "SELECT actor_id, response_status FROM action_item_assignments "
            "WHERE action_item_id = ? AND definition_version = 1 ORDER BY actor_id",
            (self.action["action_item_id"],),
        )
        statuses = {row["actor_id"]: row["response_status"] for row in first_round}
        self.assertEqual(statuses[self.owner_id], "SUPERSEDED")
        self.assertEqual(statuses[self.collaborator_id], "RETURNED")

        current = self.database.one(
            "SELECT * FROM action_items WHERE action_item_id = ?",
            (self.action["action_item_id"],),
        )
        metadata = self.service.proposal_metadata(current)
        self.service.revise_action_proposal(
            self.action["action_item_id"],
            actor_id=self.coordinator_id,
            title=current["title"],
            deliverable=metadata["deliverable"],
            acceptance_criteria=metadata.get("acceptance_criteria", ""),
            work_requirements="覆盖公众号和视频号，提交 Markdown 表格",
            management_review_policy="包含渠道、时间和责任人",
            priority="P0",
            team_required_by_sim_time=self.team_required_by,
            message_id="revise-v2",
        )
        redispatched = self._dispatch(message_id="dispatch-v2")
        self.assertEqual(redispatched["definition_version"], 2)
        second_round = self.database.all(
            "SELECT response_status FROM action_item_assignments "
            "WHERE action_item_id = ? AND definition_version = 2",
            (self.action["action_item_id"],),
        )
        self.assertEqual([row["response_status"] for row in second_round], ["PENDING", "PENDING"])
        audit_types = {
            row["event_type"]
            for row in self.database.all(
                "SELECT event_type FROM audit_events WHERE aggregate_id = ?",
                (self.action["action_item_id"],),
            )
        }
        self.assertIn("ActionItemDispatchSuperseded", audit_types)
        self.assertIn("ActionItemDispatchedAgain", audit_types)

    def test_confirmed_memory_is_shared_only_as_a_minimal_active_partner_hint(self) -> None:
        self._dispatch()
        for actor_id, message_id in (
            (self.owner_id, "memory-owner-accept"),
            (self.collaborator_id, "memory-collaborator-accept"),
        ):
            self.service.respond_to_assignment(
                self.action["action_item_id"],
                actor_id=actor_id,
                decision="ACCEPT",
                response_message="",
                message_id=message_id,
            )
        now = self.service.now()
        with self.database.transaction() as cursor:
            cursor.execute(
                "INSERT INTO collaboration_memories(memory_id, actor_id, topic, "
                "value, visibility, status, evidence_refs, created_sim_time, "
                "version, confirmed_by, confirmed_sim_time) "
                "VALUES ('memory_partner', ?, 'UPDATE_STYLE', ?, 'PRIVATE', "
                "'CONFIRMED', ?, ?, 1, ?, ?)",
                (
                    self.collaborator_id,
                    canonical_json(memory_value("UPDATE_STYLE", "RISK_FIRST")),
                    canonical_json(["event:private-evidence"]),
                    now,
                    self.collaborator_id,
                    now,
                ),
            )

        built = self.service.build_collaboration_hint_context(
            self.action["action_item_id"],
            actor_id=self.owner_id,
            subject_actor_id=self.collaborator_id,
            message_id="partner-hint",
        )
        hint = built["context"]["confirmed_memories"][0]
        # What a partner receives is the instruction, not the self-description:
        # "they tell me about risk first" is a fact about a person, while
        # "when they speak up there is usually something to handle" is
        # something the reader can act on.
        self.assertEqual(
            hint["collaborator_hint"],
            memory_value("PROGRESS_SIGNAL", "RISK_FIRST")["collaborator_hint"],
        )
        self.assertNotIn(
            "statement", hint, "the first-person sentence stays with its author"
        )
        self.assertNotIn("evidence_refs", hint)

        provider = VirtualSessionPrincipalProvider(
            self.database,
            episode_id=self.service.episode_id,
            secret="assignment-memory-test-secret-32-bytes",
        )
        owner_principal = provider.resolve(provider.issue(self.owner_id)["token"])
        coordinator_principal = provider.resolve(
            provider.issue(self.coordinator_id)["token"]
        )
        owner_state = workbench_state(self.service, principal=owner_principal)
        self.assertEqual(
            owner_state["tasks"][0]["collaboration_hints"][0]["collaborator_hint"],
            memory_value("PROGRESS_SIGNAL", "RISK_FIRST")["collaborator_hint"],
        )
        self.assertNotIn(
            "collaboration_hints",
            workbench_state(
                self.service, principal=coordinator_principal
            )["tasks"][0],
        )


if __name__ == "__main__":
    unittest.main()
