from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from .agent_worker import AgentWorker
from .metrics import build_report
from .models import SimulatedCrash
from .service import CoordinationService, load_fixture
from .store import Database


def _data_url(mime_type: str, content: bytes) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"


def _small_text_pdf(text: str) -> bytes:
    """Build a dependency-free, text-bearing PDF for the P0 attachment path."""
    escaped = (
        text.encode("ascii", errors="replace")
        .decode("ascii")
        .replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    document = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{index} 0 obj\n".encode("ascii"))
        document.extend(obj)
        document.extend(b"\nendobj\n")
    xref_offset = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    document.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(document)


def run_p0_evaluation(
    database_path: str | Path,
    fixture_path: str | Path,
    *,
    database_url: str | None = None,
    fresh: bool = False,
) -> tuple[dict[str, Any], CoordinationService]:
    fixture = load_fixture(fixture_path)
    if database_url:
        from .postgres_store import PostgresDatabase

        schema_path = Path(fixture_path).resolve().parent.parent / "db" / "postgres_schema.sql"
        database = PostgresDatabase(database_url, schema_path=schema_path)
    else:
        database = Database(database_path)
    database.initialize()
    if database_url and fresh:
        database.reset_project_data()
    service = CoordinationService(database, fixture)
    worker = AgentWorker(
        service,
        processing_mode="local",
        session_id="evaluation_agent_worker",
    )

    def process_version(version_id: str) -> dict[str, Any]:
        candidate = None
        for _ in range(12):
            step = worker.run_once()
            if step["kind"] == "IDLE":
                break
            if step["kind"] != "TASK_RESULT":
                continue
            candidate = step["result"]
            if candidate["version_id"] == version_id:
                return candidate
        states = [
            dict(row)
            for row in service.db.all(
                "SELECT version_id, validation_status, review_status, "
                "processing_status FROM artifact_versions "
                "ORDER BY received_sequence"
            )
        ]
        raise AssertionError(
            "expected the submitted task version to be processed: "
            f"target={version_id}, candidate={candidate}, states={states}"
        )

    def review_version(
        version_id: str,
        message_suffix: str,
        *,
        approve: bool,
        comment: str,
    ) -> dict[str, Any]:
        process_version(version_id)
        return service.review_artifact(
            version_id,
            actor_id="owner_lead",
            approve=approve,
            comment=comment,
            message_id=f"review_{message_suffix}",
        )

    service.bootstrap()

    # B1: extraction proposals are coordinator-reviewed and published before any
    # participant can claim them.  The fixture no longer pre-assigns owners.
    for index, item in enumerate(fixture["action_items"]):
        action = service.action(item["action_item_id"])
        metadata = service.proposal_metadata(action)
        service.revise_action_proposal(
            item["action_item_id"],
            actor_id="owner_lead",
            title=item["title"],
            deliverable=metadata["deliverable"],
            work_requirements=item["work_requirements"],
            acceptance_criteria="结构化必填字段完整，正文或附件与任务目标一致",
            management_review_policy=item["management_review_policy"],
            priority="P0" if index < 2 else "P1",
            team_required_by_sim_time=item["team_required_by_sim_time"],
            message_id=f"review_proposal_{index + 1}",
        )
        service.publish_action(
            item["action_item_id"],
            actor_id="owner_lead",
            message_id=f"publish_proposal_{index + 1}",
        )

    claimant_by_action = {
        "ai_feedback": "owner_a",
        "ai_risks": "owner_b",
        "ai_release_notes": "owner_c",
        "ai_training": "owner_c",
    }
    for item in fixture["action_items"]:
        promised_by = item["team_required_by_sim_time"]
        if item["action_item_id"] == "ai_risks":
            # Deliberately make one personal promise later than the team's need-by.
            promised_by = "2026-08-06T17:00:00+10:00"
        claim = service.claim_action(
            item["action_item_id"],
            actor_id=claimant_by_action[item["action_item_id"]],
            promised_deadline_sim_time=promised_by,
            message_id=f'claim_{item["action_item_id"]}',
        )
        if not claim["owner_actor_id"]:
            raise AssertionError("claim did not bind an attendee principal")

    # B2: only the participant's own promise changes; the team date remains fixed.
    service.advance_time("2026-08-03T09:30:00+10:00")
    resolved_schedule = service.revise_personal_commitment(
        "ai_risks",
        actor_id="owner_b",
        proposed_deadline_sim_time="2026-08-05T17:00:00+10:00",
        reason="已协调资源，可在团队需要时间前交付",
        message_id="risk_personal_promise_resolved",
    )
    if resolved_schedule["schedule_conflict"]:
        raise AssertionError("the evaluation schedule conflict was not resolved")

    # B3: explicit quick signals and attendee-only help.  Page reads are absent
    # because they must not count as business progress.
    service.advance_time("2026-08-03T10:00:00+10:00")
    service.record_progress_signal(
        "ai_release_notes",
        actor_id="owner_c",
        signal_type="ON_TRACK",
        note="已开始整理变更范围",
        message_id="release_signal_on_track",
    )
    try:
        service.request_assistance(
            "ai_release_notes",
            actor_id="owner_c",
            target_actor_id="not_an_attendee",
            category="EXPERTISE",
            summary="请校对发布范围",
            message_id="help_invalid_attendee",
        )
    except PermissionError:
        service.record_security_rejection(
            event_type="AuthorizationRejected",
            actor_id="owner_c",
            operation="request_assistance",
            reason="ASSISTANCE_TARGET_NOT_EPISODE_PARTICIPANT",
            resource_id="ai_release_notes",
        )
    else:
        raise AssertionError("a non-attendee was accepted as an assistance target")
    help_request = service.request_assistance(
        "ai_release_notes",
        actor_id="owner_c",
        target_actor_id="owner_a",
        category="EXPERTISE",
        summary="请协助校对版本范围与回滚说明",
        message_id="help_release_open",
    )
    service.advance_time("2026-08-03T10:15:00+10:00")
    service.update_assistance(
        help_request["assistance_request_id"],
        actor_id="owner_a",
        action="ACKNOWLEDGE",
        message_id="help_release_acknowledge",
    )
    service.advance_time("2026-08-03T10:30:00+10:00")
    service.update_assistance(
        help_request["assistance_request_id"],
        actor_id="owner_a",
        action="RESOLVE",
        resolution_summary="已核对版本范围并补充回滚提醒",
        message_id="help_release_resolve",
    )

    # B4: every task is submitted and accepted independently.  Together the
    # deliveries cover body-only, PDF and text attachment source layers.
    service.advance_time("2026-08-04T09:00:00+10:00")
    feedback_version = service.submit_artifact(
        "ai_feedback",
        actor_id="owner_a",
        message_id="in_feedback_v1",
        payload={
            "summary": "客户反馈已整理",
            "content": "反馈分为体验与稳定性两类，并标注优先级。",
            "categories": ["体验", "稳定性"],
            "priorities": {"稳定性": "P0", "体验": "P1"},
        },
    )
    review_version(
        feedback_version["version_id"],
        "feedback_v1",
        approve=True,
        comment="正文分类与优先级完整，验收通过",
    )

    service.advance_time("2026-08-04T10:00:00+10:00")
    service.record_progress_signal(
        "ai_release_notes",
        actor_id="owner_c",
        signal_type="READY_TO_SUBMIT",
        note="校对完成，准备提交",
        message_id="release_signal_ready",
    )
    service.advance_time("2026-08-04T11:00:00+10:00")
    release_v1 = service.submit_artifact(
        "ai_release_notes",
        actor_id="owner_c",
        message_id="in_release_v1",
        payload={
            "summary": "发布说明初版",
            "content": "初版发布说明正文，回滚范围仍待补充。",
            "version": "1.0-rc1",
            "changes": ["初版说明"],
        },
    )
    review_version(
        release_v1["version_id"],
        "release_v1",
        approve=False,
        comment="请补充回滚范围，并提交正式版本号",
    )

    service.advance_time("2026-08-04T12:00:00+10:00")
    release_pdf = _small_text_pdf(
        "Release 1.0: rollback scope verified; changes approved."
    )
    release_v2 = service.submit_artifact(
        "ai_release_notes",
        actor_id="owner_c",
        message_id="in_release_v2",
        payload={
            "summary": "发布说明正式版",
            "content": "已补充回滚范围并修正版本号，附件为正式发布说明。",
            "version": "1.0",
            "changes": ["补充回滚范围", "修正版本号"],
            "files": [
                {
                    "name": "release-notes.pdf",
                    "type": "application/pdf",
                    "size": len(release_pdf),
                    "data": _data_url("application/pdf", release_pdf),
                }
            ],
        },
    )
    release_review = review_version(
        release_v2["version_id"],
        "release_v2",
        approve=True,
        comment="回滚范围、正式版本和 PDF 来源一致，验收通过",
    )

    # B5: the accepted release task has resolved help and two quick signals, so
    # those two Memory candidates are proposed.  Its second version exists only
    # because the coordinator rejected the first, which is a property of the
    # task rather than a working preference, so no delivery-rhythm candidate may
    # be proposed from it.
    release_drafts = {
        row["topic"]: dict(row)
        for row in service.db.all(
            "SELECT * FROM collaboration_memories WHERE actor_id = 'owner_c' "
            "AND status = 'PRIVATE_DRAFT'"
        )
        if release_review["accepted_task_result_id"]
    }
    required_topics = {"HELP_SEEKING", "PROGRESS_SIGNAL"}
    if not required_topics <= set(release_drafts):
        raise AssertionError(
            f"expected the observable P0 memory candidates, got {sorted(release_drafts)}"
        )
    if "DELIVERY_RHYTHM" in release_drafts:
        raise AssertionError(
            "rework was misread as an iteration preference; a rejected version "
            "must not propose a delivery-rhythm memory"
        )
    try:
        service.decide_collaboration_memory(
            release_drafts["PROGRESS_SIGNAL"]["memory_id"],
            actor_id="owner_c",
            action="REPLACE",
            replacement_code="UNRELIABLE_PERSON",
            message_id="memory_prohibited_label",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("a prohibited evaluative Memory label was stored")
    service.decide_collaboration_memory(
        release_drafts["HELP_SEEKING"]["memory_id"],
        actor_id="owner_c",
        action="CONFIRM",
        message_id="memory_confirm_help",
    )
    service.decide_collaboration_memory(
        release_drafts["PROGRESS_SIGNAL"]["memory_id"],
        actor_id="owner_c",
        action="REPLACE",
        replacement_code="MILESTONE_ONLY",
        message_id="memory_correct_signal",
    )
    # Group B topics describe what someone wants from collaborators, which the
    # system cannot observe; they are declared by their subject with no draft.
    service.declare_collaboration_memory(
        actor_id="owner_c",
        topic="FEEDBACK_STYLE",
        code="GOAL_FIRST",
        message_id="memory_declare_feedback",
    )
    try:
        service.declare_collaboration_memory(
            actor_id="owner_c",
            topic="HELP_SEEKING",
            code="ASK_WHEN_BLOCKED",
            message_id="memory_declare_observed_topic",
        )
    except ValueError:
        pass
    else:
        raise AssertionError(
            "an observed topic must be confirmed from evidence, not declared"
        )

    service.advance_time("2026-08-04T13:00:00+10:00")
    service.submit_artifact(
        "ai_training",
        actor_id="owner_c",
        message_id="in_training_invalid",
        payload={"time": "周四 15:00"},
    )
    service.evaluate_policy()
    service.dispatch_all(session_id="dispatcher_1")
    service.advance_time("2026-08-04T14:00:00+10:00")
    training_text = "培训时间：周四 15:00\n主讲人：何佳\n材料：协作系统操作说明"
    training_bytes = training_text.encode("utf-8")
    training_version = service.submit_artifact(
        "ai_training",
        actor_id="owner_c",
        message_id="in_training_fixed",
        payload={
            "summary": "培训安排已补齐",
            "content": "周四 15:00 由何佳主讲，详细安排见文本附件。",
            "time": "周四 15:00",
            "trainer": "何佳",
            "files": [
                {
                    "name": "training-plan.txt",
                    "type": "text/plain",
                    "size": len(training_bytes),
                    "data": _data_url("text/plain", training_bytes),
                }
            ],
        },
    )
    review_version(
        training_version["version_id"],
        "training_fixed",
        approve=True,
        comment="时间、讲师、正文和文本附件完整，验收通过",
    )

    # The subject may always decline a proposed observation; rejection is a
    # first-class outcome, not a failure to respond.
    training_draft = service.db.one(
        "SELECT memory_id FROM collaboration_memories WHERE actor_id = 'owner_c' "
        "AND status = 'PRIVATE_DRAFT' ORDER BY memory_id LIMIT 1"
    )
    if not training_draft:
        raise AssertionError(
            "the second accepted task produced no Memory candidate to decline"
        )
    service.decide_collaboration_memory(
        training_draft["memory_id"],
        actor_id="owner_c",
        action="REJECT",
        message_id="memory_reject_candidate",
    )

    # One owner intentionally remains silent inside the check-in window.  The
    # crash happens after mock IM accepts the L1 but before Outbox marks it sent.
    service.advance_time("2026-08-05T09:00:00+10:00")
    decisions = service.evaluate_policy()
    reminder = next(
        decision
        for decision in decisions
        if decision.get("level") == "L1" and not decision.get("suppressed")
    )
    try:
        service.dispatch_all(
            session_id="dispatcher_1",
            crash_after_accept_effect_id=reminder["effect_id"],
        )
    except SimulatedCrash:
        pass
    else:
        raise AssertionError("the configured critical crash was not injected")
    service.recover_dispatcher("dispatcher_2")
    service.dispatch_all(session_id="dispatcher_2")

    service.advance_time("2026-08-05T22:00:00+10:00")
    service.evaluate_policy()
    service.dispatch_all(session_id="dispatcher_2")
    service.advance_time("2026-08-06T09:00:00+10:00")
    no_l3 = service.evaluate_policy()
    if any(decision.get("level") == "L3" for decision in no_l3):
        raise AssertionError("P0 must not create an L3 intervention")
    if service.pending_approval("L3_INTERVENTION"):
        raise AssertionError("P0 must not request manager escalation approval")
    service.dispatch_all(session_id="dispatcher_2")

    service.advance_time("2026-08-06T10:00:00+10:00")
    risks_version = service.submit_artifact(
        "ai_risks",
        actor_id="owner_b",
        message_id="in_risks_v1",
        payload={
            "summary": "上线风险已整理",
            "content": "覆盖依赖、回滚和责任人，参考链接仅作为来源记录。",
            "dependencies": ["支付网关"],
            "rollback": "失败后切回 0.9",
            "owners": ["周宁"],
            "links": ["https://example.invalid/release-risk-source"],
        },
    )
    review_version(
        risks_version["version_id"],
        "risks_v1",
        approve=True,
        comment="依赖、回滚与责任人齐全，验收通过",
    )

    # B6: final organization is queued automatically from accepted result
    # fingerprints.  A later accepted task version supersedes the first final and
    # its approval; the replacement is generated without a manual aggregate click.
    first_final_step = worker.run_once()
    first_final_job = first_final_step.get("queued_outbox_id")
    first_final_result = first_final_step.get("result")
    if first_final_step.get("kind") != "FINAL_ORGANIZATION" or not first_final_job:
        raise AssertionError("all accepted tasks did not queue final organization")
    if not first_final_result or first_final_result["status"] != "DELIVERED":
        raise AssertionError("the first final organization did not finish")
    first_final_id = first_final_result["final_deliverable_id"]

    service.advance_time("2026-08-06T10:15:00+10:00")
    feedback_v2 = service.submit_artifact(
        "ai_feedback",
        actor_id="owner_a",
        message_id="in_feedback_v2_after_final",
        payload={
            "summary": "客户反馈补充版",
            "content": "补充重复反馈去重口径，分类与优先级保持不变。",
            "categories": ["体验", "稳定性"],
            "priorities": {"稳定性": "P0", "体验": "P1"},
        },
    )
    review_version(
        feedback_v2["version_id"],
        "feedback_v2_after_final",
        approve=True,
        comment="补充口径清晰，采用新版本替换旧终稿来源",
    )
    first_final = service.db.one(
        "SELECT status FROM final_deliverables WHERE final_deliverable_id = ?",
        (first_final_id,),
    )
    if not first_final or first_final["status"] != "SUPERSEDED":
        raise AssertionError("a newly accepted task version did not supersede the final")

    replacement_step = worker.run_once()
    replacement_job = replacement_step.get("queued_outbox_id")
    replacement_result = replacement_step.get("result")
    if replacement_step.get("kind") != "FINAL_ORGANIZATION" or not replacement_job:
        raise AssertionError("a replacement final was not queued automatically")
    if not replacement_result or replacement_result["status"] != "DELIVERED":
        raise AssertionError("replacement final organization did not finish")
    if replacement_result["final_deliverable_id"] == first_final_id:
        raise AssertionError("replacement final reused the superseded final id")

    final_approval = service.pending_approval("FINAL_RELEASE")
    if not final_approval:
        raise AssertionError("expected a final release approval")
    service.decide_approval(
        final_approval["approval_id"], actor_id="owner_lead", approve=True
    )
    service.dispatch_all(session_id="dispatcher_2")

    report = build_report(database, fixture)
    return report, service
