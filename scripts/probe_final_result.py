from __future__ import annotations

import json

from collab_agent.models import stable_hash
from collab_agent.result_processing import (
    BailianResultOrganizer,
    RESULT_ORGANIZATION_PROMPT_VERSION,
)
from collab_agent.service import CoordinationService


def main() -> None:
    source_bundle = [
        {
            "action_item_id": "probe_task",
            "title": "整理会议后的测试结论",
            "owner": "测试执行人",
            "deliverable": "一段有来源的结论",
            "acceptance_criteria": "结论必须来自已验收正文",
            "deadline": "2026-08-07T10:00:00+10:00",
            "version_id": "probe_version_v1",
            "payload": {
                "summary": "测试已完成",
                "content": "提交确认：页面只在明确操作后刷新，输入草稿不会被定时刷新覆盖。",
                "links": [],
                "files": [],
            },
            "attachments": [],
            "accepted_task_result": {
                "accepted_task_result_id": "probe_result_v1",
                "accepted_version_id": "probe_version_v1",
                "completed_content_refs": ["submission:content"],
                "completion_report": "已验收：页面刷新和草稿保留符合测试要求。",
                "normalized_result": None,
                "source_manifest": {"binary_forwarded": False},
                "accepted_by": "probe_coordinator",
                "accepted_sim_time": "2026-08-06T10:00:00+10:00",
            },
        }
    ]
    report, metadata = BailianResultOrganizer().organize(source_bundle)
    print(
        json.dumps(
            {
                "mode": metadata["mode"],
                "model": metadata["model"],
                "prompt_version": metadata["prompt_version"],
                "repair_count": metadata.get("repair_count", 0),
                "normalization_actions": metadata.get(
                    "normalization_actions", []
                ),
                "section_version_id": report["sections"][0][
                    "source_version_id"
                ],
                "section_result_id": report["sections"][0][
                    "accepted_task_result_id"
                ],
                "finding_result_ids": (
                    report["key_findings"][0]["source_result_ids"]
                    if report["key_findings"]
                    else []
                ),
                "invocation": {
                    "capability_type": "MODEL",
                    "principal": {
                        "actor_id": "SYSTEM",
                        "episode_id": "diagnostic_probe",
                        "roles": ["SYSTEM"],
                        "auth_source": "READ_ONLY_DIAGNOSTIC",
                    },
                    "purpose": "FINAL_ORGANIZATION_DIAGNOSTIC",
                    "field_allowlist": CoordinationService.FINAL_ORGANIZATION_CONTEXT_FIELDS,
                    "entity_versions": {
                        "accepted_sources": [
                            {
                                "action_item_id": "probe_task",
                                "version_id": "probe_version_v1",
                                "accepted_task_result_id": "probe_result_v1",
                            }
                        ]
                    },
                    "prompt_version": RESULT_ORGANIZATION_PROMPT_VERSION,
                    "skill_version": None,
                    "input_hash": stable_hash(source_bundle),
                    "output_status": "SUCCEEDED",
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
