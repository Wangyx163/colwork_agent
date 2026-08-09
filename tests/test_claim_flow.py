from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from collab_agent.meeting import load_meeting_service
from collab_agent.models import parse_time
from collab_agent.result_processing import build_deterministic_report
from collab_agent.service import CoordinationService
from collab_agent.store import Database
from collab_agent.web import SingleInstanceHTTPServer, workbench_state

WEB_SRC = Path(__file__).resolve().parents[1] / "web" / "src"


def extraction_payload() -> dict:
    return {
        "provider": "bailian",
        "model": "qwen-plus",
        "input_sha256": "a" * 64,
        "action_items": [
            {
                "title": "调研平台机制",
                "deliverable": "平台机制调研结果",
                "owner_name": None,
                "deadline_text": None,
                "deadline_iso": None,
                "source_timestamp": "00:01:00",
                "source_quote": "需要有人调研平台机制",
                "confidence": 0.91,
                "needs_confirmation": True,
                "uncertainties": ["负责人未明确", "截止时间未明确"],
            },
            {
                "title": "撰写视频脚本",
                "deliverable": "一份视频脚本",
                "owner_name": None,
                "deadline_text": None,
                "deadline_iso": None,
                "source_timestamp": "00:02:00",
                "source_quote": "我们把视频脚本写一写",
                "confidence": 0.94,
                "needs_confirmation": True,
                "uncertainties": ["负责人未明确", "截止时间未明确"],
            },
        ],
    }


class ClaimFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.extraction = root / "extraction.json"
        self.transcript = root / "transcript.txt"
        self.extraction.write_text(
            json.dumps(extraction_payload(), ensure_ascii=False), encoding="utf-8"
        )
        self.transcript.write_text(
            "主持人(00:01:00): 需要有人调研平台机制\n"
            "主持人(00:02:00): 我们把视频脚本写一写\n",
            encoding="utf-8",
        )
        self.database = Database(":memory:")
        self.database.initialize()
        self.service = load_meeting_service(
            self.database,
            extraction_path=self.extraction,
            transcript_path=self.transcript,
            organization_name="测试团队",
            coordinator_name="协调人",
            participant_names=["王同事", "另一位同事", "工期测试同事"],
        )

    def tearDown(self) -> None:
        self.database.close()
        self.directory.cleanup()

    def set_team_required(self, action: dict, message_id: str) -> None:
        metadata = self.service.proposal_metadata(action)
        self.service.revise_action_proposal(
            action["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            title=action["title"],
            deliverable=metadata.get("deliverable", "会议交付"),
            acceptance_criteria=metadata.get("acceptance_criteria", ""),
            priority=metadata.get("priority", "P1"),
            message_id=message_id,
            team_required_by_sim_time=(
                parse_time(self.service.now()) + timedelta(days=3)
            ).isoformat(),
        )

    def test_the_participant_page_never_polls(self) -> None:
        """Refreshing on a timer is not the same as reflecting an action.

        A page that re-fetches on its own hides whether an action landed, and
        on a simulated clock it also makes the audit trail look like the user
        did something. State moves only after an action the person took. The
        assertion followed the page from the server-rendered template into the
        React sources rather than being dropped with it.
        """

        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (WEB_SRC.rglob("*.tsx"))
        )

        self.assertNotIn("setInterval", sources)
        self.assertNotIn("setTimeout(load", sources)

    def test_a_half_written_submission_survives_a_reload(self) -> None:
        """The old page kept form drafts; losing that would be a regression."""

        draft = (WEB_SRC / "tasks" / "useDraft.ts").read_text(encoding="utf-8")
        card = (WEB_SRC / "tasks" / "MyTaskCard.tsx").read_text(encoding="utf-8")

        self.assertIn("localStorage", draft)
        self.assertIn("useDraft", card)

    def test_workbench_port_allows_only_one_server_instance(self) -> None:
        first = SingleInstanceHTTPServer(
            ("127.0.0.1", 0), BaseHTTPRequestHandler
        )
        try:
            port = int(first.server_address[1])
            with self.assertRaises(OSError):
                second = SingleInstanceHTTPServer(
                    ("127.0.0.1", port), BaseHTTPRequestHandler
                )
                second.server_close()
        finally:
            first.server_close()

    def test_import_reuses_action_items_instead_of_candidate_entity(self) -> None:
        state = workbench_state(self.service)
        self.assertEqual(len(state["tasks"]), 2)
        self.assertTrue(all("claimable" not in task for task in state["tasks"]))
        self.assertTrue(
            all(task["published_sim_time"] is None for task in state["tasks"])
        )
        self.assertIsNone(
            self.database.one(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'action_candidates'"
            )
        )
        activated = self.database.one(
            "SELECT payload FROM audit_events WHERE event_type = 'EpisodeActivated' "
            "ORDER BY sequence_no DESC LIMIT 1"
        )
        extraction_invocation = json.loads(activated["payload"])[
            "extraction_invocation"
        ]
        self.assertEqual(
            extraction_invocation["principal"]["episode_id"],
            self.service.episode_id,
        )
        self.assertEqual(
            extraction_invocation["purpose"], "ACTION_ITEM_EXTRACTION"
        )
        self.assertEqual(extraction_invocation["output_status"], "IMPORTED")

    def test_claim_is_atomic_idempotent_and_enters_tracking(self) -> None:
        action = next(iter(self.service.action_items()))
        self.set_team_required(action, "prepare-team-1")
        self.service.publish_action(
            action["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            message_id="publish-1",
        )
        deadline = (parse_time(self.service.now()) + timedelta(days=3)).isoformat()
        first = self.service.claim_action(
            action["action_item_id"],
            claimant_name="王同事",
            promised_deadline_sim_time=deadline,
            message_id="claim-1",
        )
        repeated = self.service.claim_action(
            action["action_item_id"],
            claimant_name="王同事",
            promised_deadline_sim_time=deadline,
            message_id="claim-1",
        )
        self.assertEqual(first, repeated)
        claimed = self.service.action(action["action_item_id"])
        self.assertEqual(claimed["status"], "TRACKING")
        self.assertIsNotNone(claimed["owner_actor_id"])
        self.assertEqual(
            self.database.one(
                "SELECT COUNT(*) AS count FROM commitment_revisions "
                "WHERE action_item_id = ?",
                (action["action_item_id"],),
            )["count"],
            1,
        )
        with self.assertRaisesRegex(ValueError, "already been claimed"):
            self.service.claim_action(
                action["action_item_id"],
                claimant_name="另一位同事",
                promised_deadline_sim_time=deadline,
                message_id="claim-2",
            )

    def test_team_need_by_and_personal_promise_are_independent(self) -> None:
        action = next(iter(self.service.action_items()))
        team_required_by = (
            parse_time(self.service.now()) + timedelta(days=2)
        ).isoformat()
        metadata = self.service.proposal_metadata(action)
        self.service.revise_action_proposal(
            action["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            title=action["title"],
            deliverable=metadata["deliverable"],
            acceptance_criteria="负责人验收规则",
            priority="P0",
            message_id="schedule-prepare",
            team_required_by_sim_time=team_required_by,
        )
        self.service.publish_action(
            action["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            message_id="schedule-publish",
        )
        initial_promise = (
            parse_time(self.service.now()) + timedelta(days=1)
        ).isoformat()
        claim = self.service.claim_action(
            action["action_item_id"],
            claimant_name="工期测试同事",
            promised_deadline_sim_time=initial_promise,
            message_id="schedule-claim",
        )
        self.assertFalse(claim["schedule_conflict"])
        late_promise = (
            parse_time(self.service.now()) + timedelta(days=3)
        ).isoformat()
        late_revision = self.service.revise_personal_commitment(
            action["action_item_id"],
            actor_id=claim["owner_actor_id"],
            proposed_deadline_sim_time=late_promise,
            reason="真实个人承诺晚于团队需要时间",
            message_id="schedule-late",
        )
        self.assertTrue(late_revision["schedule_conflict"])
        stored = self.service.action(action["action_item_id"])
        self.assertEqual(stored["team_required_by_sim_time"], team_required_by)
        self.assertEqual(stored["deadline_sim_time"], late_promise)
        recovered_promise = (
            parse_time(self.service.now()) + timedelta(days=1, hours=12)
        ).isoformat()
        recovered = self.service.revise_personal_commitment(
            action["action_item_id"],
            actor_id=claim["owner_actor_id"],
            proposed_deadline_sim_time=recovered_promise,
            reason="重新安排后可满足团队时间",
            message_id="schedule-resolved",
        )
        self.assertFalse(recovered["schedule_conflict"])
        event_types = [event["event_type"] for event in self.service.audit_events()]
        self.assertIn("ScheduleConflictDetected", event_types)
        self.assertIn("ScheduleConflictResolved", event_types)
        self.assertIsNone(self.service.pending_approval("DEADLINE_CHANGE"))

    def test_review_progress_delivery_and_human_acceptance(self) -> None:
        action = next(iter(self.service.action_items()))
        original_source = action["source_span"]
        self.service.revise_action_proposal(
            action["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            title="调研推荐机制",
            deliverable="推荐机制调研说明",
            acceptance_criteria="说明主要影响因素并附来源链接",
            priority="P0",
            message_id="revise-1",
            team_required_by_sim_time=(
                parse_time(self.service.now()) + timedelta(days=3)
            ).isoformat(),
        )
        revised = self.service.action(action["action_item_id"])
        self.assertEqual(revised["title"], "调研推荐机制")
        self.assertEqual(revised["source_span"], original_source)
        self.service.publish_action(
            action["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            message_id="publish-2",
        )
        deadline = (parse_time(self.service.now()) + timedelta(days=3)).isoformat()
        claim = self.service.claim_action(
            action["action_item_id"],
            claimant_name="王同事",
            promised_deadline_sim_time=deadline,
            message_id="claim-3",
        )
        revised_deadline = (
            parse_time(self.service.now()) + timedelta(days=4)
        ).isoformat()
        deadline_revision = self.service.revise_personal_commitment(
            action["action_item_id"],
            actor_id=claim["owner_actor_id"],
            proposed_deadline_sim_time=revised_deadline,
            reason="需要等待平台访问权限",
            message_id="deadline-request-1",
        )
        self.assertEqual(deadline_revision["status"], "ACTIVE")
        self.assertTrue(deadline_revision["schedule_conflict"])
        self.assertEqual(
            self.database.one(
                "SELECT COUNT(*) AS count FROM approvals "
                "WHERE approval_type = 'DEADLINE_CHANGE'"
            )["count"],
            0,
        )
        self.assertEqual(
            self.service.action(action["action_item_id"])["deadline_sim_time"],
            revised_deadline,
        )
        self.assertEqual(
            self.database.one(
                "SELECT COUNT(*) AS count FROM commitment_revisions "
                "WHERE action_item_id = ?",
                (action["action_item_id"],),
            )["count"],
            2,
        )
        self.service.update_progress(
            action["action_item_id"],
            actor_id=claim["owner_actor_id"],
            progress_summary="已经完成资料收集",
            blocked=True,
            blocker_reason="还缺一份平台说明",
            help_needed="请协调访问权限",
            next_step="明天整理结论",
            message_id="progress-1",
        )
        invalid = self.service.submit_artifact(
            action["action_item_id"],
            actor_id=claim["owner_actor_id"],
            message_id="delivery-invalid",
            payload={"summary": ""},
        )
        self.assertEqual(invalid["validation_status"], "FAILED")
        self.assertEqual(
            invalid["missing_fields"], ["summary", "content_or_link_or_file"]
        )
        invalid_state = next(
            task
            for task in workbench_state(self.service)["tasks"]
            if task["action_item_id"] == action["action_item_id"]
        )
        self.assertEqual(
            invalid_state["latest_version"]["validation_errors"]["missing_fields"],
            ["summary", "content_or_link_or_file"],
        )
        self.assertEqual(invalid_state["status"], "TRACKING")
        submitted = self.service.submit_artifact(
            action["action_item_id"],
            actor_id=claim["owner_actor_id"],
            message_id="delivery-1",
            payload={
                "summary": "已经完成推荐机制调研",
                "links": ["https://example.com/evidence"],
                "content": "主要结论",
            },
        )
        self.assertEqual(submitted["review_status"], "PENDING")
        self.assertEqual(
            self.service.action(action["action_item_id"])["status"],
            "PENDING_ACCEPTANCE",
        )
        self.service.process_task_result_once(processing_mode="local")
        reviewed = self.service.review_artifact(
            submitted["version_id"],
            actor_id=self.service.aggregator_actor_id,
            approve=True,
            comment="符合验收要求",
            message_id="review-1",
        )
        self.assertEqual(reviewed["action_status"], "ACCEPTED")
        self.assertEqual(
            self.service.action(action["action_item_id"])["current_valid_version_id"],
            submitted["version_id"],
        )
        reloaded_service = CoordinationService(
            self.database,
            self.service.fixture,
            episode_id=self.service.episode_id,
            run_id=self.service.run_id,
        )
        activity = next(
            task["activity"]
            for task in workbench_state(reloaded_service)["tasks"]
            if task["action_item_id"] == action["action_item_id"]
        )
        self.assertTrue(
            {"COMMITMENT", "PROGRESS", "DELIVERY"}
            <= {item["kind"] for item in activity}
        )
        self.assertFalse(any(item["kind"] == "APPROVAL" for item in activity))
        accepted_delivery = next(
            item
            for item in activity
            if item["kind"] == "DELIVERY" and item["status"] == "ACCEPTED"
        )
        self.assertIn("符合验收要求", accepted_delivery["detail"])
        accepted_task = next(
            task
            for task in workbench_state(reloaded_service)["tasks"]
            if task["action_item_id"] == action["action_item_id"]
        )
        self.assertEqual(
            accepted_task["current_version"]["version_id"], submitted["version_id"]
        )

    def test_pending_final_can_be_reprocessed_without_losing_history(self) -> None:
        kept, ignored = list(self.service.action_items())
        self.service.ignore_action(
            ignored["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            reason="本次不需要",
            message_id="ignore-reprocess",
        )
        self.set_team_required(kept, "prepare-team-reprocess")
        self.service.publish_action(
            kept["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            message_id="publish-reprocess",
        )
        claim = self.service.claim_action(
            kept["action_item_id"],
            claimant_name="王同事",
            promised_deadline_sim_time=(
                parse_time(self.service.now()) + timedelta(days=1)
            ).isoformat(),
            message_id="claim-reprocess",
        )
        version = self.service.submit_artifact(
            kept["action_item_id"],
            actor_id=claim["owner_actor_id"],
            message_id="delivery-reprocess",
            payload={"summary": "调研完成", "content": "初稿正文"},
        )
        self.service.process_task_result_once(processing_mode="local")
        self.service.review_artifact(
            version["version_id"],
            actor_id=self.service.aggregator_actor_id,
            approve=True,
            comment="通过",
            message_id="review-reprocess",
        )
        first_final = self.service.aggregate()
        first_approval = self.database.one(
            "SELECT approval_id FROM approvals WHERE approval_type = 'FINAL_RELEASE' "
            "AND status = 'PENDING'"
        )["approval_id"]

        processing_outbox = self.service.queue_final_organization(
            processing_mode="bailian"
        )
        self.assertIsNotNone(processing_outbox)

        def unavailable_organizer(source_bundle: list[dict]) -> tuple[dict, dict]:
            raise RuntimeError("provider temporarily unavailable")

        retry_result = self.service.dispatch_final_organization_once(
            session_id="test-result-worker",
            organizer=unavailable_organizer,
        )
        self.assertEqual(retry_result["status"], "RETRY_WAIT")
        retry_event = self.database.one(
            "SELECT payload FROM audit_events "
            "WHERE event_type = 'FinalOrganizationRetryScheduled' "
            "ORDER BY sequence_no DESC LIMIT 1"
        )
        retry_invocation = json.loads(retry_event["payload"])["invocation"]
        self.assertEqual(retry_invocation["principal"]["actor_id"], "SYSTEM")
        self.assertEqual(retry_invocation["purpose"], "FINAL_ORGANIZATION")
        self.assertEqual(retry_invocation["output_status"], "RETRY_WAIT")
        self.assertEqual(
            self.database.one(
                "SELECT current_final_deliverable_id FROM episodes "
                "WHERE episode_id = ?",
                (self.service.episode_id,),
            )["current_final_deliverable_id"],
            first_final,
        )

        def fake_organizer(source_bundle: list[dict]) -> tuple[dict, dict]:
            return build_deterministic_report(source_bundle), {
                "mode": "bailian",
                "provider": "test",
                "model": "test-model",
                "prompt_version": "final-organization.v2",
                "input_hash": "test-input",
            }

        processing_result = self.service.dispatch_final_organization_once(
            session_id="test-result-worker",
            organizer=fake_organizer,
        )
        second_final = processing_result["final_deliverable_id"]

        self.assertNotEqual(first_final, second_final)
        self.assertEqual(processing_result["status"], "DELIVERED")
        success_event = self.database.one(
            "SELECT payload FROM audit_events "
            "WHERE event_type = 'FinalOrganizationSucceeded' "
            "ORDER BY sequence_no DESC LIMIT 1"
        )
        success_invocation = json.loads(success_event["payload"])["invocation"]
        stored_second_payload = json.loads(
            self.database.one(
                "SELECT payload FROM final_deliverables "
                "WHERE final_deliverable_id = ?",
                (second_final,),
            )["payload"]
        )
        self.assertEqual(success_invocation["output_status"], "SUCCEEDED")
        self.assertEqual(success_invocation["provider"], "test")
        self.assertEqual(
            success_invocation["input_hash"],
            stored_second_payload["processing"]["input_hash"],
        )
        self.assertNotEqual(
            stored_second_payload["processing"]["input_hash"], "test-input"
        )
        self.assertEqual(
            self.database.one(
                "SELECT status FROM final_deliverables "
                "WHERE final_deliverable_id = ?",
                (first_final,),
            )["status"],
            "SUPERSEDED",
        )
        self.assertEqual(
            self.database.one(
                "SELECT status FROM approvals WHERE approval_id = ?",
                (first_approval,),
            )["status"],
            "SUPERSEDED",
        )
        current = self.database.one(
            "SELECT revision_no, status FROM final_deliverables "
            "WHERE final_deliverable_id = ?",
            (second_final,),
        )
        self.assertEqual(current["revision_no"], 2)
        self.assertEqual(current["status"], "PENDING_APPROVAL")
        self.assertEqual(
            self.database.one(
                "SELECT COUNT(*) AS count FROM approvals "
                "WHERE approval_type = 'FINAL_RELEASE' AND status = 'PENDING'"
            )["count"],
            1,
        )

    def test_ignored_candidate_does_not_block_idempotent_final_aggregation(self) -> None:
        kept, ignored = list(self.service.action_items())
        self.service.ignore_action(
            ignored["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            reason="不是本次会议的行动项",
            message_id="ignore-before-final",
        )
        ignored_row = self.service.action(ignored["action_item_id"])
        self.assertEqual(ignored_row["status"], "REJECTED")
        self.assertFalse(ignored_row["required"])

        self.set_team_required(kept, "prepare-team-final")
        self.service.publish_action(
            kept["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            message_id="publish-before-final",
        )
        deadline = (parse_time(self.service.now()) + timedelta(days=3)).isoformat()
        claim = self.service.claim_action(
            kept["action_item_id"],
            claimant_name="王同事",
            promised_deadline_sim_time=deadline,
            message_id="claim-before-final",
        )
        submitted = self.service.submit_artifact(
            kept["action_item_id"],
            actor_id=claim["owner_actor_id"],
            message_id="delivery-before-final",
            payload={"summary": "完成调研", "content": "调研结论"},
        )
        self.service.process_task_result_once(processing_mode="local")
        accepted = self.service.review_artifact(
            submitted["version_id"],
            actor_id=self.service.aggregator_actor_id,
            approve=True,
            comment="通过",
            message_id="review-before-final",
        )

        first_final = self.service.aggregate()
        repeated_final = self.service.aggregate()
        self.assertEqual(first_final, repeated_final)
        final_payload = json.loads(
            self.database.one(
                "SELECT payload FROM final_deliverables "
                "WHERE final_deliverable_id = ?",
                (first_final,),
            )["payload"]
        )
        self.assertEqual(
            final_payload["processing"]["mode"], "deterministic_template"
        )
        self.assertEqual(len(final_payload["organized_report"]["sections"]), 1)
        self.assertEqual(
            final_payload["organized_report"]["sections"][0]["source_version_id"],
            submitted["version_id"],
        )
        self.assertEqual(
            final_payload["organized_report"]["sections"][0][
                "accepted_task_result_id"
            ],
            accepted["accepted_task_result_id"],
        )
        accepted_payload = next(
            iter(final_payload["accepted_task_results"].values())
        )
        self.assertEqual(
            accepted_payload["accepted_task_result_id"],
            accepted["accepted_task_result_id"],
        )
        self.assertEqual(
            self.database.one(
                "SELECT COUNT(*) AS count FROM final_deliverables"
            )["count"],
            1,
        )

        rejected_update = self.service.submit_artifact(
            kept["action_item_id"],
            actor_id=claim["owner_actor_id"],
            message_id="late-delivery-rejected",
            payload={"summary": "新版摘要", "content": "尚未完成的新版"},
        )
        self.service.process_task_result_once(processing_mode="local")
        self.service.review_artifact(
            rejected_update["version_id"],
            actor_id=self.service.aggregator_actor_id,
            approve=False,
            comment="新版证据不足",
            message_id="reject-late-delivery",
        )
        self.assertEqual(
            self.service.action(kept["action_item_id"])["status"], "AGGREGATED"
        )
        rejected_activity = next(
            item
            for task in workbench_state(self.service)["tasks"]
            if task["action_item_id"] == kept["action_item_id"]
            for item in task["activity"]
            if item.get("version_id") == rejected_update["version_id"]
        )
        self.assertEqual(rejected_activity["status"], "REJECTED")
        self.assertIn("新版证据不足", rejected_activity["detail"])
        self.assertEqual(
            self.database.one(
                "SELECT status FROM final_deliverables "
                "WHERE final_deliverable_id = ?",
                (first_final,),
            )["status"],
            "PENDING_APPROVAL",
        )

        accepted_update = self.service.submit_artifact(
            kept["action_item_id"],
            actor_id=claim["owner_actor_id"],
            message_id="late-delivery-accepted",
            payload={"summary": "新版摘要", "content": "补齐证据的新版"},
        )
        self.service.process_task_result_once(processing_mode="local")
        self.service.review_artifact(
            accepted_update["version_id"],
            actor_id=self.service.aggregator_actor_id,
            approve=True,
            comment="新版通过",
            message_id="accept-late-delivery",
        )
        self.assertEqual(
            self.database.one(
                "SELECT status FROM final_deliverables "
                "WHERE final_deliverable_id = ?",
                (first_final,),
            )["status"],
            "SUPERSEDED",
        )
        replacement_job = self.service.queue_final_organization(
            processing_mode="local"
        )
        self.assertIsNotNone(replacement_job)
        replacement_result = self.service.dispatch_final_organization_once(
            session_id="automatic-replacement-test",
            organizer=None,
        )
        replacement_final = replacement_result["final_deliverable_id"]
        self.assertNotEqual(replacement_final, first_final)


if __name__ == "__main__":
    unittest.main()
