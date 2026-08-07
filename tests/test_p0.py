from __future__ import annotations

import json
import copy
import tempfile
import unittest
from pathlib import Path

from collab_agent.evaluation import run_p0_evaluation
from collab_agent.content_pack import ContentPackError, validate_content_pack
from collab_agent.metrics import build_report
from collab_agent.models import effect_id
from collab_agent.service import CoordinationService, load_fixture
from collab_agent.store import Database


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "p0_weekly.json"


class P0EvaluationTests(unittest.TestCase):
    def test_layered_report_contract_and_zero_denominator(self) -> None:
        fixture = load_fixture(FIXTURE)
        database = Database(":memory:")
        database.initialize()
        service = CoordinationService(database, fixture)
        service.bootstrap()

        report = build_report(database, fixture)
        self.assertTrue(report["report_valid"])
        self.assertEqual(
            set(report["gate_summary"]),
            {
                "GATE-E2E-001",
                "GATE-DUP-001",
                "GATE-VER-001",
                "GATE-GUARD-001",
                "GATE-REC-001",
            },
        )
        self.assertEqual(
            set(report["flow_signals"]),
            {
                "SIG-FUNNEL-001",
                "SIG-ONTIME-001",
                "SIG-SCHEDULE-001",
                "SIG-LATENCY-001",
            },
        )
        self.assertEqual(
            set(report["effect_signals"]),
            {
                "SIG-SIGNAL-001",
                "SIG-INQUIRY-001",
                "SIG-HELP-001",
                "SIG-TOUCH-001",
                "SIG-PROGRESS-001",
                "SIG-ESC-001",
            },
        )
        self.assertEqual(
            set(report["node_signals"]),
            {
                "SIG-AUTH-001",
                "SIG-CONTEXT-001",
                "SIG-EXTRACT-001",
                "SIG-VALIDATE-001",
                "SIG-RESULT-001",
                "SIG-MEM-001",
                "SIG-APPROVAL-001",
                "SIG-OUTBOX-001",
            },
        )
        empty_rate = report["node_signals"]["SIG-VALIDATE-001"][
            "first_validation_pass_rate"
        ]
        self.assertEqual(empty_rate["rate"], "N/A")
        self.assertEqual(empty_rate["denominator"], 0)
        self.assertTrue(empty_rate["reason"])
        database.close()

    def test_content_pack_rejects_non_owner_assignment(self) -> None:
        fixture = load_fixture(FIXTURE)
        invalid = copy.deepcopy(fixture)
        invalid["action_items"][0]["owner_actor_id"] = "owner_lead"
        with self.assertRaisesRegex(ContentPackError, "not a PARTICIPANT"):
            validate_content_pack(invalid)

    def test_effect_id_is_deterministic_and_trigger_sensitive(self) -> None:
        common = {
            "episode_id": "episode",
            "subject_id": "action",
            "effect_type": "REMINDER",
        }
        first = effect_id(**common, trigger_key="deadline:1")
        self.assertEqual(first, effect_id(**common, trigger_key="deadline:1"))
        self.assertNotEqual(first, effect_id(**common, trigger_key="deadline:2"))

    def test_inbound_confirmation_is_idempotent(self) -> None:
        fixture = copy.deepcopy(load_fixture(FIXTURE))
        # Compatibility coverage for an older upstream that already assigned
        # one owner; the authoritative P0 fixture itself remains claim-first.
        fixture["action_items"][0]["owner_actor_id"] = "owner_a"
        database = Database(":memory:")
        database.initialize()
        service = CoordinationService(database, fixture)
        service.bootstrap()
        first = service.confirm_action(
            "ai_feedback", actor_id="owner_a", message_id="same_message"
        )
        second = service.confirm_action(
            "ai_feedback", actor_id="owner_a", message_id="same_message"
        )
        self.assertEqual(first, second)
        count = database.one(
            "SELECT COUNT(*) AS count FROM commitment_revisions WHERE action_item_id = ?",
            ("ai_feedback",),
        )["count"]
        self.assertEqual(count, 1)
        database.close()

    def test_only_assigned_owner_can_reject_pending_confirmation(self) -> None:
        fixture = copy.deepcopy(load_fixture(FIXTURE))
        fixture["action_items"][0]["owner_actor_id"] = "owner_a"
        database = Database(":memory:")
        database.initialize()
        service = CoordinationService(database, fixture)
        service.bootstrap()
        with self.assertRaises(PermissionError):
            service.reject_action(
                "ai_feedback",
                actor_id="owner_b",
                reason="不是我的任务",
                message_id="reject-wrong-owner",
            )
        rejected = service.reject_action(
            "ai_feedback",
            actor_id="owner_a",
            reason="会议中责任归属有误",
            message_id="reject-owner",
        )
        repeated = service.reject_action(
            "ai_feedback",
            actor_id="owner_a",
            reason="会议中责任归属有误",
            message_id="reject-owner",
        )
        self.assertEqual(rejected, repeated)
        action = service.action("ai_feedback")
        self.assertEqual(action["status"], "REJECTED")
        self.assertTrue(action["required"])
        database.close()

    def test_full_p0_closes_with_recovery_and_latest_version_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "p0.sqlite3"
            report, service = run_p0_evaluation(database_path, FIXTURE)
            self.assertTrue(report["passed"], report)
            self.assertTrue(
                all(gate["passed"] for gate in report["gates"].values()), report
            )
            funnel = report["flow_signals"]["SIG-FUNNEL-001"]["counts"]
            for stage in (
                "proposed",
                "published",
                "claimed",
                "submitted",
                "pending_acceptance",
                "accepted",
                "aggregated",
                "archived",
            ):
                self.assertEqual(funnel[stage], 4, (stage, report))
            schedule = report["flow_signals"]["SIG-SCHEDULE-001"]
            self.assertGreaterEqual(schedule["detected_conflicts"], 1)
            self.assertGreaterEqual(schedule["resolved_conflicts"], 1)
            self.assertEqual(schedule["active_conflicts"], 0)
            inquiry = report["effect_signals"]["SIG-INQUIRY-001"]
            self.assertGreaterEqual(inquiry["created_by_level"]["L1"], 1)
            self.assertGreaterEqual(inquiry["created_by_level"]["L2"], 1)
            help_signal = report["effect_signals"]["SIG-HELP-001"]
            self.assertEqual(help_signal["created"], 1)
            self.assertEqual(help_signal["acknowledged"], 1)
            self.assertEqual(help_signal["resolved"], 1)
            result_signal = report["node_signals"]["SIG-RESULT-001"]
            self.assertGreaterEqual(
                result_signal["attachment_extraction_statuses"]["EXTRACTED"], 2
            )
            self.assertGreaterEqual(result_signal["returned_for_revision"], 1)
            memory_signal = report["node_signals"]["SIG-MEM-001"]
            self.assertGreaterEqual(memory_signal["confirmed"], 1)
            self.assertGreaterEqual(memory_signal["replaced"], 1)
            self.assertGreaterEqual(memory_signal["rejected"], 1)
            self.assertGreaterEqual(
                memory_signal["prohibited_label_attempts_blocked"], 1
            )
            self.assertGreaterEqual(
                report["node_signals"]["SIG-AUTH-001"][
                    "authorization_rejections"
                ],
                1,
            )
            context_signal = report["node_signals"]["SIG-CONTEXT-001"]
            self.assertGreaterEqual(context_signal["call_starts"], 1)
            self.assertEqual(context_signal["manifest_complete_rate"]["rate"], 1.0)
            self.assertEqual(context_signal["principal_violation_count"], 0)
            self.assertEqual(context_signal["field_violation_count"], 0)
            self.assertEqual(context_signal["binary_leak_count"], 0)
            self.assertEqual(context_signal["input_hash_mismatch_count"], 0)
            approval_statuses = report["node_signals"]["SIG-APPROVAL-001"][
                "status_counts"
            ]
            self.assertGreaterEqual(approval_statuses["SUPERSEDED"], 1)
            self.assertEqual(approval_statuses["EXECUTED"], 1)
            self.assertEqual(
                report["gates"]["G5_critical_recovery"]["evidence"][
                    "deduplicated_deliveries"
                ],
                1,
            )
            self.assertGreaterEqual(
                service.db.one(
                    "SELECT COUNT(*) AS count FROM audit_events "
                    "WHERE event_type = 'DailyBudgetReset'"
                )["count"],
                3,
            )

            action = service.action("ai_release_notes")
            current = service.db.one(
                "SELECT * FROM artifact_versions WHERE version_id = ?",
                (action["current_valid_version_id"],),
            )
            self.assertEqual(json.loads(current["payload"])["version"], "1.0")
            lineage_versions = {
                row["version_id"]
                for row in service.db.all(
                    "SELECT * FROM final_field_lineage WHERE action_item_id = ?",
                    ("ai_release_notes",),
                )
            }
            self.assertEqual(lineage_versions, {current["version_id"]})

            invalid = service.db.one(
                "SELECT * FROM artifact_versions WHERE source_message_id = ?",
                ("in_training_invalid",),
            )
            lineage_count = service.db.one(
                "SELECT COUNT(*) AS count FROM final_field_lineage WHERE version_id = ?",
                (invalid["version_id"],),
            )["count"]
            self.assertEqual(lineage_count, 0)

            version_count = service.db.one(
                "SELECT COUNT(*) AS count FROM artifact_versions"
            )["count"]
            late = service.submit_artifact(
                "ai_feedback",
                actor_id="owner_a",
                message_id="late-after-archive",
                payload={"categories": ["不应写入"], "priorities": {}},
            )
            repeated_late = service.submit_artifact(
                "ai_feedback",
                actor_id="owner_a",
                message_id="late-after-archive",
                payload={"categories": ["不应写入"], "priorities": {}},
            )
            self.assertEqual(late, repeated_late)
            self.assertTrue(late["ignored"])
            self.assertEqual(late["reason"], "episode_archived")
            self.assertEqual(
                service.db.one("SELECT COUNT(*) AS count FROM artifact_versions")[
                    "count"
                ],
                version_count,
            )
            self.assertEqual(service.action("ai_feedback")["status"], "ARCHIVED")
            service.db.close()

    def test_guard_gate_detects_an_incomplete_invocation_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "p0-context-guard.sqlite3"
            report, service = run_p0_evaluation(database_path, FIXTURE)
            self.assertTrue(report["passed"])
            event = service.db.one(
                "SELECT event_id, payload FROM audit_events "
                "WHERE event_type = 'TaskResultProcessingSucceeded' "
                "ORDER BY sequence_no LIMIT 1"
            )
            payload = json.loads(event["payload"])
            payload["invocation"].pop("purpose")
            with service.db.transaction() as cursor:
                cursor.execute(
                    "UPDATE audit_events SET payload = ? WHERE event_id = ?",
                    (json.dumps(payload, ensure_ascii=False), event["event_id"]),
                )
            corrupted = build_report(service.db, service.fixture)
            guard = corrupted["gate_summary"]["GATE-GUARD-001"]
            self.assertFalse(guard["passed"])
            self.assertEqual(
                guard["evidence"]["incomplete_invocation_manifest_count"], 1
            )
            self.assertIn(
                event["event_id"],
                guard["evidence"]["invalid_invocation_event_ids"],
            )
            service.db.close()


if __name__ == "__main__":
    unittest.main()
