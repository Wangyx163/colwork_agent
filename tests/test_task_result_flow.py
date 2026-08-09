from __future__ import annotations

import base64
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from collab_agent.auth import VirtualSessionPrincipalProvider
from collab_agent.meeting import load_meeting_service
from collab_agent.models import parse_time
from collab_agent.store import Database
from collab_agent.task_result_processing import (
    TaskResultProcessingError,
    build_deterministic_task_result,
)
from collab_agent.web import workbench_state


class TaskResultFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        extraction = root / "extraction.json"
        transcript = root / "transcript.txt"
        extraction.write_text(
            json.dumps(
                {
                    "provider": "bailian",
                    "model": "qwen-plus",
                    "input_sha256": "c" * 64,
                    "action_items": [
                        {
                            "title": "整理活动复盘",
                            "deliverable": "活动复盘报告",
                            "owner_name": None,
                            "deadline_text": None,
                            "deadline_iso": None,
                            "source_timestamp": "00:03:00",
                            "source_quote": "请整理活动复盘",
                            "confidence": 0.95,
                            "needs_confirmation": True,
                            "uncertainties": ["负责人未明确"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        transcript.write_text(
            "主持人(00:03:00): 请整理活动复盘\n", encoding="utf-8"
        )
        self.db = Database(":memory:")
        self.db.initialize()
        self.service = load_meeting_service(
            self.db,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="成果处理测试团队",
            coordinator_name="会议负责人",
            participant_names=["执行同事"],
        )
        self.action = next(iter(self.service.action_items()))
        metadata = self.service.proposal_metadata(self.action)
        deadline = (parse_time(self.service.now()) + timedelta(days=2)).isoformat()
        self.service.revise_action_proposal(
            self.action["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            title=self.action["title"],
            deliverable=metadata["deliverable"],
            acceptance_criteria="复盘应包含结论与来源",
            priority="P0",
            team_required_by_sim_time=deadline,
            message_id="prepare-result-flow",
        )
        self.service.publish_action(
            self.action["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            message_id="publish-result-flow",
        )
        self.claim = self.service.claim_action(
            self.action["action_item_id"],
            claimant_name="执行同事",
            promised_deadline_sim_time=deadline,
            message_id="claim-result-flow",
        )

    def tearDown(self) -> None:
        self.db.close()
        self.directory.cleanup()

    def expire_processing_backoff(self, version_id: str) -> None:
        """Stand in for elapsed wall-clock time between retry attempts."""

        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE artifact_versions SET processing_next_attempt_at = NULL "
                "WHERE version_id = ?",
                (version_id,),
            )

    def submit_text_result(self, suffix: str = "") -> dict:
        content = "活动到场率提升，后续需优化报名提醒。" + suffix
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        return self.service.submit_artifact(
            self.action["action_item_id"],
            actor_id=self.claim["owner_actor_id"],
            message_id=f"submit-result-flow{suffix}",
            payload={
                "summary": "活动复盘已完成",
                "content": "结论见附件",
                "links": [],
                "files": [
                    {
                        "name": "复盘.txt",
                        "type": "text/plain",
                        "size": len(content.encode("utf-8")),
                        "data": f"data:text/plain;base64,{encoded}",
                    }
                ],
            },
        )

    def test_unauthorized_submit_is_rejected_before_attachment_decode(self) -> None:
        with patch("collab_agent.service.extract_attachments") as extractor:
            with self.assertRaisesRegex(PermissionError, "assigned owner"):
                self.service.submit_artifact(
                    self.action["action_item_id"],
                    actor_id=self.service.aggregator_actor_id,
                    message_id="unauthorized-file-submit",
                    payload={
                        "summary": "伪造提交",
                        "files": [
                            {
                                "name": "large.pdf",
                                "type": "application/pdf",
                                "size": 999999999,
                                "data": "data:application/pdf;base64,AAAA",
                            }
                        ],
                    },
                )
        extractor.assert_not_called()

    def test_layered_processing_freezes_version_bound_result(self) -> None:
        submitted = self.submit_text_result()
        version = self.db.one(
            "SELECT * FROM artifact_versions WHERE version_id = ?",
            (submitted["version_id"],),
        )
        stored_payload = json.loads(version["payload"])
        self.assertNotIn("data", stored_payload["files"][0])
        self.assertNotIn("base64", version["payload"])
        extractions = json.loads(version["attachment_extractions"])
        self.assertEqual(extractions[0]["extraction_status"], "EXTRACTED")
        self.assertIn("到场率提升", extractions[0]["extracted_text"])
        attachment_event = self.db.one(
            "SELECT payload FROM audit_events WHERE aggregate_id = ? "
            "AND event_type = 'AttachmentExtractionCompleted' "
            "ORDER BY sequence_no DESC LIMIT 1",
            (submitted["version_id"],),
        )
        attachment_audit = json.loads(attachment_event["payload"])
        attachment_invocation = attachment_audit["invocation"]
        self.assertEqual(
            attachment_invocation["purpose"],
            "TASK_RESULT_EVIDENCE_EXTRACTION",
        )
        self.assertEqual(
            attachment_invocation["skill_version"], "attachment-extractor.v1"
        )
        self.assertEqual(attachment_invocation["output_status"], "SUCCEEDED")
        self.assertNotIn(
            "data:text/plain;base64", json.dumps(attachment_audit, ensure_ascii=False)
        )
        with self.assertRaisesRegex(ValueError, "processing must finish"):
            self.service.review_artifact(
                submitted["version_id"],
                actor_id=self.service.aggregator_actor_id,
                approve=True,
                comment="通过",
                message_id="review-too-early",
            )

        contexts: list[dict] = []

        def processor(context: dict) -> tuple[dict, dict]:
            contexts.append(context)
            return build_deterministic_task_result(context)

        processed = self.service.process_task_result_once(
            processing_mode="local", processor=processor
        )
        self.assertEqual(processed["processing_status"], "READY")
        self.assertEqual(contexts[0]["action_item_id"], self.action["action_item_id"])
        self.assertEqual(contexts[0]["version_id"], submitted["version_id"])
        self.assertNotIn("data", contexts[0]["evidence"]["attachments"][0])
        self.assertEqual(
            contexts[0]["task_contract"]["work_requirements"],
            "活动复盘报告",
        )
        self.assertEqual(
            contexts[0]["task_contract"]["management_review_policy"],
            "复盘应包含结论与来源",
        )
        started_event = self.db.one(
            "SELECT payload FROM audit_events WHERE aggregate_id = ? "
            "AND event_type = 'TaskResultProcessingStarted' "
            "ORDER BY sequence_no DESC LIMIT 1",
            (submitted["version_id"],),
        )
        success_event = self.db.one(
            "SELECT payload FROM audit_events WHERE aggregate_id = ? "
            "AND event_type = 'TaskResultProcessingSucceeded' "
            "ORDER BY sequence_no DESC LIMIT 1",
            (submitted["version_id"],),
        )
        started_invocation = json.loads(started_event["payload"])["invocation"]
        success_invocation = json.loads(success_event["payload"])["invocation"]
        self.assertEqual(started_invocation["principal"]["actor_id"], "SYSTEM")
        self.assertEqual(started_invocation["purpose"], "TASK_RESULT_REVIEW")
        self.assertIn(
            "task_contract.management_review_policy",
            started_invocation["field_allowlist"],
        )
        self.assertEqual(
            started_invocation["entity_versions"]["artifact_version_id"],
            submitted["version_id"],
        )
        self.assertEqual(success_invocation["output_status"], "SUCCEEDED")
        self.assertEqual(
            success_invocation["input_hash"], started_invocation["input_hash"]
        )
        processing_metadata = json.loads(
            self.db.one(
                "SELECT processing_metadata FROM artifact_versions "
                "WHERE version_id = ?",
                (submitted["version_id"],),
            )["processing_metadata"]
        )
        self.assertEqual(
            processing_metadata["input_hash"], success_invocation["input_hash"]
        )

        provider = VirtualSessionPrincipalProvider(
            self.db,
            episode_id=self.service.episode_id,
            secret="task-result-flow-test-secret-32-bytes",
        )
        participant = provider.resolve(
            provider.issue(self.claim["owner_actor_id"])["token"]
        )
        participant_task = workbench_state(
            self.service, principal=participant
        )["tasks"][0]
        self.assertNotIn("processing_result", participant_task["latest_version"])
        coordinator_task = workbench_state(self.service)["tasks"][0]
        self.assertEqual(
            coordinator_task["latest_version"]["processing_status"], "READY"
        )
        self.assertIsNotNone(
            coordinator_task["latest_version"]["processing_result"]
        )

        accepted = self.service.review_artifact(
            submitted["version_id"],
            actor_id=self.service.aggregator_actor_id,
            approve=True,
            comment="来源清楚，可以验收",
            completion_report="活动复盘已验收：结论与附件来源完整。",
            message_id="review-result-flow",
        )
        frozen = self.db.one(
            "SELECT * FROM accepted_task_results WHERE accepted_task_result_id = ?",
            (accepted["accepted_task_result_id"],),
        )
        self.assertEqual(frozen["accepted_version_id"], submitted["version_id"])
        self.assertEqual(
            frozen["completion_report"],
            "活动复盘已验收：结论与附件来源完整。",
        )
        repeated = self.service.review_artifact(
            submitted["version_id"],
            actor_id=self.service.aggregator_actor_id,
            approve=True,
            comment="重复回调",
            message_id="review-result-flow-repeated",
        )
        self.assertTrue(repeated["idempotent"])
        with self.assertRaisesRegex(ValueError, "opposite decision"):
            self.service.review_artifact(
                submitted["version_id"],
                actor_id=self.service.aggregator_actor_id,
                approve=False,
                comment="反向决定",
                message_id="review-result-flow-opposite",
            )
        self.assertIsNotNone(self.service.aggregate())

    def test_processing_failure_has_system_error_code_and_retry_state(self) -> None:
        submitted = self.submit_text_result()

        def failed_processor(context: dict) -> tuple[dict, dict]:
            raise TaskResultProcessingError(
                "provider timed out",
                error_code="NETWORK_TIMEOUT",
                stage="MODEL_REQUEST",
                retryable=True,
            )

        failed = self.service.process_task_result_once(
            processing_mode="bailian", processor=failed_processor
        )
        self.assertEqual(failed["processing_status"], "RETRY_WAIT")
        self.assertEqual(failed["error_code"], "NETWORK_TIMEOUT")
        stored = self.db.one(
            "SELECT * FROM artifact_versions WHERE version_id = ?",
            (submitted["version_id"],),
        )
        self.assertEqual(stored["processing_error_stage"], "MODEL_REQUEST")
        self.assertEqual(stored["processing_retryable"], 1)
        failed_event = self.db.one(
            "SELECT payload FROM audit_events WHERE aggregate_id = ? "
            "AND event_type = 'TaskResultProcessingFailed' "
            "ORDER BY sequence_no DESC LIMIT 1",
            (submitted["version_id"],),
        )
        failed_invocation = json.loads(failed_event["payload"])["invocation"]
        self.assertEqual(failed_invocation["purpose"], "TASK_RESULT_REVIEW")
        self.assertEqual(failed_invocation["output_status"], "RETRY_WAIT")
        self.assertEqual(failed_invocation["error_code"], "NETWORK_TIMEOUT")

    def test_unexpected_processing_failure_cannot_strand_a_version(self) -> None:
        submitted = self.submit_text_result("-unexpected")

        def unexpected_processor(context: dict) -> tuple[dict, dict]:
            raise RuntimeError("unexpected adapter defect")

        failed = self.service.process_task_result_once(
            processing_mode="bailian", processor=unexpected_processor
        )
        self.assertEqual(failed["processing_status"], "FAILED")
        self.assertEqual(failed["error_code"], "UNEXPECTED_PROCESSING_ERROR")
        stored = self.db.one(
            "SELECT * FROM artifact_versions WHERE version_id = ?",
            (submitted["version_id"],),
        )
        self.assertEqual(stored["processing_status"], "FAILED")
        self.assertEqual(stored["processing_error_stage"], "TASK_RESULT_PROCESSING")

    def test_coordinator_can_retry_an_exhausted_transient_model_failure(self) -> None:
        submitted = self.submit_text_result("-retry-provider")

        def failed_processor(context: dict) -> tuple[dict, dict]:
            raise TaskResultProcessingError(
                "provider timed out",
                error_code="NETWORK_TIMEOUT",
                stage="MODEL_REQUEST",
                retryable=True,
            )

        for attempt in range(3):
            failed = self.service.process_task_result_once(
                processing_mode="bailian", processor=failed_processor
            )
            self.assertIsNotNone(failed)
            if attempt < 2:
                # A retryable failure serves a wall-clock backoff before it can
                # be claimed again; without it the poll loop would burn every
                # attempt within seconds of the first blip.
                self.assertEqual(failed["processing_status"], "RETRY_WAIT")
                self.assertIsNone(
                    self.service.process_task_result_once(
                        processing_mode="bailian", processor=failed_processor
                    )
                )
                self.expire_processing_backoff(submitted["version_id"])
        self.assertEqual(failed["processing_status"], "FAILED")

        retried = self.service.retry_task_result_processing(
            submitted["version_id"],
            actor_id=self.service.aggregator_actor_id,
            message_id="retry-transient-provider",
        )
        self.assertEqual(retried["processing_status"], "PENDING")
        stored = self.db.one(
            "SELECT processing_status, processing_attempt_count "
            "FROM artifact_versions WHERE version_id = ?",
            (submitted["version_id"],),
        )
        self.assertEqual(stored["processing_status"], "PENDING")
        self.assertEqual(int(stored["processing_attempt_count"]), 0)

        processed = self.service.process_task_result_once(processing_mode="local")
        self.assertEqual(processed["processing_status"], "READY")
        retry_event = self.db.one(
            "SELECT event_type FROM audit_events WHERE aggregate_id = ? "
            "AND event_type = 'TaskResultProcessingRetryRequested'",
            (submitted["version_id"],),
        )
        self.assertIsNotNone(retry_event)

    def test_coordinator_can_retry_schema_failure_after_contract_fix(self) -> None:
        submitted = self.submit_text_result("-retry-schema")

        def invalid_schema(context: dict) -> tuple[dict, dict]:
            raise TaskResultProcessingError(
                "gaps[3] has invalid source_refs",
                error_code="INVALID_SCHEMA",
                stage="MODEL_RESPONSE",
                retryable=False,
            )

        failed = self.service.process_task_result_once(
            processing_mode="bailian", processor=invalid_schema
        )
        self.assertEqual(failed["processing_status"], "FAILED")
        self.assertEqual(failed["error_code"], "INVALID_SCHEMA")

        retried = self.service.retry_task_result_processing(
            submitted["version_id"],
            actor_id=self.service.aggregator_actor_id,
            message_id="retry-schema-after-contract-fix",
        )
        self.assertEqual(retried["processing_status"], "PENDING")
        processed = self.service.process_task_result_once(processing_mode="local")
        self.assertEqual(processed["processing_status"], "READY")

    def test_final_rejection_requires_feedback_and_replacement_is_released(self) -> None:
        first = self.submit_text_result("-final-v1")
        self.service.process_task_result_once(processing_mode="local")
        first_accepted = self.service.review_artifact(
            first["version_id"],
            actor_id=self.service.aggregator_actor_id,
            approve=True,
            comment="任务结果通过",
            completion_report="第一版任务完成报告",
            message_id="final-review-v1",
        )
        first_final = self.service.aggregate()
        approval = self.service.pending_approval("FINAL_RELEASE")
        self.assertIsNotNone(approval)

        stored_final = self.db.one(
            "SELECT payload FROM final_deliverables WHERE final_deliverable_id = ?",
            (first_final,),
        )
        valid_payload = json.loads(stored_final["payload"])
        stale_payload = json.loads(stored_final["payload"])
        stale_payload["organized_report"]["sections"][0][
            "accepted_task_result_id"
        ] = "accepted_result_from_an_old_version"
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE final_deliverables SET payload = ? "
                "WHERE final_deliverable_id = ?",
                (json.dumps(stale_payload, ensure_ascii=False), first_final),
            )
        with self.assertRaisesRegex(ValueError, "lineage is stale"):
            self.service.decide_approval(
                approval["approval_id"],
                actor_id=self.service.aggregator_actor_id,
                approve=True,
            )
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE final_deliverables SET payload = ? "
                "WHERE final_deliverable_id = ?",
                (json.dumps(valid_payload, ensure_ascii=False), first_final),
            )

        with self.assertRaisesRegex(ValueError, "requires feedback"):
            self.service.decide_approval(
                approval["approval_id"],
                actor_id=self.service.aggregator_actor_id,
                approve=False,
            )
        rejected = self.service.decide_approval(
            approval["approval_id"],
            actor_id=self.service.aggregator_actor_id,
            approve=False,
            comment="请补充对应任务的结论来源后重新提交",
        )
        self.assertEqual(rejected["status"], "REJECTED")
        self.assertFalse(
            any(
                row["effect_type"] == "FINAL_RELEASE"
                for row in self.service.outbox()
            )
        )
        coordinator_state = workbench_state(self.service)
        self.assertEqual(
            coordinator_state["final"]["release_review"]["comment"],
            "请补充对应任务的结论来源后重新提交",
        )

        provider = VirtualSessionPrincipalProvider(
            self.db,
            episode_id=self.service.episode_id,
            secret="final-release-visibility-secret-32-bytes",
        )
        participant = provider.resolve(
            provider.issue(self.claim["owner_actor_id"])["token"]
        )
        self.assertIsNone(
            workbench_state(self.service, principal=participant)["final"]
        )

        second = self.submit_text_result("-final-v2")
        self.service.process_task_result_once(processing_mode="local")
        second_accepted = self.service.review_artifact(
            second["version_id"],
            actor_id=self.service.aggregator_actor_id,
            approve=True,
            comment="补充来源后通过",
            completion_report="第二版任务完成报告",
            message_id="final-review-v2",
        )
        self.assertEqual(
            self.db.one(
                "SELECT status FROM final_deliverables "
                "WHERE final_deliverable_id = ?",
                (first_final,),
            )["status"],
            "SUPERSEDED",
        )
        replacement = self.service.aggregate()
        replacement_payload = json.loads(
            self.db.one(
                "SELECT payload FROM final_deliverables "
                "WHERE final_deliverable_id = ?",
                (replacement,),
            )["payload"]
        )
        section = replacement_payload["organized_report"]["sections"][0]
        self.assertEqual(section["source_version_id"], second["version_id"])
        self.assertEqual(
            section["accepted_task_result_id"],
            second_accepted["accepted_task_result_id"],
        )
        self.assertNotEqual(
            section["accepted_task_result_id"],
            first_accepted["accepted_task_result_id"],
        )

        replacement_approval = self.service.pending_approval("FINAL_RELEASE")
        self.service.decide_approval(
            replacement_approval["approval_id"],
            actor_id=self.service.aggregator_actor_id,
            approve=True,
        )
        self.service.dispatch_all(session_id="final-release-test")
        released_state = workbench_state(self.service, principal=participant)
        self.assertEqual(released_state["final"]["status"], "RELEASED")
        self.assertEqual(released_state["episode"]["status"], "ARCHIVED")

    def test_fact_report_and_private_memory_are_generated_and_user_controlled(self) -> None:
        owner = self.claim["owner_actor_id"]
        coordinator = self.service.aggregator_actor_id
        self.service.record_progress_signal(
            self.action["action_item_id"],
            actor_id=owner,
            signal_type="ON_TRACK",
            note="按计划推进",
            message_id="memory-signal-1",
        )
        self.service.record_progress_signal(
            self.action["action_item_id"],
            actor_id=owner,
            signal_type="READY_TO_SUBMIT",
            note="准备提交",
            message_id="memory-signal-2",
        )
        assistance = self.service.request_assistance(
            self.action["action_item_id"],
            actor_id=owner,
            target_actor_id=coordinator,
            category="DECISION",
            summary="请确认复盘重点",
            message_id="memory-help-open",
        )
        self.service.update_assistance(
            assistance["assistance_request_id"],
            actor_id=coordinator,
            action="ACKNOWLEDGE",
            message_id="memory-help-ack",
        )
        self.service.update_assistance(
            assistance["assistance_request_id"],
            actor_id=coordinator,
            action="RESOLVE",
            resolution_summary="已确认复盘重点",
            message_id="memory-help-resolve",
        )

        first = self.submit_text_result("-v1")
        self.service.process_task_result_once(processing_mode="local")
        first_review = self.service.review_artifact(
            first["version_id"],
            actor_id=coordinator,
            approve=True,
            comment="第一版通过",
            message_id="memory-review-v1",
        )
        self.assertEqual(first_review["collaboration_report_status"], "READY")
        first_result = self.db.one(
            "SELECT * FROM accepted_task_results WHERE accepted_task_result_id = ?",
            (first_review["accepted_task_result_id"],),
        )
        first_report = json.loads(first_result["collaboration_report"])
        self.assertEqual(first_report["accepted_version_id"], first["version_id"])
        self.assertEqual(len(first_report["assistance"]), 1)
        self.assertGreaterEqual(len(first_report["signals"]), 2)
        self.assertTrue(first_report["source_event_ids"])

        second = self.submit_text_result("-v2")
        self.service.process_task_result_once(processing_mode="local")
        second_review = self.service.review_artifact(
            second["version_id"],
            actor_id=coordinator,
            approve=True,
            comment="第二版通过",
            message_id="memory-review-v2",
        )
        self.assertEqual(second_review["collaboration_report_status"], "READY")
        drafts = self.db.all(
            "SELECT * FROM collaboration_memories WHERE actor_id = ? "
            "AND status = 'PRIVATE_DRAFT' ORDER BY topic, created_sim_time",
            (owner,),
        )
        topics = {row["topic"] for row in drafts}
        self.assertTrue({"HELP_SEEKING", "PROGRESS_SIGNAL"} <= topics)
        # 交付模式 is asked in the questionnaire now rather than counted from
        # version history, so observing it here would be the system answering
        # a question that belongs to the person.
        self.assertNotIn("DELIVERY_RHYTHM", topics)

        provider = VirtualSessionPrincipalProvider(
            self.db,
            episode_id=self.service.episode_id,
            secret="memory-visibility-test-secret-32-bytes",
        )
        owner_principal = provider.resolve(provider.issue(owner)["token"])
        coordinator_principal = provider.resolve(provider.issue(coordinator)["token"])
        self.assertTrue(workbench_state(self.service, principal=owner_principal)["memories"])
        self.assertEqual(
            workbench_state(self.service, principal=coordinator_principal)["memories"],
            [],
        )
        with self.assertRaises(PermissionError):
            self.service.decide_collaboration_memory(
                drafts[0]["memory_id"],
                actor_id=coordinator,
                action="CONFIRM",
                message_id="memory-coordinator-forbidden",
            )

        by_topic = {}
        for row in drafts:
            by_topic.setdefault(row["topic"], row)
        confirmed = self.service.decide_collaboration_memory(
            by_topic["HELP_SEEKING"]["memory_id"],
            actor_id=owner,
            action="CONFIRM",
            message_id="memory-confirm",
        )
        self.assertEqual(confirmed["status"], "CONFIRMED")
        # An evaluative label cannot be smuggled in as a correction. This runs
        # before the reject below because it needs a draft still standing --
        # and it is the assertion here most worth keeping, since the lexicon is
        # the only thing preventing "unreliable person" from being stored about
        # somebody.
        with self.assertRaisesRegex(ValueError, "not allowed for this topic"):
            self.service.decide_collaboration_memory(
                by_topic["PROGRESS_SIGNAL"]["memory_id"],
                actor_id=owner,
                action="REPLACE",
                replacement_code="UNRELIABLE_PERSON",
                message_id="memory-prohibited",
            )
        # Declining is a first-class outcome, not a failure to respond.
        # Correcting a draft to another value in the same topic is exercised by
        # the P0 evaluation, which has a run long enough to reach it.
        rejected = self.service.decide_collaboration_memory(
            by_topic["PROGRESS_SIGNAL"]["memory_id"],
            actor_id=owner,
            action="REJECT",
            message_id="memory-reject",
        )
        self.assertEqual(rejected["status"], "REJECTED")


if __name__ == "__main__":
    unittest.main()
