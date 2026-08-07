from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from collab_agent.auth import VirtualSessionPrincipalProvider
from collab_agent.meeting import load_meeting_service
from collab_agent.metrics import build_report
from collab_agent.models import parse_time
from collab_agent.store import Database
from collab_agent.web import workbench_state


class SignalAndAssistanceTests(unittest.TestCase):
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
                    "input_sha256": "b" * 64,
                    "action_items": [
                        {
                            "title": "整理客户问题",
                            "deliverable": "客户问题清单",
                            "owner_name": None,
                            "deadline_text": None,
                            "deadline_iso": None,
                            "source_timestamp": "00:01:00",
                            "source_quote": "整理客户问题",
                            "confidence": 0.9,
                            "needs_confirmation": True,
                            "uncertainties": ["负责人未明确"],
                        },
                        {
                            "title": "准备发布说明",
                            "deliverable": "发布说明",
                            "owner_name": None,
                            "deadline_text": None,
                            "deadline_iso": None,
                            "source_timestamp": "00:02:00",
                            "source_quote": "准备发布说明",
                            "confidence": 0.9,
                            "needs_confirmation": True,
                            "uncertainties": ["负责人未明确"],
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        transcript.write_text(
            "主持人(00:01:00): 整理客户问题\n"
            "主持人(00:02:00): 准备发布说明\n",
            encoding="utf-8",
        )
        self.db = Database(":memory:")
        self.db.initialize()
        self.service = load_meeting_service(
            self.db,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="信号测试团队",
            coordinator_name="会议负责人",
            participant_names=["参会者1", "参会者2"],
        )
        self.service.fixture["policy"].update(
            {
                "check_in_lead_hours": 72,
                "silence_window_hours": 1,
                "inquiry_cooldown_hours": 1,
                "signal_default_ttl_hours": 1,
                "l2_wait_hours": 1,
            }
        )
        self.actions = list(self.service.action_items())
        team_required_by = (
            parse_time(self.service.now()) + timedelta(days=3)
        ).isoformat()
        self.claims = []
        for index, action in enumerate(self.actions):
            metadata = self.service.proposal_metadata(action)
            self.service.revise_action_proposal(
                action["action_item_id"],
                actor_id=self.service.aggregator_actor_id,
                title=action["title"],
                deliverable=metadata["deliverable"],
                acceptance_criteria="负责人检查内容完整性",
                priority="P1",
                team_required_by_sim_time=team_required_by,
                message_id=f"prepare-{index}",
            )
            self.service.publish_action(
                action["action_item_id"],
                actor_id=self.service.aggregator_actor_id,
                message_id=f"publish-{index}",
            )
            self.claims.append(
                self.service.claim_action(
                    action["action_item_id"],
                    claimant_name=f"参会者{index + 1}",
                    promised_deadline_sim_time=team_required_by,
                    message_id=f"claim-{index}",
                )
            )

    def tearDown(self) -> None:
        self.db.close()
        self.directory.cleanup()

    def test_refresh_is_not_a_signal_but_quick_status_is(self) -> None:
        action_id = self.actions[0]["action_item_id"]
        before = self.service.action(action_id)
        event_count = len(self.service.audit_events())
        workbench_state(self.service)
        workbench_state(self.service)
        after_refresh = self.service.action(action_id)
        self.assertEqual(
            before["last_owner_signal_at"], after_refresh["last_owner_signal_at"]
        )
        self.assertEqual(event_count, len(self.service.audit_events()))

        result = self.service.record_progress_signal(
            action_id,
            actor_id=self.claims[0]["owner_actor_id"],
            signal_type="BLOCKED",
            note="等待客户补充数据",
            message_id="signal-blocked",
        )
        self.assertEqual(result["signal_type"], "BLOCKED")
        stored = self.service.action(action_id)
        self.assertEqual(stored["last_owner_signal_type"], "BLOCKED")

    def test_assistance_is_limited_to_attendees_and_keeps_lifecycle(self) -> None:
        action_id = self.actions[0]["action_item_id"]
        requester = self.claims[0]["owner_actor_id"]
        target = self.claims[1]["owner_actor_id"]
        with self.assertRaisesRegex(ValueError, "another meeting attendee"):
            self.service.request_assistance(
                action_id,
                actor_id=requester,
                target_actor_id=requester,
                category="CAPACITY",
                summary="需要分担资料整理",
                message_id="help-self",
            )
        with self.assertRaisesRegex(PermissionError, "meeting attendee"):
            self.service.request_assistance(
                action_id,
                actor_id=requester,
                target_actor_id="outsider",
                category="CAPACITY",
                summary="需要分担资料整理",
                message_id="help-outsider",
            )

        request = self.service.request_assistance(
            action_id,
            actor_id=requester,
            target_actor_id=target,
            category="CAPACITY",
            summary="请协助整理其中两条客户问题",
            message_id="help-open",
        )
        provider = VirtualSessionPrincipalProvider(
            self.db,
            episode_id=self.service.episode_id,
            secret="signal-assistance-test-secret-32-bytes",
        )
        target_principal = provider.resolve(provider.issue(target)["token"])
        target_state = workbench_state(self.service, principal=target_principal)
        target_task = next(
            task for task in target_state["tasks"] if task["action_item_id"] == action_id
        )
        self.assertTrue(target_task["is_help_target"])
        self.assertTrue(target_task["is_collaborator"])
        self.assertTrue(target_task["can_contribute"])
        self.assertEqual(
            target_task["active_assistance"]["summary"],
            "请协助整理其中两条客户问题",
        )

        acknowledged = self.service.update_assistance(
            request["assistance_request_id"],
            actor_id=target,
            action="ACKNOWLEDGE",
            message_id="help-ack",
        )
        self.assertEqual(acknowledged["status"], "ACKNOWLEDGED")
        signal = self.service.record_progress_signal(
            action_id,
            actor_id=target,
            signal_type="ON_TRACK",
            note="已开始整理协作部分",
            message_id="help-collaborator-signal",
        )
        self.assertEqual(signal["contributor_role"], "REQUESTED_COLLABORATOR")
        delivery = self.service.submit_artifact(
            action_id,
            actor_id=target,
            message_id="help-collaborator-delivery",
            payload={
                "summary": "协作部分已整理",
                "content": "补充两条客户问题及分类。",
                "links": [],
                "files": [],
            },
        )
        self.assertEqual(delivery["submission_kind"], "CONTRIBUTION")
        self.assertEqual(delivery["review_status"], "NOT_REQUIRED")
        self.assertEqual(delivery["processing_status"], "PENDING")
        self.assertEqual(delivery["action_status"], "TRACKING")
        stored_delivery = self.db.one(
            "SELECT submitted_by_actor_id, review_status, processing_status "
            "FROM artifact_versions "
            "WHERE version_id = ?",
            (delivery["version_id"],),
        )
        self.assertEqual(stored_delivery["submitted_by_actor_id"], target)
        self.assertEqual(stored_delivery["review_status"], "NOT_REQUIRED")
        self.assertIsNone(
            self.service.process_task_result_once(processing_mode="local")
        )
        processed = self.service.process_task_result_once(
            processing_mode="local", allow_contribution_analysis=True
        )
        self.assertEqual(processed["version_id"], delivery["version_id"])
        self.assertEqual(processed["processing_purpose"], "CONTRIBUTION_ANALYSIS")
        self.assertEqual(self.service.action(action_id)["status"], "TRACKING")
        activity_state = workbench_state(
            self.service, principal=target_principal
        )
        activity_task = next(
            task
            for task in activity_state["tasks"]
            if task["action_item_id"] == action_id
        )
        activity_titles = {item["title"] for item in activity_task["activity"]}
        self.assertIn("邀请协作者", activity_titles)
        self.assertIn("协作者确认接手", activity_titles)
        self.assertIn("状态更新：按计划", activity_titles)
        self.assertTrue(
            any(title.startswith("协作者提交贡献版本") for title in activity_titles)
        )
        revision = self.service.decide_contribution(
            delivery["version_id"],
            actor_id=requester,
            action="REQUEST_REVISION",
            comment="请补充分类依据",
            message_id="help-contribution-revision",
        )
        self.assertEqual(revision["contribution_status"], "REVISION_REQUESTED")
        self.assertEqual(revision["action_status"], "TRACKING")
        revised_delivery = self.service.submit_artifact(
            action_id,
            actor_id=target,
            message_id="help-collaborator-delivery-v2",
            payload={
                "summary": "协作部分已按反馈补充",
                "content": "补充两条客户问题、分类及分类依据。",
                "links": [],
                "files": [],
            },
        )
        self.assertEqual(revised_delivery["submission_kind"], "CONTRIBUTION")
        processed_revision = self.service.process_task_result_once(
            processing_mode="local", allow_contribution_analysis=True
        )
        self.assertEqual(processed_revision["version_id"], revised_delivery["version_id"])
        self.assertEqual(
            processed_revision["processing_purpose"], "CONTRIBUTION_ANALYSIS"
        )
        with self.assertRaises(PermissionError):
            self.service.update_assistance(
                request["assistance_request_id"],
                actor_id=self.service.aggregator_actor_id,
                action="CANCEL",
                message_id="help-invalid-cancel",
            )
        resolved = self.service.update_assistance(
            request["assistance_request_id"],
            actor_id=target,
            action="RESOLVE",
            resolution_summary="已补充两条问题及分类",
            message_id="help-resolve",
        )
        self.assertEqual(resolved["status"], "RESOLVED")
        stored = self.db.one(
            "SELECT * FROM assistance_requests WHERE assistance_request_id = ?",
            (request["assistance_request_id"],),
        )
        self.assertEqual(stored["resolution_summary"], "已补充两条问题及分类")
        coordinator_state = workbench_state(self.service)
        coordinator_task = next(
            task
            for task in coordinator_state["tasks"]
            if task["action_item_id"] == action_id
        )
        self.assertIn(
            "协作已完成",
            {item["title"] for item in coordinator_task["activity"]},
        )
        historical_state = workbench_state(
            self.service, principal=target_principal
        )
        historical_task = next(
            task
            for task in historical_state["tasks"]
            if task["action_item_id"] == action_id
        )
        self.assertTrue(historical_task["has_collaborated"])
        self.assertFalse(historical_task["is_collaborator"])
        self.assertFalse(historical_task["can_contribute"])
        historical_titles = [item["title"] for item in historical_task["activity"]]
        self.assertLess(
            historical_titles.index("协作已完成"),
            next(
                index
                for index, title in enumerate(historical_titles)
                if title.startswith("协作者提交贡献版本")
            ),
        )
        with self.assertRaisesRegex(ValueError, "active collaboration"):
            self.service.decide_contribution(
                revised_delivery["version_id"],
                actor_id=requester,
                action="REQUEST_REVISION",
                comment="再次补充",
                message_id="help-revision-after-resolve",
            )
        promoted = self.service.decide_contribution(
            revised_delivery["version_id"],
            actor_id=requester,
            action="PROMOTE",
            comment="该版本可代表整项任务成果",
            message_id="help-contribution-promote",
        )
        self.assertEqual(promoted["contribution_status"], "PROMOTED")
        self.assertEqual(promoted["review_status"], "PENDING")
        self.assertEqual(promoted["action_status"], "PENDING_ACCEPTANCE")
        self.assertEqual(self.service.action(action_id)["status"], "PENDING_ACCEPTANCE")
        self.assertEqual(self.service.reconcile_legacy_collaborator_candidates(), 0)

    def test_legacy_collaborator_candidate_is_reclassified_only_once(self) -> None:
        action_id = self.actions[0]["action_item_id"]
        requester = self.claims[0]["owner_actor_id"]
        target = self.claims[1]["owner_actor_id"]
        self.service.request_assistance(
            action_id,
            actor_id=requester,
            target_actor_id=target,
            category="CAPACITY",
            summary="协助补充材料",
            message_id="legacy-help-open",
        )
        delivery = self.service.submit_artifact(
            action_id,
            actor_id=target,
            message_id="legacy-collaborator-delivery",
            payload={
                "summary": "历史协作者成果",
                "content": "模拟旧版本直接进入会议负责人验收。",
                "links": [],
                "files": [],
            },
        )
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE artifact_versions SET review_status = 'PENDING' "
                "WHERE version_id = ?",
                (delivery["version_id"],),
            )
            cursor.execute(
                "UPDATE action_items SET status = 'PENDING_ACCEPTANCE' "
                "WHERE action_item_id = ?",
                (action_id,),
            )
        self.assertEqual(self.service.reconcile_legacy_collaborator_candidates(), 1)
        repaired = self.db.one(
            "SELECT review_status FROM artifact_versions WHERE version_id = ?",
            (delivery["version_id"],),
        )
        self.assertEqual(repaired["review_status"], "NOT_REQUIRED")
        self.assertEqual(self.service.action(action_id)["status"], "TRACKING")
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE artifact_versions SET processing_status = 'READY', "
                "processing_result = '{}', processing_metadata = '{}' "
                "WHERE version_id = ?",
                (delivery["version_id"],),
            )
        self.assertEqual(self.service.reconcile_legacy_collaborator_candidates(), 1)
        requeued = self.db.one(
            "SELECT processing_status, processing_result, processing_metadata "
            "FROM artifact_versions WHERE version_id = ?",
            (delivery["version_id"],),
        )
        self.assertEqual(requeued["processing_status"], "PENDING")
        self.assertIsNone(requeued["processing_result"])
        self.assertIsNone(requeued["processing_metadata"])
        processed = self.service.process_task_result_once(
            processing_mode="local", allow_contribution_analysis=True
        )
        self.assertEqual(processed["processing_purpose"], "CONTRIBUTION_ANALYSIS")
        ready = self.db.one(
            "SELECT processing_metadata FROM artifact_versions WHERE version_id = ?",
            (delivery["version_id"],),
        )
        ready_metadata = json.loads(ready["processing_metadata"])
        self.assertEqual(
            ready_metadata["context_manifest"]["purpose"],
            "CONTRIBUTION_ANALYSIS",
        )
        ready_metadata["context_manifest"]["purpose"] = "TASK_RESULT_REVIEW"
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE artifact_versions SET processing_metadata = ? "
                "WHERE version_id = ?",
                (json.dumps(ready_metadata), delivery["version_id"]),
            )
        self.assertEqual(self.service.reconcile_legacy_collaborator_candidates(), 1)
        normalized = self.db.one(
            "SELECT processing_metadata FROM artifact_versions WHERE version_id = ?",
            (delivery["version_id"],),
        )
        self.assertEqual(
            json.loads(normalized["processing_metadata"])["context_manifest"][
                "purpose"
            ],
            "CONTRIBUTION_ANALYSIS",
        )
        self.assertEqual(self.service.reconcile_legacy_collaborator_candidates(), 0)

    def test_open_assistance_suppresses_generic_inquiry_and_p0_has_no_l3(self) -> None:
        action_id = self.actions[0]["action_item_id"]
        requester = self.claims[0]["owner_actor_id"]
        target = self.claims[1]["owner_actor_id"]
        request = self.service.request_assistance(
            action_id,
            actor_id=requester,
            target_actor_id=target,
            category="DEPENDENCY",
            summary="等待对方给输入",
            message_id="policy-help-open",
        )
        self.service.advance_time(
            (parse_time(self.service.now()) + timedelta(hours=2)).isoformat()
        )
        decisions = self.service.evaluate_policy()
        self.assertFalse(
            any(
                decision.get("intervention_id")
                and self.db.one(
                    "SELECT action_item_id FROM interventions "
                    "WHERE intervention_id = ?",
                    (decision["intervention_id"],),
                )["action_item_id"]
                == action_id
                for decision in decisions
            )
        )
        self.assertEqual(
            self.db.one(
                "SELECT COUNT(*) AS count FROM audit_events "
                "WHERE aggregate_id = ? AND event_type = 'ProgressInquirySuppressed'",
                (action_id,),
            )["count"],
            1,
        )
        report = build_report(
            self.db,
            self.service.fixture,
            episode_id=self.service.episode_id,
            run_id=self.service.run_id,
        )
        self.assertEqual(
            report["effect_signals"]["SIG-INQUIRY-001"][
                "assistance_suppressed"
            ],
            1,
        )
        self.service.update_assistance(
            request["assistance_request_id"],
            actor_id=requester,
            action="CANCEL",
            message_id="policy-help-cancel",
        )
        l1 = self.service.evaluate_policy()
        action_l1 = [
            decision
            for decision in l1
            if decision.get("level") == "L1" and not decision.get("suppressed")
        ]
        self.assertTrue(action_l1)
        self.service.dispatch_all(session_id="signal-policy-dispatcher")

        promised_by = parse_time(self.claims[0]["promised_deadline_sim_time"])
        self.service.advance_time((promised_by + timedelta(hours=2)).isoformat())
        l2 = self.service.evaluate_policy()
        self.assertTrue(
            any(
                decision.get("level") == "L2" and not decision.get("suppressed")
                for decision in l2
            )
        )
        self.service.dispatch_all(session_id="signal-policy-dispatcher")
        self.service.advance_time((promised_by + timedelta(days=1)).isoformat())
        later = self.service.evaluate_policy()
        self.assertFalse(any(decision.get("level") == "L3" for decision in later))
        self.assertIsNone(self.service.pending_approval("L3_INTERVENTION"))

    def test_check_window_signal_expiry_cooldown_and_daily_budget(self) -> None:
        action_id = self.actions[0]["action_item_id"]
        owner = self.claims[0]["owner_actor_id"]

        # With a one-hour lead, the task is still too far from its deadline.
        self.service.fixture["policy"]["check_in_lead_hours"] = 1
        self.service.advance_time(
            (parse_time(self.service.now()) + timedelta(hours=2)).isoformat()
        )
        far_decisions = self.service.evaluate_policy()
        self.assertFalse(
            any(
                decision.get("intervention_id")
                and self.db.one(
                    "SELECT action_item_id FROM interventions "
                    "WHERE intervention_id = ?",
                    (decision["intervention_id"],),
                )["action_item_id"]
                == action_id
                for decision in far_decisions
            )
        )

        # Enter a deterministic same-day window.  One delivered L1 consumes the
        # configured per-person budget; a fresh signal suppresses until expiry,
        # and a later eligible L1 candidate is then budget-suppressed.
        self.service.fixture["policy"]["check_in_lead_hours"] = 96
        self.service.fixture["policy"]["daily_touch_budget"] = 1
        self.service.fixture["policy"]["inquiry_cooldown_hours"] = 2
        current = parse_time(self.service.now())
        window_start = (current + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        self.service.advance_time(window_start.isoformat())
        first = self.service.evaluate_policy()
        first_for_action = next(
            decision
            for decision in first
            if decision.get("intervention_id")
            and self.db.one(
                "SELECT action_item_id FROM interventions WHERE intervention_id = ?",
                (decision["intervention_id"],),
            )["action_item_id"]
            == action_id
        )
        self.assertEqual(first_for_action["level"], "L1")
        self.service.dispatch_all(session_id="budget-policy-dispatcher")
        signal_time = window_start + timedelta(minutes=15)
        self.service.advance_time(signal_time.isoformat())
        self.service.record_progress_signal(
            action_id,
            actor_id=owner,
            signal_type="ON_TRACK",
            message_id="budget-fresh-signal",
        )
        self.service.advance_time(
            (signal_time + timedelta(minutes=30)).isoformat()
        )
        self.service.evaluate_policy()
        self.assertEqual(
            self.db.one(
                "SELECT COUNT(*) AS count FROM interventions "
                "WHERE action_item_id = ?",
                (action_id,),
            )["count"],
            1,
        )
        self.service.advance_time(
            (signal_time + timedelta(hours=1, minutes=30)).isoformat()
        )
        cooldown = self.service.evaluate_policy()
        self.assertFalse(any(decision.get("intervention_id") for decision in cooldown))
        self.assertEqual(
            self.db.one(
                "SELECT COUNT(*) AS count FROM audit_events "
                "WHERE aggregate_id = ? AND event_type = 'ProgressInquirySuppressed'",
                (action_id,),
            )["count"],
            1,
        )
        self.service.advance_time((signal_time + timedelta(hours=3)).isoformat())
        suppressed = self.service.evaluate_policy()
        self.assertTrue(
            any(
                decision.get("suppressed") and decision.get("level") == "L1"
                for decision in suppressed
            )
        )
        self.assertEqual(
            self.db.one(
                "SELECT COUNT(*) AS count FROM interventions "
                "WHERE action_item_id = ?",
                (action_id,),
            )["count"],
            1,
        )
        self.assertEqual(
            self.db.one(
                "SELECT COUNT(*) AS count FROM audit_events "
                "WHERE aggregate_id = ? AND event_type = 'InterventionSuppressed'",
                (action_id,),
            )["count"],
            1,
        )
        report = build_report(
            self.db,
            self.service.fixture,
            episode_id=self.service.episode_id,
            run_id=self.service.run_id,
        )
        inquiry = report["effect_signals"]["SIG-INQUIRY-001"]
        self.assertEqual(inquiry["cooldown_suppressed"], 1)
        self.assertEqual(inquiry["budget_suppressed"], 1)

    def test_pending_acceptance_suppresses_progress_inquiry(self) -> None:
        action_id = self.actions[0]["action_item_id"]
        owner = self.claims[0]["owner_actor_id"]
        submitted = self.service.submit_artifact(
            action_id,
            actor_id=owner,
            message_id="pending-acceptance-submit",
            payload={
                "summary": "已提交待验收成果",
                "content": "负责人尚未执行验收。",
            },
        )
        self.assertEqual(submitted["validation_status"], "PASSED")
        self.assertEqual(self.service.action(action_id)["status"], "PENDING_ACCEPTANCE")
        self.service.advance_time(
            (parse_time(self.service.now()) + timedelta(days=4)).isoformat()
        )
        self.service.evaluate_policy()
        self.assertEqual(
            self.db.one(
                "SELECT COUNT(*) AS count FROM interventions "
                "WHERE action_item_id = ? AND action_type = 'PROGRESS_INQUIRY'",
                (action_id,),
            )["count"],
            0,
        )


if __name__ == "__main__":
    unittest.main()
