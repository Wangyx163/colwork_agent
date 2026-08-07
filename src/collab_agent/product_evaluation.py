"""Product-effectiveness metrics, separate from the GATE report.

The GATE report answers "did the workflow hold". This module answers the five
questions an evaluation of the *product* has to answer: quality, human cost,
process safety, recovery, and token cost.

Everything here is derived from facts the system already records -- audit
events, context manifests, provider usage, accepted results. Nothing needs
human annotation, so these numbers can be recomputed on any database at any
time and do not depend on model quality. That independence is the point: they
stay true when the model is swapped or unavailable.

Extraction quality is the one dimension that cannot be measured this way; it
needs labelled meetings and lives in `extraction_evaluation`.
"""

from __future__ import annotations

import json
from typing import Any

from .models import canonical_json, stable_hash


def _decoded(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value if value is not None else fallback


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def review_cost_metrics(db: Any, *, episode_id: str) -> dict[str, Any]:
    """How much human correction the extracted definitions needed.

    This is the most direct product-value signal available without annotation:
    if the coordinator has to rewrite every field, the extraction is not saving
    anyone time regardless of how good its F1 looks.
    """

    proposed = db.all(
        "SELECT COUNT(*) AS count FROM action_items WHERE episode_id = ?",
        (episode_id,),
    )
    total_items = int(proposed[0]["count"]) if proposed else 0
    revisions = db.all(
        "SELECT aggregate_id, payload FROM audit_events "
        "WHERE event_type = 'ActionItemProposalRevised' ORDER BY sequence_no"
    )
    field_edits: dict[str, int] = {}
    edited_items: set[str] = set()
    revisions_without_detail = 0
    for row in revisions:
        payload = _decoded(row["payload"], {}) or {}
        changed = payload.get("changed_fields")
        if changed is None:
            # Written before change tracking existed; counted but not attributed.
            revisions_without_detail += 1
            continue
        if changed:
            edited_items.add(row["aggregate_id"])
        for field in changed:
            field_edits[field] = field_edits.get(field, 0) + 1
    ignored = db.all(
        "SELECT COUNT(*) AS count FROM audit_events "
        "WHERE event_type = 'ActionItemIgnored'"
    )
    merged = db.all(
        "SELECT COUNT(*) AS count FROM audit_events "
        "WHERE event_type = 'ActionItemMerged'"
    )
    returned = db.all(
        "SELECT COUNT(*) AS count FROM audit_events "
        "WHERE event_type = 'ActionItemAssignmentReturned'"
    )
    ignored_count = int(ignored[0]["count"]) if ignored else 0
    merged_count = int(merged[0]["count"]) if merged else 0
    return {
        "extracted_action_items": total_items,
        "revision_events": len(revisions),
        "revision_events_without_change_detail": revisions_without_detail,
        "action_items_edited": len(edited_items),
        "edited_ratio": _rate(len(edited_items), total_items),
        "field_edit_counts": dict(sorted(field_edits.items())),
        "field_edits_per_action_item": _rate(
            sum(field_edits.values()), total_items
        ),
        # A dropped or merged proposal is a false positive the human had to
        # clean up; it costs more than editing a field.
        "ignored_count": ignored_count,
        "merged_count": merged_count,
        "discard_ratio": _rate(ignored_count + merged_count, total_items),
        "assignment_returned_count": (
            int(returned[0]["count"]) if returned else 0
        ),
    }


def citation_fidelity_metrics(db: Any) -> dict[str, Any]:
    """Whether model conclusions cite evidence the system actually read.

    The processor already reports which refs it used and which it could not
    read, so a claim resting on an unread source is detectable without a human
    checking any output.
    """

    rows = db.all(
        "SELECT version_id, processing_result FROM artifact_versions "
        "WHERE processing_status = 'READY' AND processing_result IS NOT NULL"
    )
    checked = 0
    with_unread = 0
    conclusions = 0
    conclusions_on_unread = 0
    offenders: list[dict[str, Any]] = []
    for row in rows:
        result = _decoded(row["processing_result"], {}) or {}
        coverage = result.get("source_coverage") or {}
        used = {str(ref) for ref in coverage.get("used_refs") or []}
        unread = {
            str(ref) for ref in coverage.get("unreadable_or_uninspected") or []
        }
        checked += 1
        if unread:
            with_unread += 1
        digest = result.get("evidence_digest") or {}
        for point in digest.get("key_points") or []:
            if not isinstance(point, dict):
                continue
            conclusions += 1
            refs = {str(ref) for ref in point.get("source_refs") or []}
            # A conclusion may only rest on refs the run actually read.
            violating = refs & unread
            if violating or (refs and not refs <= used | refs):
                conclusions_on_unread += 1
                offenders.append(
                    {
                        "version_id": row["version_id"],
                        "unread_refs_cited": sorted(violating),
                    }
                )
    return {
        "processed_versions_checked": checked,
        "versions_with_unread_sources": with_unread,
        "conclusion_points": conclusions,
        "conclusions_citing_unread_sources": conclusions_on_unread,
        "citation_hallucination_rate": _rate(conclusions_on_unread, conclusions),
        "offending_versions": offenders[:20],
    }


def _walk_invocations(payload: Any):
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            context = node.get("context") or node.get("context_manifest")
            if isinstance(context, dict) and "estimated_input_tokens" in context:
                yield node, context
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def token_accuracy_metrics(db: Any) -> dict[str, Any]:
    """Compare the pre-call estimate against what the provider actually billed.

    A budget guard is only as trustworthy as its estimator. Grouping by the
    recorded `estimator` name is what makes the old and new estimators
    comparable on the same database instead of silently mixed.
    """

    rows = db.all(
        "SELECT payload FROM audit_events WHERE event_type IN "
        "('TaskResultProcessingSucceeded','TaskResultProcessingFailed',"
        "'QuestionBallotDraftPrepared','FinalDeliverableGenerated')"
    )
    by_estimator: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = _decoded(row["payload"], {}) or {}
        for node, context in _walk_invocations(payload):
            usage = node.get("usage") or {}
            actual = usage.get("first_request_prompt_tokens")
            if not isinstance(actual, int):
                actual = usage.get("prompt_tokens")
            estimated = context.get("estimated_input_tokens")
            if not isinstance(estimated, int) or not isinstance(actual, int):
                continue
            if actual <= 0:
                continue
            name = str(context.get("estimator") or "unknown")
            bucket = by_estimator.setdefault(
                name,
                {
                    "samples": 0,
                    "ratios": [],
                    "counts_prompt_overhead": False,
                    "purposes": set(),
                },
            )
            bucket["samples"] += 1
            bucket["ratios"].append(estimated / actual)
            bucket["purposes"].add(str(context.get("purpose") or ""))
            if context.get("reserved_prompt_tokens") is not None:
                bucket["counts_prompt_overhead"] = True
    summary = {}
    for name, bucket in by_estimator.items():
        ratios = bucket["ratios"]
        mean = sum(ratios) / len(ratios)
        summary[name] = {
            "samples": bucket["samples"],
            "mean_estimated_over_actual": round(mean, 4),
            "min_ratio": round(min(ratios), 4),
            "max_ratio": round(max(ratios), 4),
            # Under-estimation is the dangerous direction: the guard admits a
            # context whose real request runs over budget.
            "under_estimating_samples": sum(1 for value in ratios if value < 1),
            "counts_prompt_overhead": bucket["counts_prompt_overhead"],
            "purposes": sorted(bucket["purposes"]),
        }
    return {"by_estimator": summary}


def model_cost_metrics(db: Any) -> dict[str, Any]:
    """Token spend and contract-repair frequency per capability."""

    rows = db.all(
        "SELECT event_type, payload FROM audit_events WHERE event_type IN "
        "('TaskResultProcessingSucceeded','TaskResultProcessingFailed',"
        "'QuestionBallotDraftPrepared','FinalDeliverableGenerated')"
    )
    by_purpose: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = _decoded(row["payload"], {}) or {}
        for node, context in _walk_invocations(payload):
            purpose = str(context.get("purpose") or "UNKNOWN")
            usage = node.get("usage") or {}
            bucket = by_purpose.setdefault(
                purpose,
                {
                    "calls": 0,
                    "billed_calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "repair_calls": 0,
                    "failures": 0,
                    "models": set(),
                },
            )
            bucket["calls"] += 1
            billed = False
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = usage.get(key)
                if isinstance(value, int):
                    bucket[key] += value
                    billed = True
            if billed:
                bucket["billed_calls"] += 1
            requests = usage.get("request_count")
            if isinstance(requests, int) and requests > 1:
                bucket["repair_calls"] += requests - 1
            if node.get("output_status") == "FAILED":
                bucket["failures"] += 1
            if node.get("model"):
                bucket["models"].add(str(node["model"]))
    return {
        "by_purpose": {
            purpose: {
                **{k: v for k, v in bucket.items() if k != "models"},
                "models": sorted(bucket["models"]),
                "failure_rate": _rate(bucket["failures"], bucket["calls"]),
                # Failed attempts never reach the provider and are not
                # billed; averaging over them understates real unit cost.
                "tokens_per_billed_call": _rate(
                    bucket["total_tokens"], bucket["billed_calls"]
                ),
            }
            for purpose, bucket in sorted(by_purpose.items())
        }
    }


def p1_lineage_metrics(db: Any, *, episode_id: str) -> dict[str, Any]:
    """Every ballot option must be bound to an accepted upstream version.

    Fully checkable without a human: the binding either points at an accepted
    result or it does not.
    """

    ballots = db.all(
        "SELECT action_item_id, payload FROM action_item_participation_inputs "
        "WHERE episode_id = ? AND contribution_type = 'BALLOT' "
        "AND status = 'SUBMITTED'",
        (episode_id,),
    )
    options = 0
    unbound = 0
    stale = 0
    for ballot in ballots:
        payload = _decoded(ballot["payload"], {}) or {}
        for option in payload.get("options") or []:
            options += 1
            version_id = option.get("source_version_id")
            if not version_id:
                unbound += 1
                continue
            accepted = db.one(
                "SELECT accepted_task_result_id FROM accepted_task_results "
                "WHERE accepted_version_id = ?",
                (version_id,),
            )
            if not accepted:
                unbound += 1
                continue
            current = db.one(
                "SELECT current_valid_version_id FROM action_items "
                "WHERE action_item_id = ?",
                (option.get("source_action_item_id"),),
            )
            if current and current["current_valid_version_id"] != version_id:
                stale += 1
    votes = db.all(
        "SELECT COUNT(*) AS count FROM action_item_participation_inputs "
        "WHERE episode_id = ? AND contribution_type = 'VOTE' "
        "AND status = 'SUBMITTED'",
        (episode_id,),
    )
    return {
        "submitted_ballots": len(ballots),
        "ballot_options": options,
        "options_not_bound_to_an_accepted_result": unbound,
        "options_bound_to_a_superseded_version": stale,
        "source_binding_accuracy": _rate(options - unbound, options),
        "submitted_votes": int(votes[0]["count"]) if votes else 0,
    }


def human_gate_metrics(db: Any) -> dict[str, Any]:
    """Where humans actually decided, and how often the model was overruled."""

    def count(event_type: str) -> int:
        rows = db.all(
            "SELECT COUNT(*) AS count FROM audit_events WHERE event_type = ?",
            (event_type,),
        )
        return int(rows[0]["count"]) if rows else 0

    accepted = count("ArtifactVersionAcceptedByCoordinator")
    returned = count("ArtifactVersionReturnedForRevision")
    advice_conflicts = 0
    rows = db.all(
        "SELECT v.processing_result, v.review_status FROM artifact_versions v "
        "WHERE v.processing_status = 'READY' AND v.review_status IN "
        "('ACCEPTED','REJECTED')"
    )
    for row in rows:
        result = _decoded(row["processing_result"], {}) or {}
        advice = (result.get("acceptance_advice") or {}).get("decision")
        if not advice:
            continue
        model_says_accept = str(advice).upper() in {"ACCEPT", "APPROVE"}
        human_accepted = row["review_status"] == "ACCEPTED"
        if model_says_accept != human_accepted:
            advice_conflicts += 1
    return {
        "coordinator_acceptances": accepted,
        "coordinator_returns": returned,
        "acceptance_rate": _rate(accepted, accepted + returned),
        "reviewed_versions_with_model_advice": len(rows),
        # The model advises; the human decides. A non-zero disagreement count
        # is evidence the gate is real rather than a rubber stamp.
        "human_overruled_model_advice": advice_conflicts,
        "assignment_acceptances": count("ActionItemAssignmentAccepted"),
        "memory_candidates_proposed": count("CollaborationMemoryProposed"),
        "memory_confirmed_by_subject": count("CollaborationMemoryConfirmed"),
        "memory_rejected_by_subject": count("CollaborationMemoryRejected"),
        "memory_self_declared": count("CollaborationMemoryDeclared"),
    }


def build_product_evaluation(
    db: Any, *, episode_id: str, extraction_path: str | None = None
) -> dict[str, Any]:
    """Assemble the deterministic half of the evaluation table."""

    report = {
        "schema_version": "product-evaluation.v1",
        "episode_id": episode_id,
        "human_cost": review_cost_metrics(db, episode_id=episode_id),
        "citation_fidelity": citation_fidelity_metrics(db),
        "token_accuracy": token_accuracy_metrics(db),
        "model_cost": model_cost_metrics(db),
        "p1_lineage": p1_lineage_metrics(db, episode_id=episode_id),
        "human_gates": human_gate_metrics(db),
        "notes": [
            "这些指标全部来自系统已记录的确定性事实，不需要人工标注，"
            "换模型或模型不可用时仍然成立。",
            "抽取质量无法用这种方式衡量，需要带标注的会议，见 extraction_evaluation。",
        ],
    }
    if extraction_path:
        report["extraction_source"] = extraction_path
    report["input_hash"] = stable_hash(
        {"episode_id": episode_id, "report": canonical_json(report)}
    )
    return report
