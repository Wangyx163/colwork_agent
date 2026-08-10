"""Storage and transitions for compound tasks.

Written against a database handle rather than as methods on the coordination
service: the stage rules live in `compound_tasks`, the persistence lives here,
and neither needs the eight thousand lines the rest of the domain has grown.
Everything below is one transaction, and every transition appends its own
audit event -- a compound task moves for the same reasons and under the same
rules as anything else here.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from .compound_tasks import (
    CompoundKind,
    CompoundTaskError,
    Stage,
    is_complete,
    may_act,
    next_stage,
    role_at,
    stages_for,
)
from .models import canonical_json


def _decode(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value if value is not None else fallback


def create_compound_task(
    db: Any,
    *,
    run_id: str,
    episode_id: str,
    kind: str,
    title: str,
    body: str,
    owner_actor_id: str,
    member_actor_ids: list[str],
    source_span: str,
    selection_count: int | None,
    sim_time: str,
    message_id: str,
) -> dict[str, Any]:
    """Declare the shape. Nobody is asked to do anything until this exists."""

    kind = str(kind or "").strip().upper()
    stages_for(kind)  # refuses an unknown kind before anything is written
    title = title.strip()
    if not title:
        raise CompoundTaskError("compound task title is required")
    if not source_span.strip():
        # Same rule the ordinary structures have: a shape is something the
        # meeting decided, not something a console invented.
        raise CompoundTaskError("meeting source span is required")
    members = [str(item) for item in dict.fromkeys(member_actor_ids) if item]
    if len(members) < 2:
        raise CompoundTaskError("a compound task needs at least two people")
    if owner_actor_id not in members:
        # The owner does the merging, and merging without having contributed
        # is how a summary drifts from what people actually wrote.
        raise CompoundTaskError("the owner has to be one of the members")
    if kind == CompoundKind.VOTE:
        if not selection_count or not 1 <= int(selection_count) < len(members) * 8:
            raise CompoundTaskError("selection count is out of range")

    existing = db.one(
        "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
        (message_id,),
    )
    if existing:
        return json.loads(existing["processed_result"])

    compound_task_id = f"cmp_{uuid4().hex}"
    with db.transaction() as cursor:
        cursor.execute(
            "INSERT INTO compound_tasks(compound_task_id, episode_id, kind, title, "
            "body, stage, owner_actor_id, member_actor_ids, selection_count, "
            "source_span, created_sim_time, stage_entered_sim_time, version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                compound_task_id,
                episode_id,
                kind,
                title,
                body,
                Stage.COLLECTING,
                owner_actor_id,
                canonical_json(members),
                int(selection_count) if selection_count else None,
                source_span.strip(),
                sim_time,
                sim_time,
            ),
        )
        db.append_audit(
            cursor,
            run_id=run_id,
            aggregate_type="CompoundTask",
            aggregate_id=compound_task_id,
            event_type="CompoundTaskDeclared",
            sim_time=sim_time,
            payload={
                "kind": kind,
                "owner_actor_id": owner_actor_id,
                "member_actor_ids": members,
                "selection_count": selection_count,
            },
            correlation_id=f"corr_{message_id}",
        )
        result = {
            "compound_task_id": compound_task_id,
            "kind": kind,
            "stage": str(Stage.COLLECTING),
        }
        _record_inbound(db, cursor, message_id, result, sim_time)
    return result


def _record_inbound(
    db: Any, cursor: Any, message_id: str, result: dict[str, Any], sim_time: str
) -> None:
    """Same receipt table the rest of the intake path uses.

    Sharing it is the point: a retried compound-task click and a retried
    Feishu callback are the same problem, and a second ledger would only be a
    second thing to keep honest.
    """

    row = cursor.execute(
        "SELECT COALESCE(MAX(accepted_sequence), 0) + 1 AS next_sequence "
        "FROM inbound_receipts"
    ).fetchone()
    cursor.execute(
        "INSERT INTO inbound_receipts VALUES (?, ?, ?, ?)",
        (
            message_id,
            dict(row)["next_sequence"],
            canonical_json(result),
            sim_time,
        ),
    )


def load(db: Any, compound_task_id: str) -> dict[str, Any]:
    row = db.one(
        "SELECT * FROM compound_tasks WHERE compound_task_id = ?",
        (compound_task_id,),
    )
    if not row:
        raise KeyError(compound_task_id)
    task = dict(row)
    task["member_actor_ids"] = _decode(task["member_actor_ids"], [])
    return task


def submit_input(
    db: Any,
    compound_task_id: str,
    *,
    run_id: str,
    actor_id: str,
    payload: dict[str, Any],
    sim_time: str,
    message_id: str,
) -> dict[str, Any]:
    """One person's answer at the current stage.

    Re-answering replaces: somebody who adds a fourth option before everyone
    else has finished is correcting their own contribution, not starting a
    second one, and keeping both would double their say in the merge.
    """

    existing = db.one(
        "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
        (message_id,),
    )
    if existing:
        return json.loads(existing["processed_result"])

    task = load(db, compound_task_id)
    stage = task["stage"]
    if not may_act(
        stage,
        actor_id=actor_id,
        owner_actor_id=task["owner_actor_id"],
        members=task["member_actor_ids"],
    ):
        raise PermissionError("this stage is not yours to act in")
    if role_at(stage) != "EVERYONE":
        raise CompoundTaskError("this stage is the owner's to finish")
    _validate_payload(task, stage, payload)

    with db.transaction() as cursor:
        prior = cursor.execute(
            "SELECT input_id FROM compound_task_inputs WHERE compound_task_id = ? "
            "AND stage = ? AND actor_id = ?",
            (compound_task_id, stage, actor_id),
        ).fetchone()
        if prior:
            cursor.execute(
                "UPDATE compound_task_inputs SET payload = ?, source_message_id = ?, "
                "created_sim_time = ? WHERE input_id = ?",
                (
                    canonical_json(payload),
                    message_id,
                    sim_time,
                    prior["input_id"],
                ),
            )
            input_id = prior["input_id"]
        else:
            input_id = f"cin_{uuid4().hex}"
            cursor.execute(
                "INSERT INTO compound_task_inputs(input_id, compound_task_id, stage, "
                "actor_id, payload, source_message_id, created_sim_time) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    input_id,
                    compound_task_id,
                    stage,
                    actor_id,
                    canonical_json(payload),
                    message_id,
                    sim_time,
                ),
            )
        db.append_audit(
            cursor,
            run_id=run_id,
            aggregate_type="CompoundTask",
            aggregate_id=compound_task_id,
            event_type="CompoundTaskInputRecorded",
            sim_time=sim_time,
            payload={"stage": stage, "actor_id": actor_id, "replaced": bool(prior)},
            correlation_id=f"corr_{message_id}",
        )
        answered = {
            dict(row)["actor_id"]
            for row in cursor.execute(
                "SELECT actor_id FROM compound_task_inputs "
                "WHERE compound_task_id = ? AND stage = ?",
                (compound_task_id, stage),
            ).fetchall()
        }
        complete = is_complete(
            stage, submitted_actor_ids=answered, members=task["member_actor_ids"]
        )
        if complete:
            _enter(
                db,
                cursor,
                task,
                next_stage(task["kind"], stage),
                run_id=run_id,
                sim_time=sim_time,
                reason="everyone answered",
            )
        result = {
            "compound_task_id": compound_task_id,
            "input_id": input_id,
            "stage": stage,
            "answered": sorted(answered),
            "stage_complete": complete,
        }
        _record_inbound(db, cursor, message_id, result, sim_time)
    return result


def _validate_payload(task: dict[str, Any], stage: str, payload: dict[str, Any]) -> None:
    """What a stage will accept, checked before anything is stored.

    Deliberately narrow. A vote payload that is not a mapping of option to
    score, or an option list that is empty, would pass straight through to a
    merge that has to guess what it meant.
    """

    if stage == Stage.COLLECTING:
        if task["kind"] == CompoundKind.VOTE:
            options = [
                str(item).strip() for item in (payload.get("options") or []) if str(item).strip()
            ]
            if not options:
                raise CompoundTaskError("至少填写一条")
            payload["options"] = options
        elif not str(payload.get("content") or "").strip():
            raise CompoundTaskError("内容不能为空")
    elif stage == Stage.VOTING:
        scores = payload.get("scores")
        if not isinstance(scores, dict) or not scores:
            raise CompoundTaskError("请给每一条打分")
        for value in scores.values():
            if not isinstance(value, int) or not 1 <= value <= 5:
                raise CompoundTaskError("每条的分数要在 1 到 5 之间")


def finish_owner_stage(
    db: Any,
    compound_task_id: str,
    *,
    run_id: str,
    actor_id: str,
    payload: dict[str, Any],
    sim_time: str,
    message_id: str,
) -> dict[str, Any]:
    """The owner's turn: the merged shortlist, or the finished write-up."""

    existing = db.one(
        "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
        (message_id,),
    )
    if existing:
        return json.loads(existing["processed_result"])

    task = load(db, compound_task_id)
    stage = task["stage"]
    if role_at(stage) != "OWNER":
        raise CompoundTaskError("现在轮不到负责人")
    if actor_id != task["owner_actor_id"]:
        raise PermissionError("只有这个复合任务的负责人能做这一步")
    if stage == Stage.MERGING and task["kind"] == CompoundKind.VOTE:
        options = [
            str(item).strip()
            for item in (payload.get("options") or [])
            if str(item).strip()
        ]
        if len(options) <= int(task["selection_count"] or 0):
            # Scoring a list you must take whole decides nothing.
            raise CompoundTaskError("候选条数要多于最终保留的条数")
        payload["options"] = options

    with db.transaction() as cursor:
        input_id = f"cin_{uuid4().hex}"
        cursor.execute(
            "INSERT INTO compound_task_inputs(input_id, compound_task_id, stage, "
            "actor_id, payload, source_message_id, created_sim_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                input_id,
                compound_task_id,
                stage,
                actor_id,
                canonical_json(payload),
                message_id,
                sim_time,
            ),
        )
        following = next_stage(task["kind"], stage)
        _enter(
            db,
            cursor,
            task,
            following,
            run_id=run_id,
            sim_time=sim_time,
            reason="owner finished the stage",
        )
        result = {
            "compound_task_id": compound_task_id,
            "stage": str(following),
            "input_id": input_id,
        }
        _record_inbound(db, cursor, message_id, result, sim_time)
    return result


def revoke(
    db: Any,
    compound_task_id: str,
    *,
    run_id: str,
    actor_id: str,
    reason: str,
    sim_time: str,
    message_id: str,
) -> dict[str, Any]:
    """Withdraw a shape declared over the wrong people or the wrong thing."""

    if not reason.strip():
        raise CompoundTaskError("撤销要写原因")
    existing = db.one(
        "SELECT processed_result FROM inbound_receipts WHERE message_id = ?",
        (message_id,),
    )
    if existing:
        return json.loads(existing["processed_result"])

    task = load(db, compound_task_id)
    if task["stage"] in (Stage.DONE, Stage.REVOKED):
        raise CompoundTaskError("这个复合任务已经结束了")
    with db.transaction() as cursor:
        _enter(
            db,
            cursor,
            task,
            Stage.REVOKED,
            run_id=run_id,
            sim_time=sim_time,
            reason=reason,
            actor_id=actor_id,
        )
        result = {"compound_task_id": compound_task_id, "stage": str(Stage.REVOKED)}
        _record_inbound(db, cursor, message_id, result, sim_time)
    return result


def _enter(
    db: Any,
    cursor: Any,
    task: dict[str, Any],
    stage: Stage,
    *,
    run_id: str,
    sim_time: str,
    reason: str,
    actor_id: str | None = None,
) -> None:
    cursor.execute(
        "UPDATE compound_tasks SET stage = ?, stage_entered_sim_time = ?, "
        "version = version + 1 WHERE compound_task_id = ?",
        (str(stage), sim_time, task["compound_task_id"]),
    )
    db.append_audit(
        cursor,
        run_id=run_id,
        aggregate_type="CompoundTask",
        aggregate_id=task["compound_task_id"],
        event_type="CompoundTaskStageEntered",
        sim_time=sim_time,
        payload={
            "from": task["stage"],
            "to": str(stage),
            "reason": reason,
            **({"actor_id": actor_id} if actor_id else {}),
        },
        correlation_id=f"corr_{task['compound_task_id']}_{stage}",
    )


def project(db: Any, episode_id: str, *, actor_id: str) -> list[dict[str, Any]]:
    """What one person sees: every compound task, and whether it is their turn.

    Inputs from other people are counted, not shown. Seeing what four
    colleagues wrote before writing your own is how five lists become one
    list written five times.
    """

    projected: list[dict[str, Any]] = []
    for row in db.all(
        "SELECT * FROM compound_tasks WHERE episode_id = ? "
        "ORDER BY created_sim_time, compound_task_id",
        (episode_id,),
    ):
        task = dict(row)
        task["member_actor_ids"] = _decode(task["member_actor_ids"], [])
        stage = task["stage"]
        inputs = [
            dict(item)
            for item in db.all(
                "SELECT * FROM compound_task_inputs WHERE compound_task_id = ? "
                "ORDER BY created_sim_time, input_id",
                (task["compound_task_id"],),
            )
        ]
        for item in inputs:
            item["payload"] = _decode(item["payload"], {})
        answered = {
            item["actor_id"] for item in inputs if item["stage"] == stage
        }
        merged = next(
            (
                item["payload"]
                for item in reversed(inputs)
                if item["stage"] == Stage.MERGING
            ),
            None,
        )
        mine = next(
            (
                item["payload"]
                for item in inputs
                if item["stage"] == stage and item["actor_id"] == actor_id
            ),
            None,
        )
        task["stage_role"] = role_at(stage)
        task["my_turn"] = may_act(
            stage,
            actor_id=actor_id,
            owner_actor_id=task["owner_actor_id"],
            members=task["member_actor_ids"],
        ) and (mine is None or role_at(stage) == "OWNER")
        task["answered_count"] = len(answered)
        task["member_count"] = len(task["member_actor_ids"])
        task["my_input"] = mine
        task["options"] = (merged or {}).get("options") or []
        task["collected"] = (
            [
                {"actor_id": item["actor_id"], "payload": item["payload"]}
                for item in inputs
                if item["stage"] == Stage.COLLECTING
            ]
            if role_at(stage) == "OWNER" and actor_id == task["owner_actor_id"]
            else []
        )
        task["result"] = _tally(task, inputs)
        projected.append(task)
    return projected


def _tally(task: dict[str, Any], inputs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The scoring outcome, once there is one.

    The whole ranking is returned, not only the survivors: which options came
    close is how somebody judges whether the cut was drawn sensibly.
    """

    if task["kind"] != CompoundKind.VOTE:
        return None
    options = next(
        (
            item["payload"].get("options") or []
            for item in reversed(inputs)
            if item["stage"] == Stage.MERGING
        ),
        [],
    )
    if not options:
        return None
    votes = [item for item in inputs if item["stage"] == Stage.VOTING]
    ranked = []
    for index, text in enumerate(options):
        scores = [
            int(vote["payload"].get("scores", {}).get(str(index), 0))
            for vote in votes
            if str(index) in (vote["payload"].get("scores") or {})
        ]
        ranked.append(
            {
                "index": index,
                "text": text,
                "score_total": sum(scores),
                "score_count": len(scores),
                "score_average": round(sum(scores) / len(scores), 2) if scores else None,
            }
        )
    ranked.sort(key=lambda item: (-item["score_total"], item["index"]))
    keep = int(task["selection_count"] or len(ranked))
    return {
        "voted_count": len(votes),
        "member_count": len(task["member_actor_ids"]),
        "complete": len(votes) >= len(task["member_actor_ids"]),
        "selection_count": keep,
        "ranked": ranked,
        "selected": ranked[:keep],
    }
