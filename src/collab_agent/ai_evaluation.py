from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .extraction import validate_extraction, validate_source_evidence
from .models import read_text_file
from .service import CoordinationService


def _normalized_title(value: Any) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "").casefold())


def _rate(numerator: int, denominator: int) -> float | str:
    return round(numerator / denominator, 3) if denominator else "N/A"


def score_extraction_cases(payload: dict[str, Any]) -> dict[str, Any]:
    true_positive = 0
    expected_count = 0
    predicted_count = 0
    matched_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    invalid_cases: list[dict[str, str]] = []

    for case in payload.get("cases") or []:
        expected = list(case.get("expected") or [])
        predicted = list(case.get("prediction") or [])
        expected_count += len(expected)
        predicted_count += len(predicted)
        try:
            validated_items = validate_extraction({"action_items": predicted})
            validate_source_evidence(
                validated_items, str(case.get("transcript") or "")
            )
            validated = [asdict(item) for item in validated_items]
        except Exception as error:
            invalid_cases.append(
                {"case_id": str(case.get("case_id")), "error": str(error)}
            )
            continue

        remaining = list(validated)
        for expected_item in expected:
            expected_title = _normalized_title(expected_item.get("title"))
            matched_index = next(
                (
                    index
                    for index, item in enumerate(remaining)
                    if _normalized_title(item.get("title")) == expected_title
                ),
                None,
            )
            if matched_index is None:
                continue
            predicted_item = remaining.pop(matched_index)
            true_positive += 1
            matched_pairs.append((expected_item, predicted_item))

    precision = _rate(true_positive, predicted_count)
    recall = _rate(true_positive, expected_count)
    f1 = (
        round(2 * precision * recall / (precision + recall), 3)
        if isinstance(precision, float)
        and isinstance(recall, float)
        and precision + recall
        else "N/A"
    )

    def field_accuracy(*fields: str) -> float | str:
        correct = sum(
            all(expected.get(field) == predicted.get(field) for field in fields)
            for expected, predicted in matched_pairs
        )
        return _rate(correct, len(matched_pairs))

    metrics = {
        "task_precision": precision,
        "task_recall": recall,
        "task_f1": f1,
        "owner_accuracy": field_accuracy("owner_name"),
        "deadline_accuracy": field_accuracy("deadline_iso"),
        "evidence_accuracy": field_accuracy("source_timestamp", "source_quote"),
        "confirmation_accuracy": field_accuracy("needs_confirmation"),
        "expected_count": expected_count,
        "predicted_count": predicted_count,
        "matched_count": true_positive,
        "invalid_cases": invalid_cases,
    }
    numeric = [
        metrics["task_f1"],
        metrics["owner_accuracy"],
        metrics["deadline_accuracy"],
        metrics["evidence_accuracy"],
        metrics["confirmation_accuracy"],
    ]
    metrics["passed"] = not invalid_cases and all(
        isinstance(value, float) and value >= 0.9 for value in numeric
    )
    return metrics


def _audit_payload(row: Any) -> dict[str, Any]:
    payload = row["payload"]
    return json.loads(payload) if isinstance(payload, str) else dict(payload)


def build_ai_p0_report(
    service: CoordinationService,
    workflow_report: dict[str, Any],
    *,
    extraction_cases_path: str | Path,
) -> dict[str, Any]:
    extraction_cases = json.loads(
        read_text_file(extraction_cases_path)
    )
    extraction = score_extraction_cases(extraction_cases)
    gates = workflow_report["gate_summary"]

    agent_steps = service.db.all(
        "SELECT payload FROM audit_events WHERE run_id = ? "
        "AND event_type = 'AgentStepCompleted' ORDER BY sequence_no",
        (service.run_id,),
    )
    step_payloads = [_audit_payload(row) for row in agent_steps]
    workflow = {
        "passed": all(
            gates[name]["passed"]
            for name in ("GATE-E2E-001", "GATE-VER-001", "GATE-GUARD-001")
        )
        and bool(step_payloads),
        "agent_step_count": len(step_payloads),
        "agent_step_kinds": sorted(
            {str(payload.get("step_kind")) for payload in step_payloads}
        ),
        "human_acceptance_decisions": int(
            service.db.one(
                "SELECT COUNT(*) AS count FROM artifact_versions "
                "WHERE review_status IN ('ACCEPTED','REJECTED')"
            )["count"]
        ),
        "latest_version_lineage_gate": gates["GATE-VER-001"],
        "authorization_and_hitl_gate": gates["GATE-GUARD-001"],
        "end_to_end_gate": gates["GATE-E2E-001"],
    }

    recovery_events = service.db.all(
        "SELECT event_type FROM audit_events WHERE run_id = ? "
        "AND event_type IN ('ProcessRecovered','TaskResultProcessingRecovered')",
        (service.run_id,),
    )
    recovery = {
        "passed": gates["GATE-REC-001"]["passed"]
        and gates["GATE-DUP-001"]["passed"]
        and bool(recovery_events),
        "recovery_event_count": len(recovery_events),
        "recovery_gate": gates["GATE-REC-001"],
        "idempotency_gate": gates["GATE-DUP-001"],
    }

    started = service.db.all(
        "SELECT payload FROM audit_events WHERE run_id = ? "
        "AND event_type = 'TaskResultProcessingStarted' ORDER BY sequence_no",
        (service.run_id,),
    )
    manifests: list[dict[str, Any]] = []
    for row in started:
        invocation = _audit_payload(row).get("invocation") or {}
        context = invocation.get("context")
        if isinstance(context, dict):
            manifests.append(context)
    within_budget = [
        int(item.get("estimated_input_tokens", -1))
        <= int(item.get("token_budget", -2))
        for item in manifests
    ]
    context = {
        "passed": len(manifests) == len(started)
        and bool(manifests)
        and all(within_budget),
        "invocation_count": len(started),
        "manifest_count": len(manifests),
        "within_budget_count": sum(within_budget),
        "estimated_input_tokens_total": sum(
            int(item.get("estimated_input_tokens", 0)) for item in manifests
        ),
        "truncated_invocation_count": sum(
            bool(item.get("truncated")) for item in manifests
        ),
        "estimator": (
            manifests[0].get("estimator") if manifests else None
        ),
        "note": "provider-reported usage remains attached to success audits; this gate is the pre-call budget guard",
    }

    groups = {
        "structured_fact_extraction": extraction,
        "agent_workflow_hitl_lineage": workflow,
        "recovery_and_idempotency": recovery,
        "context_and_token_management": context,
    }
    return {
        "schema_version": "ai-p0-harness.v1",
        "passed": all(group["passed"] for group in groups.values()),
        "execution_profile": {
            "external_model_calls": 0,
            "mode": "OFFLINE_REGRESSION",
            "reason": "routine regression must not spend model tokens; real Bailian contract smoke is a separate deliberate test",
        },
        "groups": groups,
        "workflow_report_ref": {
            "schema_version": workflow_report["schema_version"],
            "run_id": workflow_report["run_id"],
            "passed": workflow_report["passed"],
        },
    }
