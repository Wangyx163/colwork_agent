from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from collab_agent.meeting import load_meeting_service
from collab_agent.store import Database
from collab_agent.web import workbench_state


class ReviewHintFlowTests(unittest.TestCase):
    def test_hint_is_not_a_task_until_coordinator_materializes_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "meeting.txt"
            extraction = root / "extraction.json"
            transcript.write_text(
                "主持人(00:00:01): 那个方案明天下午。\n", encoding="utf-8"
            )
            extraction.write_text(
                json.dumps(
                    {
                        "provider": "bailian",
                        "model": "qwen-plus",
                        "input_sha256": "b" * 64,
                        "action_items": [],
                        "review_hints": [
                            {
                                "hint_id": "hint_one",
                                "candidate_id": "rc_one",
                                "source_timestamp": "00:00:01",
                                "source_quote": "那个方案明天下午。",
                                "evidence_text": "那个方案明天下午。",
                                "reason_code": "MINIMUM_SEMANTICS_UNRESOLVED",
                                "evidence_unit_ids": ["u000000.000"],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            database = Database(":memory:")
            database.initialize()
            try:
                service = load_meeting_service(
                    database,
                    extraction_path=extraction,
                    transcript_path=transcript,
                    organization_name="测试团队",
                    coordinator_name="负责人",
                    participant_names=["小王"],
                    timezone="UTC",
                )
                state = workbench_state(service)
                self.assertEqual(state["tasks"], [])
                self.assertEqual(len(state["review_hints"]), 1)
                self.assertEqual(state["review_hints"][0]["status"], "OPEN")

                created = service.materialize_review_hint(
                    "hint_one",
                    actor_id=service.aggregator_actor_id,
                    title="确认并提交方案",
                    deliverable="一份确认后的方案文档",
                    acceptance_criteria="负责人可直接评审",
                    priority="P1",
                    message_id="msg_hint_one",
                )
                self.assertTrue(created["action_item_id"].startswith("ai_"))
                tasks = list(service.action_items())
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0]["status"], "PENDING_CONFIRMATION")
                hint = database.one(
                    "SELECT * FROM review_hints WHERE hint_id = ?", ("hint_one",)
                )
                self.assertEqual(hint["status"], "MATERIALIZED")
                self.assertEqual(
                    hint["materialized_action_item_id"], created["action_item_id"]
                )

                repeated = service.materialize_review_hint(
                    "hint_one",
                    actor_id=service.aggregator_actor_id,
                    title="ignored on idempotent replay",
                    deliverable="ignored",
                    acceptance_criteria="",
                    priority="P2",
                    message_id="msg_hint_one",
                )
                self.assertEqual(repeated, created)
                self.assertEqual(len(list(service.action_items())), 1)
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
