"""Assemble one Agent run into the shape the Observatory page renders.

This computes almost nothing of its own. `build_product_evaluation` already
derives the human-gate and citation numbers, and `_walk_invocations` already
knows how to find a model call's usage inside an audit payload -- recomputing
either here would give the page a second source of truth for numbers that are
supposed to be recomputable from one, which is what CON-016 exists to prevent.

What it does add is the two views that have no metric behind them because they
are shapes rather than figures: where the 202 audit events fall along the run,
and which final-deliverable fields each artifact version contributed.
"""

from __future__ import annotations

import json
from typing import Any

from .product_evaluation import (
    _walk_invocations,
    citation_fidelity_metrics,
    human_gate_metrics,
)


# Six types cover ~86% of a typical run; the tail becomes one lane rather than
# seven near-empty ones. Order is by volume, which is also roughly the order
# they appear.
LANE_TYPES = (
    "OutboxEntry",
    "ArtifactVersion",
    "ActionItem",
    "Episode",
    "AcceptedTaskResult",
    "CollaborationMemory",
)
OTHER_LANE = "其他"


def _decoded(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def audit_lanes(db: Any, *, run_id: str) -> dict[str, Any]:
    """Where each audit event falls along the run's sequence.

    Returned as positions rather than counts: the count is already a metric,
    while the distribution is the thing a reader can only get from a picture --
    a burst of OutboxEntry events right after the last acceptance says more
    about how the run went than "58 outbox events" does.
    """

    rows = [
        dict(row)
        for row in db.all(
            "SELECT sequence_no, aggregate_type, event_type FROM audit_events "
            "WHERE run_id = ? ORDER BY sequence_no",
            (run_id,),
        )
    ]
    if not rows:
        return {"total": 0, "first": 0, "last": 0, "lanes": []}

    buckets: dict[str, list[dict[str, Any]]] = {
        name: [] for name in (*LANE_TYPES, OTHER_LANE)
    }
    for row in rows:
        lane = row["aggregate_type"]
        if lane not in buckets:
            lane = OTHER_LANE
        buckets[lane].append(
            {"seq": int(row["sequence_no"]), "event": row["event_type"]}
        )

    first = rows[0]["sequence_no"]
    last = rows[-1]["sequence_no"]
    return {
        "total": len(rows),
        "first": first,
        "last": last,
        "lanes": [
            {"name": name, "count": len(events), "events": events}
            for name, events in buckets.items()
            if events
        ],
    }


def lineage_by_version(db: Any, *, episode_id: str) -> dict[str, Any]:
    """Which final-deliverable fields each version contributed.

    Indexed by version rather than by field because the interesting question is
    the negative one: a superseded version should contribute nothing, and that
    is only visible if you can select a version and see an empty result.
    `GATE-VER-001` asserts it; this is the same claim as something to click.
    """

    final = db.one(
        "SELECT final_deliverable_id, revision_no, status FROM final_deliverables "
        "WHERE episode_id = ? ORDER BY revision_no DESC LIMIT 1",
        (episode_id,),
    )
    if not final:
        return {"final_deliverable_id": None, "versions": [], "fields": []}
    final = dict(final)

    rows = [
        dict(row)
        for row in db.all(
            "SELECT l.field_path, l.version_id, l.action_item_id, i.title "
            "FROM final_field_lineage l "
            "JOIN action_items i ON i.action_item_id = l.action_item_id "
            "WHERE l.final_deliverable_id = ? ORDER BY l.field_path",
            (final["final_deliverable_id"],),
        )
    ]
    contributing = {row["version_id"] for row in rows}

    # Every version of every task, so the ones that contributed nothing are
    # listed too -- they are the point.
    versions = [
        dict(row)
        for row in db.all(
            "SELECT v.version_id, v.action_item_id, v.received_sequence, "
            "v.review_status, v.supersedes_version_id, i.title "
            "FROM artifact_versions v "
            "JOIN action_items i ON i.action_item_id = v.action_item_id "
            "WHERE i.episode_id = ? ORDER BY i.title, v.received_sequence",
            (episode_id,),
        )
    ]
    # A version named by another's supersedes pointer is the replaced one. It
    # is the row that must show zero fields, so it has to be identifiable
    # rather than merely absent from the lineage.
    superseded = {
        version["supersedes_version_id"]
        for version in versions
        if version["supersedes_version_id"]
    }
    return {
        "final_deliverable_id": final["final_deliverable_id"],
        "revision_no": final["revision_no"],
        "status": final["status"],
        "fields": rows,
        "versions": [
            {
                "version_id": version["version_id"],
                "action_item_id": version["action_item_id"],
                "title": version["title"],
                "received_sequence": version["received_sequence"],
                "review_status": version["review_status"],
                "superseded": version["version_id"] in superseded,
                "field_count": sum(
                    1 for row in rows if row["version_id"] == version["version_id"]
                ),
                "contributed": version["version_id"] in contributing,
            }
            for version in versions
        ],
    }


def token_calls(db: Any) -> dict[str, Any]:
    """Per-call token spend, read from the audit payloads that already hold it.

    Reported as individual calls with order statistics rather than a fitted
    curve. At the handful of calls a run makes, a density plot draws a shape
    the sample cannot support and hides the one expensive call, which is the
    only thing anybody looks for.
    """

    rows = db.all(
        "SELECT event_type, sequence_no, payload FROM audit_events "
        "ORDER BY sequence_no"
    )
    calls: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        payload = _decoded(record["payload"], {}) or {}
        for node, context in _walk_invocations(payload):
            usage = node.get("usage") or {}
            total = usage.get("total_tokens")
            if not isinstance(total, int):
                continue
            calls.append(
                {
                    "sequence_no": int(record["sequence_no"]),
                    "event_type": record["event_type"],
                    "purpose": str(context.get("purpose") or "UNKNOWN"),
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "total_tokens": total,
                }
            )
    calls.sort(key=lambda call: call["sequence_no"])

    from .extraction import summarize_token_calls

    return {"calls": calls, "summary": summarize_token_calls(calls)}


def _outbox_flow(db: Any, *, run_id: str) -> dict[str, Any]:
    """Outbox counts, plus the duplicate check as the gate actually defines it.

    "Zero duplicate sends" is not `delivered - created`. Those two differ for
    innocent reasons -- a retry claims twice, an idempotent hit delivers
    without creating -- and subtracting them reports a duplicate where none
    happened. The gate's definition is the one that means something: no
    effect_id and no external message id appears more than once.
    """

    counts = {
        record["event_type"]: int(record["n"])
        for record in (
            dict(row)
            for row in db.all(
                "SELECT event_type, count(*) AS n FROM audit_events "
                "WHERE run_id = ? AND aggregate_type = 'OutboxEntry' "
                "GROUP BY event_type",
                (run_id,),
            )
        )
    }

    def count(sql: str, params: tuple[Any, ...] = ()) -> int:
        return int(dict(db.one(sql, params))["n"])

    messages = [
        dict(row)
        for row in db.all(
            "SELECT effect_id, external_message_id FROM mock_im_messages"
        )
    ]
    effect_seen: dict[str, int] = {}
    external_seen: dict[str, int] = {}
    for message in messages:
        effect_seen[message["effect_id"]] = (
            effect_seen.get(message["effect_id"], 0) + 1
        )
        external_seen[message["external_message_id"]] = (
            external_seen.get(message["external_message_id"], 0) + 1
        )
    duplicate_effects = sum(1 for n in effect_seen.values() if n > 1)
    duplicate_externals = sum(1 for n in external_seen.values() if n > 1)

    return {
        "created": count(
            "SELECT count(*) AS n FROM outbox_entries WHERE run_id = ?", (run_id,)
        ),
        # Claims come from the domain table's attempt counter, not from
        # OutboxEntryClaimed events: final-organization entries are claimed
        # through a different event type, so counting events under-reports.
        # attempt_count is what the dispatcher actually increments.
        "claimed": count(
            "SELECT COALESCE(sum(attempt_count), 0) AS n FROM outbox_entries "
            "WHERE run_id = ?",
            (run_id,),
        ),
        "retried": max(
            0,
            count(
                "SELECT COALESCE(sum(attempt_count), 0) AS n FROM outbox_entries "
                "WHERE run_id = ?",
                (run_id,),
            )
            - count(
                "SELECT count(*) AS n FROM outbox_entries WHERE run_id = ?",
                (run_id,),
            ),
        ),
        "deduplicated": counts.get("OutboxDeliveryDeduplicated", 0),
        "delivered": count(
            "SELECT count(*) AS n FROM outbox_entries "
            "WHERE run_id = ? AND status = 'DELIVERED'",
            (run_id,),
        ),
        "dead_letter": count(
            "SELECT count(*) AS n FROM outbox_entries "
            "WHERE run_id = ? AND status = 'DEAD_LETTER'",
            (run_id,),
        ),
        "external_messages": len(messages),
        "duplicate_effect_ids": duplicate_effects,
        "duplicate_external_ids": duplicate_externals,
    }


def _result_flow(db: Any, *, episode_id: str) -> dict[str, int]:
    def count(sql: str) -> int:
        return int(dict(db.one(sql, (episode_id,)))["n"])

    return {
        "received": count(
            "SELECT count(*) AS n FROM artifact_versions v "
            "JOIN action_items i ON i.action_item_id = v.action_item_id "
            "WHERE i.episode_id = ?"
        ),
        "validation_failed": count(
            "SELECT count(*) AS n FROM artifact_versions v "
            "JOIN action_items i ON i.action_item_id = v.action_item_id "
            "WHERE i.episode_id = ? AND v.validation_status = 'FAILED'"
        ),
        "returned": count(
            "SELECT count(*) AS n FROM artifact_versions v "
            "JOIN action_items i ON i.action_item_id = v.action_item_id "
            "WHERE i.episode_id = ? AND v.review_status = 'REJECTED'"
        ),
        "accepted": count(
            "SELECT count(*) AS n FROM accepted_task_results r "
            "JOIN action_items i ON i.action_item_id = r.action_item_id "
            "WHERE i.episode_id = ?"
        ),
    }


def available_runs(db: Any) -> list[dict[str, Any]]:
    rows = db.all(
        "SELECT e.episode_id, e.run_id, e.status, e.created_sim_time, "
        "(SELECT count(*) FROM audit_events a WHERE a.run_id = e.run_id) AS events "
        "FROM episodes e ORDER BY e.created_sim_time DESC"
    )
    return [
        {
            "run_id": record["run_id"],
            "episode_id": record["episode_id"],
            "status": record["status"],
            "created_sim_time": record["created_sim_time"],
            "events": int(record["events"]),
        }
        for record in (dict(row) for row in rows)
    ]


def build_observatory(db: Any, *, episode_id: str, run_id: str) -> dict[str, Any]:
    """One run, assembled for the page."""

    gates = human_gate_metrics(db)
    citations = citation_fidelity_metrics(db)
    outbox = _outbox_flow(db, run_id=run_id)
    audit = audit_lanes(db, run_id=run_id)
    tokens = token_calls(db)

    return {
        "schema_version": "observatory.v1",
        "run": {"run_id": run_id, "episode_id": episode_id},
        "runs": available_runs(db),
        "headline": {
            "duplicate_sends": outbox["duplicate_effect_ids"]
            + outbox["duplicate_external_ids"],
            "deduplicated": outbox["deduplicated"],
            "delivered": outbox["delivered"],
            "human_overruled": gates.get("human_overruled_model_advice"),
            "model_advised": gates.get("reviewed_versions_with_model_advice"),
            "citation_hallucination_rate": citations.get(
                "citation_hallucination_rate"
            ),
            "conclusion_points": citations.get("conclusion_points"),
            "audit_events": audit["total"],
        },
        "outbox": outbox,
        "results": _result_flow(db, episode_id=episode_id),
        "human_gates": gates,
        "citations": citations,
        "audit": audit,
        "tokens": tokens,
        "lineage": lineage_by_version(db, episode_id=episode_id),
    }
