from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from .result_processing import BailianResultOrganizer
from .service import TASK_RESULT_MAX_ATTEMPTS, CoordinationService


class AgentWorker:
    """Durable single-agent loop over externally stored workflow state."""

    MAX_TASK_RESULT_ATTEMPTS = TASK_RESULT_MAX_ATTEMPTS

    def __init__(
        self,
        service: CoordinationService,
        *,
        processing_mode: str = "local",
        allow_contribution_analysis: bool = False,
        session_id: str | None = None,
    ) -> None:
        if processing_mode not in {"bailian", "local"}:
            raise ValueError("Agent Worker mode must be bailian or local")
        self.service = service
        self.processing_mode = processing_mode
        self.allow_contribution_analysis = bool(allow_contribution_analysis)
        self.session_id = session_id or f"agent_worker_{uuid4().hex}"
        self.organizer = (
            BailianResultOrganizer().organize
            if processing_mode == "bailian"
            else None
        )

    def recover(self) -> dict[str, Any]:
        """Requeue work left transient by a previous process interruption."""

        sim_time = self.service.now()
        recovered_versions: list[str] = []
        exhausted_versions: list[str] = []
        with self.service.db.transaction() as cursor:
            rows = cursor.execute(
                "SELECT v.version_id, v.processing_attempt_count "
                "FROM artifact_versions v "
                "JOIN action_items a ON a.action_item_id = v.action_item_id "
                "WHERE a.episode_id = ? AND v.processing_status = 'PROCESSING' "
                "ORDER BY v.received_sequence, v.version_id",
                (self.service.episode_id,),
            ).fetchall()
            for row in rows:
                exhausted = (
                    int(row["processing_attempt_count"])
                    >= self.MAX_TASK_RESULT_ATTEMPTS
                )
                status = "FAILED" if exhausted else "RETRY_WAIT"
                error_code = (
                    "CRASH_RECOVERY_EXHAUSTED"
                    if exhausted
                    else "WORKER_INTERRUPTED"
                )
                changed = cursor.execute(
                    "UPDATE artifact_versions SET processing_status = ?, "
                    "processing_error_code = ?, "
                    "processing_error_stage = 'AGENT_WORKER_RECOVERY', "
                    "processing_error_detail = 'Recovered transient PROCESSING state', "
                    "processing_retryable = ?, processing_updated_sim_time = ?, "
                    # An interrupted attempt never reached the provider, so it
                    # should be picked up again immediately rather than serve a
                    # backoff it did not earn.
                    "processing_next_attempt_at = NULL "
                    "WHERE version_id = ? AND processing_status = 'PROCESSING'",
                    (
                        status,
                        error_code,
                        not exhausted,
                        sim_time,
                        row["version_id"],
                    ),
                ).rowcount
                if not changed:
                    continue
                target = exhausted_versions if exhausted else recovered_versions
                target.append(row["version_id"])
                self.service.db.append_audit(
                    cursor,
                    run_id=self.service.run_id,
                    aggregate_type="ArtifactVersion",
                    aggregate_id=row["version_id"],
                    event_type="TaskResultProcessingRecovered",
                    sim_time=sim_time,
                    payload={
                        "session_id": self.session_id,
                        "attempt_count": int(row["processing_attempt_count"]),
                        "resulting_status": status,
                        "error_code": error_code,
                    },
                    correlation_id=f'corr_task_result_{row["version_id"]}',
                )

        recovered_outbox = self.service.recover_dispatcher(self.session_id)
        return {
            "session_id": self.session_id,
            "task_results_requeued": recovered_versions,
            "task_results_exhausted": exhausted_versions,
            "outbox_entries_requeued": recovered_outbox,
        }

    def _record_step(self, kind: str, result: dict[str, Any]) -> None:
        aggregate_id = str(
            result.get("version_id")
            or result.get("final_deliverable_id")
            or self.service.episode_id
        )
        with self.service.db.transaction() as cursor:
            self.service.db.append_audit(
                cursor,
                run_id=self.service.run_id,
                aggregate_type="AgentRun",
                aggregate_id=aggregate_id,
                event_type="AgentStepCompleted",
                sim_time=self.service.now(),
                payload={
                    "session_id": self.session_id,
                    "step_kind": kind,
                    "result_ref": aggregate_id,
                    "result_status": result.get("processing_status")
                    or result.get("status"),
                },
                correlation_id=f"corr_agent_step_{aggregate_id}",
            )

    def run_once(self) -> dict[str, Any]:
        task_result = self.service.process_task_result_once(
            processing_mode=self.processing_mode,
            allow_contribution_analysis=self.allow_contribution_analysis,
        )
        if task_result is not None:
            self._record_step("TASK_RESULT", task_result)
            return {"kind": "TASK_RESULT", "result": task_result}

        queued_outbox_id = self.service.queue_final_organization(
            processing_mode=self.processing_mode
        )
        final_result = self.service.dispatch_final_organization_once(
            session_id=self.session_id,
            organizer=self.organizer,
        )
        if final_result is not None:
            self._record_step("FINAL_ORGANIZATION", final_result)
            return {
                "kind": "FINAL_ORGANIZATION",
                "queued_outbox_id": queued_outbox_id,
                "result": final_result,
            }
        return {"kind": "IDLE"}

    def run_until_idle(self, *, max_steps: int = 100) -> dict[str, Any]:
        if max_steps <= 0:
            raise ValueError("max_steps must be positive")
        steps: list[dict[str, Any]] = []
        for _ in range(max_steps):
            result = self.run_once()
            if result["kind"] == "IDLE":
                return {
                    "status": "IDLE",
                    "session_id": self.session_id,
                    "step_count": len(steps),
                    "steps": steps,
                }
            steps.append(result)
        return {
            "status": "STEP_LIMIT",
            "session_id": self.session_id,
            "step_count": len(steps),
            "steps": steps,
        }

    def run_forever(
        self,
        *,
        poll_seconds: float = 2.0,
        on_step: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        while True:
            result = self.run_once()
            if on_step is not None and result["kind"] != "IDLE":
                on_step(result)
            if result["kind"] == "IDLE":
                time.sleep(poll_seconds)
