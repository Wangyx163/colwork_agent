from __future__ import annotations

import argparse
import json
from pathlib import Path

from collab_agent.cli import _database_url_from_local_env
from collab_agent.models import stable_hash
from collab_agent.postgres_store import PostgresDatabase
from collab_agent.service import CoordinationService
from collab_agent.task_result_processing import (
    BailianTaskResultProcessor,
    TASK_RESULT_PROMPT_VERSION,
    build_task_result_context,
)


def _json(value: object) -> dict:
    if isinstance(value, str):
        return json.loads(value)
    return dict(value or {})


def build_synthetic_context(*, contribution: bool = False) -> dict:
    """Build a stable, binary-free input that does not depend on project data."""
    evidence = "页面只在用户明确提交后刷新；未提交的表单草稿在刷新前会保留。"
    return build_task_result_context(
        action_item_id="probe_task",
        title="验证页面提交刷新与草稿保留",
        deliverable="一段说明刷新触发条件与草稿保留情况的测试结论",
        acceptance_criteria=(
            "必须同时说明页面何时刷新、未提交草稿是否保留，并引用已读取证据"
        ),
        source_timestamp="00:10:00",
        source_quote="请验证提交后的页面刷新和未提交草稿保留。",
        version_id="probe_version_v1",
        payload={
            "summary": "刷新与草稿保留测试已完成",
            "content": "测试结论记录在已读取的文本附件中。",
            "links": [],
            "completion_note": "用于百炼单任务成果处理契约烟测",
        },
        attachments=[
            {
                "name": "smoke-evidence.txt",
                "type": "text/plain",
                "size": len(evidence.encode("utf-8")),
                "extraction_status": "EXTRACTED",
                "text_characters": len(evidence),
                "truncated": False,
                "extracted_text": evidence,
            }
        ],
        previous_versions=[],
        work_requirements="只整理当前测试证据，不补造外部事实",
        management_review_policy="检查结论是否引用 attachment:0",
        submitted_by_actor_id=("probe_collaborator" if contribution else "probe_owner"),
        contributor_role=("REQUESTED_COLLABORATOR" if contribution else "OWNER"),
        processing_purpose=(
            "CONTRIBUTION_ANALYSIS" if contribution else "TASK_RESULT_REVIEW"
        ),
    )


def build_invocation(
    context: dict,
    *,
    episode_id: str,
    action_item_version: int,
    received_sequence: int,
    output_status: str,
) -> dict:
    return {
        "capability_type": "MODEL",
        "principal": {
            "actor_id": "SYSTEM",
            "episode_id": episode_id,
            "roles": ["SYSTEM"],
            "auth_source": "READ_ONLY_DIAGNOSTIC",
        },
        "purpose": (
            "CONTRIBUTION_ANALYSIS_DIAGNOSTIC"
            if context.get("processing_purpose") == "CONTRIBUTION_ANALYSIS"
            else "TASK_RESULT_REVIEW_DIAGNOSTIC"
        ),
        "field_allowlist": CoordinationService.TASK_RESULT_CONTEXT_FIELDS,
        "entity_versions": {
            "action_item_id": context["action_item_id"],
            "action_item_version": action_item_version,
            "artifact_version_id": context["version_id"],
            "artifact_received_sequence": received_sequence,
        },
        "prompt_version": TASK_RESULT_PROMPT_VERSION,
        "skill_version": None,
        "input_hash": stable_hash(context),
        "output_status": output_status,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only probe of one submitted task version through Bailian"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--synthetic",
        action="store_true",
        help="use an isolated fixed sample instead of reading PostgreSQL",
    )
    source.add_argument(
        "--title-contains",
        help="read exactly one accepted task from PostgreSQL by title fragment",
    )
    parser.add_argument(
        "--input-only",
        action="store_true",
        help="print the layered input without calling Bailian",
    )
    parser.add_argument(
        "--contribution",
        action="store_true",
        help="treat the synthetic version as a collaborator contribution",
    )
    parser.add_argument("--env", default=".env.local")
    args = parser.parse_args()

    database: PostgresDatabase | None = None
    try:
        if args.synthetic:
            context = build_synthetic_context(contribution=args.contribution)
            previous_versions: list[dict] = []
            episode_id = "diagnostic_probe"
            action_item_version = 1
            received_sequence = 1
        else:
            if args.contribution:
                parser.error("--contribution currently requires --synthetic")
            database = PostgresDatabase(_database_url_from_local_env(args.env))
            matches = database.all(
                "SELECT * FROM action_items WHERE title LIKE ? ORDER BY created_sim_time DESC",
                (f"%{args.title_contains}%",),
            )
            if len(matches) != 1:
                raise RuntimeError(
                    f"expected exactly one matching task, found {len(matches)}"
                )
            action = matches[0]
            version_id = action.get("current_valid_version_id")
            if not version_id:
                raise RuntimeError("matching task has no accepted current version")
            version = database.one(
                "SELECT * FROM artifact_versions WHERE version_id = ?", (version_id,)
            )
            if not version:
                raise RuntimeError("current version row is missing")

            payload = _json(version["payload"])
            metadata = CoordinationService.proposal_metadata(action)
            raw_attachments = version["attachment_extractions"]
            attachments = (
                json.loads(raw_attachments)
                if isinstance(raw_attachments, str)
                else list(raw_attachments or [])
            )
            previous_rows = database.all(
                "SELECT version_id, payload, review_status, review_comment "
                "FROM artifact_versions WHERE action_item_id = ? AND version_id <> ? "
                "ORDER BY received_sequence DESC",
                (action["action_item_id"], version_id),
            )
            previous_versions = [
                {
                    "version_id": row["version_id"],
                    "summary": _json(row["payload"]).get("summary", ""),
                    "review_status": row.get("review_status"),
                    "review_comment": row.get("review_comment"),
                }
                for row in previous_rows
            ]
            context = build_task_result_context(
                action_item_id=action["action_item_id"],
                title=action["title"],
                deliverable=str(metadata.get("deliverable") or ""),
                acceptance_criteria=str(metadata.get("acceptance_criteria") or ""),
                source_timestamp=str(metadata.get("source_timestamp") or ""),
                source_quote=str(
                    metadata.get("source_quote") or action.get("source_span") or ""
                ),
                version_id=version_id,
                payload=payload,
                attachments=attachments,
                previous_versions=previous_versions,
                work_requirements=str(metadata.get("work_requirements") or ""),
                management_review_policy=str(
                    metadata.get("management_review_policy") or ""
                ),
            )
            episode_id = action["episode_id"]
            action_item_version = action["version"]
            received_sequence = version["received_sequence"]

        invocation = build_invocation(
            context,
            episode_id=episode_id,
            action_item_version=action_item_version,
            received_sequence=received_sequence,
            output_status="NOT_INVOKED" if args.input_only else "SUCCEEDED",
        )
        if args.input_only:
            print(
                json.dumps(
                    {
                        "read_only": True,
                        "synthetic": args.synthetic,
                        "context": context,
                        "invocation": invocation,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        result, processing = BailianTaskResultProcessor().process(context)
        output = {
            "read_only": True,
            "input_layers": {
                "task_contract": context["task_contract"],
                "submission_claim": context["submission_claim"],
                "evidence_manifest": {
                    "links": context["evidence"]["links"],
                    "attachments": [
                        {
                            key: attachment[key]
                            for key in (
                                "source_ref",
                                "name",
                                "type",
                                "size",
                                "extraction_status",
                                "text_characters",
                                "truncated",
                            )
                        }
                        for attachment in context["evidence"]["attachments"]
                    ],
                },
                "previous_versions": previous_versions,
            },
            "processing": processing,
            "invocation": invocation,
            "result": result,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        if database is not None:
            database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
