from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from collab_agent.auth import (
    AuthorizationService,
    PrincipalError,
    PrincipalRole,
    VirtualSessionPrincipalProvider,
)
from collab_agent.meeting import load_meeting_service
from collab_agent.models import parse_time
from collab_agent.store import Database
from collab_agent.web import workbench_state


def _extraction() -> dict:
    return {
        "provider": "fixture",
        "model": "fixture",
        "input_sha256": "b" * 64,
        "action_items": [
            {
                "title": "准备会议成果",
                "deliverable": "一份成果说明",
                "owner_name": "参会者甲",
                "deadline_text": None,
                "deadline_iso": None,
                "source_timestamp": "00:00:10",
                "source_quote": "需要有人准备会议成果",
                "confidence": 0.9,
                "needs_confirmation": True,
                "uncertainties": ["负责人未明确"],
                "collaborator_names": [],
            }
        ],
    }


class VirtualSessionAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.extraction = root / "extraction.json"
        self.transcript = root / "transcript.txt"
        self.extraction.write_text(
            json.dumps(_extraction(), ensure_ascii=False), encoding="utf-8"
        )
        self.transcript.write_text(
            "主持人(00:00:10): 需要有人准备会议成果\n", encoding="utf-8"
        )
        self.db = Database(":memory:")
        self.db.initialize()
        self.service = load_meeting_service(
            self.db,
            extraction_path=self.extraction,
            transcript_path=self.transcript,
            organization_name="测试组织",
            coordinator_name="会议负责人",
            participant_names=["参会者甲", "参会者乙"],
        )
        self.participants = [
            row["actor_id"]
            for row in self.db.all(
                "SELECT ep.actor_id FROM episode_participants ep "
                "JOIN actors a ON a.actor_id = ep.actor_id "
                "WHERE ep.episode_id = ? AND ep.role = 'PARTICIPANT' "
                "ORDER BY a.display_name",
                (self.service.episode_id,),
            )
        ]
        self.provider = VirtualSessionPrincipalProvider(
            self.db,
            episode_id=self.service.episode_id,
            secret="test-virtual-session-secret-32-bytes",
        )
        self.authorization = AuthorizationService(
            self.db, episode_id=self.service.episode_id
        )

    def tearDown(self) -> None:
        self.db.close()
        self.directory.cleanup()

    def test_signed_session_maps_legacy_and_new_roles(self) -> None:
        coordinator = self.provider.issue(self.service.aggregator_actor_id)
        resolved_coordinator = self.provider.resolve(coordinator["token"])
        self.assertIn(PrincipalRole.COORDINATOR, resolved_coordinator.roles)
        participant = self.provider.issue(self.participants[0])
        resolved_participant = self.provider.resolve(participant["token"])
        self.assertEqual(
            resolved_participant.roles, frozenset({PrincipalRole.PARTICIPANT})
        )
        tampered = participant["token"][:-1] + (
            "A" if participant["token"][-1] != "A" else "B"
        )
        with self.assertRaises(PrincipalError):
            self.provider.resolve(tampered)

    def test_coordinator_in_explicit_roster_can_also_claim_work(self) -> None:
        database = Database(":memory:")
        database.initialize()
        try:
            service = load_meeting_service(
                database,
                extraction_path=self.extraction,
                transcript_path=self.transcript,
                organization_name="测试组织",
                coordinator_name="会议负责人",
                participant_names=["会议负责人", "参会者甲"],
            )
            provider = VirtualSessionPrincipalProvider(
                database,
                episode_id=service.episode_id,
                secret="test-virtual-session-secret-32-bytes",
            )
            principal = provider.resolve(
                provider.issue(service.aggregator_actor_id)["token"]
            )
            self.assertEqual(
                principal.roles,
                frozenset(
                    {PrincipalRole.COORDINATOR, PrincipalRole.PARTICIPANT}
                ),
            )
            action = next(iter(service.action_items()))
            team_required_by = (
                parse_time(service.now()) + timedelta(days=3)
            ).isoformat()
            service.revise_action_proposal(
                action["action_item_id"],
                actor_id=service.aggregator_actor_id,
                title=action["title"],
                deliverable="成果说明",
                acceptance_criteria="管理侧验收规则",
                priority="P0",
                message_id="dual-role-revise",
                team_required_by_sim_time=team_required_by,
            )
            service.publish_action(
                action["action_item_id"],
                actor_id=service.aggregator_actor_id,
                message_id="dual-role-publish",
            )
            claimed = service.claim_action(
                action["action_item_id"],
                actor_id=service.aggregator_actor_id,
                promised_deadline_sim_time=(
                    parse_time(service.now()) + timedelta(days=2)
                ).isoformat(),
                message_id="dual-role-claim",
            )
            self.assertEqual(claimed["status"], "TRACKING")
        finally:
            database.close()

    def test_extracted_owner_is_only_a_coordinator_hint(self) -> None:
        action = next(iter(self.service.action_items()))
        metadata = self.service.proposal_metadata(action)
        self.assertEqual(metadata["suggested_owner_name"], "参会者甲")
        self.assertIsNone(action["owner_actor_id"])

    def test_existing_meeting_cannot_silently_change_participant_roster(self) -> None:
        repeated = load_meeting_service(
            self.db,
            extraction_path=self.extraction,
            transcript_path=self.transcript,
            organization_name="测试组织",
            coordinator_name="会议负责人",
            participant_names=["参会者乙", "参会者甲"],
        )
        self.assertEqual(repeated.episode_id, self.service.episode_id)
        with self.assertRaisesRegex(ValueError, "different participant roster"):
            load_meeting_service(
                self.db,
                extraction_path=self.extraction,
                transcript_path=self.transcript,
                organization_name="测试组织",
                coordinator_name="会议负责人",
                participant_names=["参会者甲", "未参会者"],
            )

    def test_existing_meeting_backfills_minimal_collaboration_policy(self) -> None:
        action = next(iter(self.service.action_items()))
        metadata = self.service.proposal_metadata(action)
        for key in (
            "suggested_collaborator_names",
            "collaboration_mode",
            "collaborator_names",
            "collaborator_actor_ids",
        ):
            metadata.pop(key, None)
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE action_items SET proposal_metadata = ? "
                "WHERE action_item_id = ?",
                (json.dumps(metadata, ensure_ascii=False), action["action_item_id"]),
            )
        repeated = load_meeting_service(
            self.db,
            extraction_path=self.extraction,
            transcript_path=self.transcript,
            organization_name="测试组织",
            coordinator_name="会议负责人",
            participant_names=["参会者甲", "参会者乙"],
        )
        refreshed_action = next(
            item
            for item in repeated.action_items()
            if item["action_item_id"] == action["action_item_id"]
        )
        refreshed = repeated.proposal_metadata(refreshed_action)
        self.assertEqual(refreshed["collaboration_mode"], "SOLO")
        self.assertEqual(refreshed["collaborator_names"], [])
        audit = self.db.one(
            "SELECT event_type FROM audit_events WHERE aggregate_id = ? "
            "AND event_type = 'ActionItemCollaborationPolicyBackfilled'",
            (action["action_item_id"],),
        )
        self.assertIsNotNone(audit)

    def test_explicit_meeting_collaborator_is_mapped_without_coownership(self) -> None:
        payload = _extraction()
        payload["input_sha256"] = "c" * 64
        item = payload["action_items"][0]
        item["source_quote"] = "参会者甲负责整理，参会者乙配合参会者甲一起完成"
        item["collaborator_names"] = ["参会者乙"]
        self.extraction.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        self.transcript.write_text(
            "会议负责人(00:00:10): 参会者甲负责整理，参会者乙配合参会者甲一起完成\n",
            encoding="utf-8",
        )
        database = Database(":memory:")
        database.initialize()
        try:
            service = load_meeting_service(
                database,
                extraction_path=self.extraction,
                transcript_path=self.transcript,
                organization_name="测试组织",
                coordinator_name="会议负责人",
                participant_names=["参会者甲", "参会者乙"],
            )
            action = next(iter(service.action_items()))
            metadata = service.proposal_metadata(action)
            self.assertEqual(metadata["collaboration_mode"], "RECORDED_COLLABORATION")
            self.assertEqual(metadata["collaborator_names"], ["参会者乙"])
            self.assertEqual(len(metadata["collaborator_actor_ids"]), 1)
            self.assertIsNone(action["owner_actor_id"])
            deadline = (
                parse_time(service.now()) + timedelta(days=2)
            ).isoformat()
            service.revise_action_proposal(
                action["action_item_id"],
                actor_id=service.aggregator_actor_id,
                title=action["title"],
                deliverable="协作成果",
                acceptance_criteria="负责人验收",
                priority="P0",
                team_required_by_sim_time=deadline,
                message_id="explicit-collab-revise",
            )
            service.publish_action(
                action["action_item_id"],
                actor_id=service.aggregator_actor_id,
                message_id="explicit-collab-publish",
            )
            owner = database.one(
                "SELECT actor_id FROM actors WHERE display_name = '参会者甲'"
            )["actor_id"]
            collaborator = metadata["collaborator_actor_ids"][0]
            service.claim_action(
                action["action_item_id"],
                actor_id=owner,
                promised_deadline_sim_time=deadline,
                message_id="explicit-collab-claim",
            )
            signal = service.record_progress_signal(
                action["action_item_id"],
                actor_id=collaborator,
                signal_type="ON_TRACK",
                message_id="explicit-collab-signal",
            )
            self.assertEqual(signal["contributor_role"], "MEETING_COLLABORATOR")
            delivery = service.submit_artifact(
                action["action_item_id"],
                actor_id=collaborator,
                message_id="explicit-collab-delivery",
                payload={
                    "summary": "共同成果已完成",
                    "content": "由会议指定协作者提交。",
                    "links": [],
                    "files": [],
                },
            )
            submitted_by = database.one(
                "SELECT submitted_by_actor_id, review_status FROM artifact_versions "
                "WHERE version_id = ?",
                (delivery["version_id"],),
            )
            self.assertEqual(submitted_by["submitted_by_actor_id"], collaborator)
            self.assertEqual(submitted_by["review_status"], "NOT_REQUIRED")
            self.assertEqual(delivery["submission_kind"], "CONTRIBUTION")
            self.assertEqual(service.action(action["action_item_id"])["status"], "TRACKING")
            included = service.decide_contribution(
                delivery["version_id"],
                actor_id=owner,
                action="INCLUDE",
                comment="纳入负责人后续整理",
                message_id="explicit-collab-include",
            )
            self.assertEqual(included["contribution_status"], "INCLUDED")
            self.assertEqual(included["action_status"], "TRACKING")
            included_version = database.one(
                "SELECT processing_status FROM artifact_versions WHERE version_id = ?",
                (delivery["version_id"],),
            )
            self.assertEqual(included_version["processing_status"], "NOT_REQUIRED")
            self.assertIsNone(service.process_task_result_once(processing_mode="local"))
        finally:
            database.close()

    def test_nonparticipant_cannot_receive_a_session_or_claim(self) -> None:
        with self.assertRaises(PrincipalError):
            self.provider.issue("actor_not_in_meeting")
        action = next(iter(self.service.action_items()))
        self.service.revise_action_proposal(
            action["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            title=action["title"],
            deliverable="成果说明",
            acceptance_criteria="管理侧验收规则",
            priority="P0",
            message_id="auth-revise",
            team_required_by_sim_time=(
                parse_time(self.service.now()) + timedelta(days=3)
            ).isoformat(),
        )
        self.service.publish_action(
            action["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            message_id="auth-publish",
        )
        promised_by = (
            parse_time(self.service.now()) + timedelta(days=2)
        ).isoformat()
        with self.assertRaises(PermissionError):
            self.service.claim_action(
                action["action_item_id"],
                actor_id="actor_not_in_meeting",
                promised_deadline_sim_time=promised_by,
                message_id="auth-invalid-claim",
            )
        with self.assertRaisesRegex(PermissionError, "explicit meeting participant"):
            self.service.claim_action(
                action["action_item_id"],
                claimant_name="正文里出现但未列入参会名单的人",
                promised_deadline_sim_time=promised_by,
                message_id="auth-unlisted-name-claim",
            )

    def test_meeting_import_requires_an_explicit_participant_roster(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit meeting participant"):
            load_meeting_service(
                self.db,
                extraction_path=self.extraction,
                transcript_path=self.transcript,
                organization_name="测试组织",
                coordinator_name="会议负责人",
            )

    def test_participant_projection_hides_management_and_other_delivery(self) -> None:
        action = next(iter(self.service.action_items()))
        self.service.revise_action_proposal(
            action["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            title=action["title"],
            deliverable="成果说明",
            acceptance_criteria="只有会议负责人可见",
            priority="P0",
            message_id="projection-revise",
            team_required_by_sim_time=(
                parse_time(self.service.now()) + timedelta(days=3)
            ).isoformat(),
        )
        self.service.publish_action(
            action["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            message_id="projection-publish",
        )
        promised_by = (
            parse_time(self.service.now()) + timedelta(days=2)
        ).isoformat()
        self.service.claim_action(
            action["action_item_id"],
            actor_id=self.participants[0],
            promised_deadline_sim_time=promised_by,
            message_id="projection-claim",
        )
        self.service.submit_artifact(
            action["action_item_id"],
            actor_id=self.participants[0],
            message_id="projection-submit",
            payload={"summary": "私有提交", "content": "不可向其他参会者公开"},
        )

        owner_principal = self.provider.resolve(
            self.provider.issue(self.participants[0])["token"]
        )
        other_principal = self.provider.resolve(
            self.provider.issue(self.participants[1])["token"]
        )
        owner_state = workbench_state(self.service, principal=owner_principal)
        other_state = workbench_state(self.service, principal=other_principal)
        owner_task = owner_state["tasks"][0]
        self.assertTrue(owner_task["is_mine"])
        self.assertEqual(owner_task["latest_version"]["payload"]["summary"], "私有提交")
        self.assertEqual(other_state["tasks"], [])
        self.assertEqual(other_state["meeting_progress"]["total"], 1)
        self.assertEqual(other_state["pending_approvals"], [])
        self.assertIsNone(other_state["report"])
