from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from collab_agent.agent_worker import AgentWorker
from collab_agent.meeting import load_meeting_service
from collab_agent.models import parse_time
from collab_agent.store import Database


class AgentWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.database_path = root / "meeting.sqlite3"
        self.extraction = root / "extraction.json"
        self.transcript = root / "transcript.txt"
        self.extraction.write_text(
            json.dumps(
                {
                    "provider": "test",
                    "model": "test",
                    "input_sha256": "d" * 64,
                    "action_items": [
                        {
                            "title": "Prepare demo",
                            "deliverable": "Interview demo",
                            "owner_name": None,
                            "deadline_text": None,
                            "deadline_iso": None,
                            "source_timestamp": "00:01:00",
                            "source_quote": "Please prepare the interview demo",
                            "confidence": 0.95,
                            "needs_confirmation": True,
                            "uncertainties": ["owner is not explicit"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.transcript.write_text(
            "Coordinator (00:01:00): Please prepare the interview demo\n",
            encoding="utf-8",
        )
        self.database, self.service = self._open_service()
        action = next(iter(self.service.action_items()))
        deadline = (parse_time(self.service.now()) + timedelta(days=2)).isoformat()
        self.service.revise_action_proposal(
            action["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            title=action["title"],
            deliverable="Interview demo",
            acceptance_criteria="Shows state and recovery",
            priority="P0",
            team_required_by_sim_time=deadline,
            message_id="revise-demo",
        )
        self.service.publish_action(
            action["action_item_id"],
            actor_id=self.service.aggregator_actor_id,
            message_id="publish-demo",
        )
        claim = self.service.claim_action(
            action["action_item_id"],
            claimant_name="Alice",
            promised_deadline_sim_time=deadline,
            message_id="claim-demo",
        )
        submitted = self.service.submit_artifact(
            action["action_item_id"],
            actor_id=claim["owner_actor_id"],
            message_id="submit-demo",
            payload={
                "summary": "Demo is ready",
                "content": "The demo shows durable state and recovery.",
                "links": [],
                "files": [],
            },
        )
        self.version_id = submitted["version_id"]

    def tearDown(self) -> None:
        self.database.close()
        self.directory.cleanup()

    def _open_service(self) -> tuple[Database, object]:
        database = Database(self.database_path)
        database.initialize()
        service = load_meeting_service(
            database,
            extraction_path=self.extraction,
            transcript_path=self.transcript,
            organization_name="Interview team",
            coordinator_name="Coordinator",
            participant_names=["Alice"],
        )
        return database, service

    def test_run_until_idle_processes_once_without_duplicates(self) -> None:
        worker = AgentWorker(self.service, processing_mode="local", session_id="w1")

        first = worker.run_until_idle()
        second = worker.run_until_idle()

        self.assertEqual(first["status"], "IDLE")
        self.assertEqual(first["step_count"], 1)
        self.assertEqual(first["steps"][0]["kind"], "TASK_RESULT")
        self.assertEqual(second["step_count"], 0)
        row = self.database.one(
            "SELECT processing_status, processing_attempt_count "
            "FROM artifact_versions WHERE version_id = ?",
            (self.version_id,),
        )
        self.assertEqual(row["processing_status"], "READY")
        self.assertEqual(int(row["processing_attempt_count"]), 1)

    def test_restart_recovers_interrupted_processing(self) -> None:
        with self.database.transaction() as cursor:
            cursor.execute(
                "UPDATE artifact_versions SET processing_status = 'PROCESSING', "
                "processing_attempt_count = 1 WHERE version_id = ?",
                (self.version_id,),
            )
        self.database.close()
        self.database, self.service = self._open_service()
        worker = AgentWorker(self.service, processing_mode="local", session_id="w2")

        recovery = worker.recover()
        result = worker.run_until_idle()

        self.assertEqual(recovery["task_results_requeued"], [self.version_id])
        self.assertEqual(result["step_count"], 1)
        row = self.database.one(
            "SELECT processing_status, processing_attempt_count "
            "FROM artifact_versions WHERE version_id = ?",
            (self.version_id,),
        )
        self.assertEqual(row["processing_status"], "READY")
        self.assertEqual(int(row["processing_attempt_count"]), 2)
        event = self.database.one(
            "SELECT event_type FROM audit_events WHERE aggregate_id = ? "
            "AND event_type = 'TaskResultProcessingRecovered'",
            (self.version_id,),
        )
        self.assertIsNotNone(event)


if __name__ == "__main__":
    unittest.main()
