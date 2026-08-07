from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request
from copy import deepcopy
from typing import Any

from .extraction import DEFAULT_BAILIAN_ENDPOINT, DEFAULT_BAILIAN_MODEL
from .models import stable_hash


TASK_RESULT_PROMPT_VERSION = "task-result-processing.v2"


class TaskResultProcessingError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "PROCESSING_FAILED",
        stage: str = "TASK_RESULT_PROCESSING",
        retryable: bool = False,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.stage = stage
        self.retryable = retryable


def build_task_result_context(
    *,
    action_item_id: str,
    title: str,
    deliverable: str,
    acceptance_criteria: str,
    source_timestamp: str,
    source_quote: str,
    version_id: str,
    payload: dict[str, Any],
    attachments: list[dict[str, Any]],
    previous_versions: list[dict[str, Any]] | None = None,
    work_requirements: str = "",
    management_review_policy: str = "",
    submitted_by_actor_id: str | None = None,
    contributor_role: str | None = None,
    processing_purpose: str = "TASK_RESULT_REVIEW",
    collaboration_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a binary-free, explicitly layered context for one task version."""
    links = [
        {
            "source_ref": f"link:{index}",
            "url": str(value),
            "inspection_status": "NOT_FETCHED",
        }
        for index, value in enumerate(payload.get("links") or [])
        if str(value).strip()
    ]
    evidence_attachments: list[dict[str, Any]] = []
    for index, raw in enumerate(attachments):
        evidence_attachments.append(
            {
                "source_ref": f"attachment:{index}",
                "name": raw.get("name"),
                "type": raw.get("type"),
                "size": raw.get("size"),
                "extraction_status": raw.get("extraction_status"),
                "text_characters": raw.get("text_characters", 0),
                "truncated": bool(raw.get("truncated", False)),
                "extracted_text": str(raw.get("extracted_text") or ""),
            }
        )

    return {
        "action_item_id": action_item_id,
        "version_id": version_id,
        "processing_purpose": processing_purpose,
        "task_contract": {
            "source_ref": "task:contract",
            "title": title,
            "deliverable": deliverable,
            "acceptance_criteria": acceptance_criteria,
            "work_requirements": work_requirements,
            "management_review_policy": management_review_policy,
            "meeting_source_timestamp": source_timestamp,
            "meeting_source_quote": source_quote,
        },
        "submission_claim": {
            "summary": str(payload.get("summary") or ""),
            "content": str(payload.get("content") or ""),
            "completion_note": str(payload.get("completion_note") or ""),
            "submitted_by_actor_id": submitted_by_actor_id,
            "contributor_role": contributor_role,
        },
        "evidence": {
            "links": links,
            "attachments": evidence_attachments,
        },
        "previous_version_context": list(previous_versions or []),
        "collaboration_inputs": dict(
            collaboration_inputs
            or {"upstream_results": [], "collective_decision": None}
        ),
        "source_rules": {
            "task_contract": "authoritative intent, not proof that the work was done",
            "submission_claim": "the submitter's description, not independently verified evidence",
            "attachment_text": "inspectable evidence extracted from the file; no binary is included",
            "link": "metadata only unless inspection_status is INSPECTED",
            "previous_version_context": "revision context only; not current evidence",
            "upstream_results": "accepted version-bound results from required upstream tasks",
            "collective_decision": "deterministically aggregated votes, not a replacement for the final owner's result",
        },
    }


def _require_text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise TaskResultProcessingError(f"{field} is required")
    return result


def normalize_task_result_shape(
    candidate: Any, context: dict[str, Any]
) -> tuple[Any, list[str]]:
    """Repair deterministic shape defects without inventing semantic claims."""
    if not isinstance(candidate, dict):
        return candidate, []
    result = deepcopy(candidate)
    actions: list[str] = []
    contract = context["task_contract"]
    alignment = result.get("task_alignment")
    status = str(alignment.get("status") or "") if isinstance(alignment, dict) else ""

    if not str(result.get("task_interpretation") or "").strip():
        result["task_interpretation"] = (
            f'任务“{contract.get("title") or ""}”要求交付：'
            f'{contract.get("deliverable") or ""}'
        )
        actions.append("task_interpretation<-task_contract")

    digest = result.get("evidence_digest")
    if not isinstance(digest, dict):
        readable = [
            item
            for item in context["evidence"]["attachments"]
            if item.get("extraction_status") == "EXTRACTED"
            and item.get("extracted_text")
        ]
        digest = {
            "title": str(contract.get("title") or "当前证据"),
            "summary": f"已读取 {len(readable)} 份附件文本",
            "key_points": [
                {
                    "text": str(item["extracted_text"])[:500],
                    "source_refs": [item["source_ref"]],
                }
                for item in readable
            ],
        }
        result["evidence_digest"] = digest
        actions.append("evidence_digest<-readable_input_evidence")
    if isinstance(digest, dict):
        if not str(digest.get("title") or "").strip():
            digest["title"] = str(contract.get("title") or "当前证据")
            actions.append("evidence_digest.title<-task_contract.title")
        if digest.get("key_points") is None:
            digest["key_points"] = []
            actions.append("evidence_digest.key_points<-empty_array")
        if not str(digest.get("summary") or "").strip():
            point_texts = [
                str(item.get("text") or "").strip()
                for item in digest.get("key_points") or []
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            ]
            digest["summary"] = (
                "；".join(point_texts[:3]) or "当前证据内容详见已引用要点"
            )
            actions.append("evidence_digest.summary<-cited_key_points")

    if status in {"MISALIGNED", "INSUFFICIENT"}:
        if result.get("normalized_result") is not None:
            result["normalized_result"] = None
            actions.append("normalized_result<-null_for_unusable_evidence")
    elif status in {"ALIGNED", "PARTIAL"}:
        normalized = result.get("normalized_result")
        if not isinstance(normalized, dict):
            normalized = {}
            result["normalized_result"] = normalized
            actions.append("normalized_result<-object_for_usable_evidence")
        digest = result.get("evidence_digest")
        digest = digest if isinstance(digest, dict) else {}
        if not str(normalized.get("title") or "").strip():
            normalized["title"] = str(
                digest.get("title") or contract.get("title") or "任务成果"
            )
            actions.append("normalized_result.title<-evidence_digest.title")
        if not str(normalized.get("executive_summary") or "").strip():
            normalized["executive_summary"] = str(
                normalized.get("summary")
                or digest.get("summary")
                or "当前成果内容详见已引用要点"
            )
            actions.append(
                "normalized_result.executive_summary<-evidence_digest.summary"
            )
        if normalized.get("key_points") is None:
            normalized["key_points"] = deepcopy(digest.get("key_points") or [])
            actions.append("normalized_result.key_points<-evidence_digest.key_points")

    if result.get("gaps") is None:
        result["gaps"] = []
        actions.append("gaps<-empty_array")

    advice = result.get("acceptance_advice")
    if not isinstance(advice, dict) and status in {
        "ALIGNED",
        "PARTIAL",
        "MISALIGNED",
        "INSUFFICIENT",
    }:
        advice = {
            "decision": "REVISE",
            "reasons": ["模型未返回验收建议，请负责人复核"],
        }
        result["acceptance_advice"] = advice
        actions.append("acceptance_advice<-safe_human_review")
    if isinstance(advice, dict) and not advice.get("reasons"):
        reason = str(alignment.get("reason") or "") if isinstance(alignment, dict) else ""
        advice["reasons"] = [reason or "模型未提供验收理由，请负责人复核"]
        actions.append("acceptance_advice.reasons<-alignment.reason")

    coverage = result.get("source_coverage")
    if not isinstance(coverage, dict):
        coverage = {}
        result["source_coverage"] = coverage
        actions.append("source_coverage<-input_manifest")
    if isinstance(coverage, dict):
        if coverage.get("used_refs") is None:
            cited_refs: set[str] = {"task:contract", "submission:claim"}
            for section in (
                result.get("evidence_digest"),
                result.get("normalized_result"),
            ):
                if not isinstance(section, dict):
                    continue
                for point in section.get("key_points") or []:
                    if isinstance(point, dict):
                        cited_refs.update(
                            str(ref) for ref in point.get("source_refs") or []
                        )
            coverage["used_refs"] = sorted(cited_refs)
            actions.append("source_coverage.used_refs<-cited_refs")
        if coverage.get("unreadable_or_uninspected") is None:
            coverage["unreadable_or_uninspected"] = [
                item["source_ref"]
                for item in context["evidence"]["attachments"]
                if item.get("extraction_status") != "EXTRACTED"
                or not item.get("extracted_text")
            ] + [
                item["source_ref"]
                for item in context["evidence"]["links"]
                if item.get("inspection_status") != "INSPECTED"
            ]
            actions.append("source_coverage.unreadable<-input_manifest")
    return result, actions


def validate_task_result(
    result: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise TaskResultProcessingError("task result must be an object")

    alignment = result.get("task_alignment")
    if not isinstance(alignment, dict):
        raise TaskResultProcessingError("task_alignment must be an object")
    status = str(alignment.get("status") or "")
    if status not in {"ALIGNED", "PARTIAL", "MISALIGNED", "INSUFFICIENT"}:
        raise TaskResultProcessingError("task_alignment.status is invalid")
    reason = _require_text(alignment.get("reason"), "task_alignment.reason")
    try:
        confidence = float(alignment.get("confidence"))
    except (TypeError, ValueError) as error:
        raise TaskResultProcessingError(
            "task_alignment.confidence must be a number"
        ) from error
    if not 0 <= confidence <= 1:
        raise TaskResultProcessingError(
            "task_alignment.confidence must be between 0 and 1"
        )

    evidence_refs = {
        attachment["source_ref"]
        for attachment in context["evidence"]["attachments"]
        if attachment["extraction_status"] == "EXTRACTED"
        and attachment["extracted_text"]
    }
    inspected_link_refs = {
        link["source_ref"]
        for link in context["evidence"]["links"]
        if link["inspection_status"] == "INSPECTED"
    }
    usable_evidence_refs = evidence_refs | inspected_link_refs
    collaboration_inputs = context.get("collaboration_inputs") or {}
    upstream_refs = {
        str(item.get("source_ref"))
        for item in collaboration_inputs.get("upstream_results") or []
        if item.get("source_ref") and item.get("accepted_version_id")
    }
    collective_decision = collaboration_inputs.get("collective_decision")
    decision_refs = (
        {str(collective_decision.get("source_ref"))}
        if isinstance(collective_decision, dict)
        and collective_decision.get("source_ref")
        and collective_decision.get("required_vote_count")
        == collective_decision.get("submitted_vote_count")
        else set()
    )
    collaboration_evidence_refs = upstream_refs | decision_refs
    usable_evidence_refs |= collaboration_evidence_refs
    descriptive_source_refs = {
        attachment["source_ref"]
        for attachment in context["evidence"]["attachments"]
    } | {
        link["source_ref"] for link in context["evidence"]["links"]
    }
    known_refs = (
        descriptive_source_refs
        | collaboration_evidence_refs
        | {"task:contract", "submission:claim"}
    )

    def evidence_points(raw_points: Any, field: str) -> list[dict[str, Any]]:
        if not isinstance(raw_points, list):
            raise TaskResultProcessingError(f"{field} must be an array")
        points: list[dict[str, Any]] = []
        for index, item in enumerate(raw_points):
            if not isinstance(item, dict):
                raise TaskResultProcessingError(f"{field}[{index}] must be an object")
            text = _require_text(item.get("text"), f"{field}[{index}].text")
            refs = item.get("source_refs")
            if not isinstance(refs, list) or not refs or not set(refs) <= usable_evidence_refs:
                raise TaskResultProcessingError(
                    f"{field}[{index}] must cite only inspected evidence"
                )
            points.append({"text": text, "source_refs": sorted(set(refs))})
        return points

    evidence_digest = result.get("evidence_digest")
    if not isinstance(evidence_digest, dict):
        raise TaskResultProcessingError("evidence_digest must be an object")
    digest_title = _require_text(evidence_digest.get("title"), "evidence_digest.title")
    digest_summary = _require_text(
        evidence_digest.get("summary"), "evidence_digest.summary"
    )
    digest_points = evidence_points(
        evidence_digest.get("key_points"), "evidence_digest.key_points"
    )

    normalized = result.get("normalized_result")
    if status in {"MISALIGNED", "INSUFFICIENT"}:
        if normalized is not None:
            raise TaskResultProcessingError(
                "misaligned or insufficient evidence cannot produce a normalized task result"
            )
        normalized_result = None
    else:
        if not isinstance(normalized, dict):
            raise TaskResultProcessingError(
                "aligned or partial evidence requires normalized_result"
            )
        normalized_result = {
            "title": _require_text(normalized.get("title"), "normalized_result.title"),
            "executive_summary": _require_text(
                normalized.get("executive_summary"),
                "normalized_result.executive_summary",
            ),
            "key_points": evidence_points(
                normalized.get("key_points"), "normalized_result.key_points"
            ),
        }

    raw_gaps = result.get("gaps")
    if not isinstance(raw_gaps, list):
        raise TaskResultProcessingError("gaps must be an array")
    gaps: list[dict[str, Any]] = []
    for index, item in enumerate(raw_gaps):
        if not isinstance(item, dict):
            raise TaskResultProcessingError(f"gaps[{index}] must be an object")
        severity = str(item.get("severity") or "")
        if severity not in {"BLOCKING", "MAJOR", "MINOR"}:
            raise TaskResultProcessingError(f"gaps[{index}].severity is invalid")
        refs = item.get("source_refs") or []
        invalid_refs = sorted(set(refs) - known_refs) if isinstance(refs, list) else []
        if not isinstance(refs, list) or invalid_refs:
            raise TaskResultProcessingError(
                f"gaps[{index}] has invalid source_refs: {invalid_refs}; "
                f"allowed refs: {sorted(known_refs)}"
            )
        gaps.append(
            {
                "issue": _require_text(item.get("issue"), f"gaps[{index}].issue"),
                "severity": severity,
                "source_refs": sorted(set(refs)),
            }
        )

    advice = result.get("acceptance_advice")
    if not isinstance(advice, dict):
        raise TaskResultProcessingError("acceptance_advice must be an object")
    decision = str(advice.get("decision") or "")
    if decision not in {"ACCEPT", "REVISE"}:
        raise TaskResultProcessingError("acceptance_advice.decision is invalid")
    reasons = advice.get("reasons")
    if not isinstance(reasons, list) or not reasons:
        raise TaskResultProcessingError("acceptance_advice.reasons must be a non-empty array")
    if status in {"MISALIGNED", "INSUFFICIENT"} and decision != "REVISE":
        raise TaskResultProcessingError("misaligned or insufficient work must be revised")

    coverage = result.get("source_coverage")
    if not isinstance(coverage, dict):
        raise TaskResultProcessingError("source_coverage must be an object")
    used_refs = coverage.get("used_refs") or []
    unreadable = coverage.get("unreadable_or_uninspected") or []
    if not isinstance(used_refs, list) or not set(used_refs) <= known_refs:
        raise TaskResultProcessingError("source_coverage.used_refs is invalid")
    if not isinstance(unreadable, list):
        raise TaskResultProcessingError(
            "source_coverage.unreadable_or_uninspected must be an array"
        )

    return {
        "task_interpretation": _require_text(
            result.get("task_interpretation"), "task_interpretation"
        ),
        "task_alignment": {
            "status": status,
            "reason": reason,
            "confidence": confidence,
        },
        "evidence_digest": {
            "title": digest_title,
            "summary": digest_summary,
            "key_points": digest_points,
        },
        "normalized_result": normalized_result,
        "gaps": gaps,
        "acceptance_advice": {
            "decision": decision,
            "reasons": [_require_text(value, "acceptance_advice.reasons[]") for value in reasons],
        },
        "source_coverage": {
            "used_refs": sorted(set(used_refs)),
            "unreadable_or_uninspected": [str(value) for value in unreadable],
        },
    }


_USER_FRAMING = "请处理以下单任务当前版本：\n"
# Chat framing the provider adds around each message (role markers, separators).
# Small, but it belongs on the reserved side rather than silently over-running.
_CHAT_FRAMING_TOKENS = 16


def task_result_prompt_overhead_tokens() -> int:
    """Tokens every task-result request spends before any context is added.

    The budget guard previously measured only the context object while the
    request also carried this system prompt, so a context could pass the guard
    and still put the real request far over budget.
    """

    from .context_budget import count_text_tokens

    return (
        count_text_tokens(_system_prompt())
        + count_text_tokens(_USER_FRAMING)
        + _CHAT_FRAMING_TOKENS
    )


def _system_prompt() -> str:
    return """你是办公协作系统中的“单任务成果处理器”，只处理一个任务的一个提交版本。
你的工作不是替同事补做任务，而是把当前提交整理成可验收结果，并判断它是否满足任务契约。

输入严格分层：
1. task_contract 是权威任务意图；其中执行要求、公开验收标准和管理侧复核规则保持分层，但都不是完成证据。
2. submission_claim 是提交人的自述，不能自动当成已证实事实。
3. evidence.attachments 的 extracted_text 才是已读取的附件证据；系统没有发送附件二进制。
4. evidence.links 如果 inspection_status 不是 INSPECTED，只是链接元数据，不得声称读过网页。
5. previous_version_context 只帮助理解返修过程，不能替代当前版本证据。
6. collaboration_inputs.upstream_results 只包含已由会议负责人验收并绑定版本的上游任务结果，可以作为当前结构化任务的输入证据。
7. collaboration_inputs.collective_decision 是确定性汇总的完整投票结果；它可以说明参与选择，但不能替最终负责人完成定稿。

若 processing_purpose=CONTRIBUTION_ANALYSIS，当前版本只是协作者对任务的阶段性贡献：
- 仍要按完整 task_contract 判断覆盖范围，以便负责人知道它能解决什么、还缺什么；
- PARTIAL 是正常且有价值的结果，不得把它表述为整项任务已经提交或已经结束；
- acceptance_advice 表示“若把该贡献直接作为最终候选是否足够”，不执行任何任务状态变化。

规则：
- 只输出 JSON，不要 Markdown。
- 不使用外部知识，不上网，不补造输入里没有的事实、数字、来源或结论。
- 必须比较任务标题、交付物、验收标准、提交自述和证据内容；不要把它们盲目合并。
- task_interpretation 只能复述 task_contract 的原文含义，不得加入原文没有写出的领域知识、技术例子或隐含验收项。若任务定义含糊，应在 gaps 中指出，不能自行补全。
- evidence_digest 只概括当前证据实际是什么，不表示它已经满足任务。
- 只有 ALIGNED 或 PARTIAL 才能生成 normalized_result；MISALIGNED 或 INSUFFICIENT 时 normalized_result 必须为 null，避免不相关内容污染任务成果和终稿。
- evidence_digest 与 normalized_result 的每个 key_point 都必须引用 attachment:N、已读取的 link:N、已验收 upstream:* 或完整的 decision:votes。
- gaps 可以引用 task:contract、submission:claim，以及输入中存在但未读取/未检查的 attachment:N 或 link:N；引用未检查来源时只能说明“已提供但未检查”或“证据不足”，不能声称知道其内容。
- P0 不核验外部链接真实性。链接的存在可以作为提交元数据，但 inspection_status 不是 INSPECTED 时不能把网页内容写成已证实事实。
- 若附件内容与任务不相关，必须判为 MISALIGNED 并建议 REVISE；不要把附件改写成符合任务标题的报告。
- 若证据过少而无法判断，判为 INSUFFICIENT；只覆盖部分要求时判为 PARTIAL。
- gaps 只能描述 task_contract 原文与当前证据之间可直接观察的差异；不得枚举合同未要求的假设性技术内容。
- source_coverage 必须明确哪些来源实际使用，哪些附件或链接没有读取。

输出结构：
{
  "task_interpretation": "对本任务实际要求的简洁解释",
  "task_alignment": {
    "status": "ALIGNED|PARTIAL|MISALIGNED|INSUFFICIENT",
    "reason": "判断理由",
    "confidence": 0.0
  },
  "evidence_digest": {
    "title": "当前证据实际内容的标题",
    "summary": "当前证据实际内容的摘要",
    "key_points": [{"text": "证据支持的要点", "source_refs": ["attachment:0"]}]
  },
  "normalized_result": {
    "title": "只基于已读取证据的成果标题",
    "executive_summary": "只基于已读取证据的成果摘要",
    "key_points": [{"text": "证据支持的成果要点", "source_refs": ["attachment:0"]}]
  },
  "gaps": [{
    "issue": "与任务要求之间的具体缺口",
    "severity": "BLOCKING|MAJOR|MINOR",
    "source_refs": ["task:contract", "attachment:0"]
  }],
  "acceptance_advice": {
    "decision": "ACCEPT|REVISE",
    "reasons": ["可直接发给负责人参考的理由"]
  },
  "source_coverage": {
    "used_refs": ["task:contract", "submission:claim", "attachment:0"],
    "unreadable_or_uninspected": ["link:0"]
  }
}
当 status 为 MISALIGNED 或 INSUFFICIENT 时，上面的 normalized_result 必须改为 null。"""


def build_deterministic_task_result(
    context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Safe local fallback: organize inspectable facts without semantic approval."""
    attachments = [
        item
        for item in context["evidence"]["attachments"]
        if item.get("extraction_status") == "EXTRACTED"
        and item.get("extracted_text")
    ]
    points = [
        {
            "text": str(item["extracted_text"])[:500],
            "source_refs": [item["source_ref"]],
        }
        for item in attachments
    ]
    claim = context["submission_claim"]
    contract = context["task_contract"]
    if points:
        status = "PARTIAL"
        normalized_result: dict[str, Any] | None = {
            "title": str(contract["title"]),
            "executive_summary": str(
                claim.get("summary") or "已提取附件文本，等待负责人验收"
            ),
            "key_points": points,
        }
        gap_issue = "本地处理只验证了来源可读性，未做模型语义对齐判断"
    else:
        status = "INSUFFICIENT"
        normalized_result = None
        gap_issue = "当前版本没有可读取的附件证据，链接也尚未检查"
    raw_result = {
        "task_interpretation": (
            f'任务“{contract["title"]}”要求交付：{contract["deliverable"]}'
        ),
        "task_alignment": {
            "status": status,
            "reason": gap_issue,
            "confidence": 0.5,
        },
        "evidence_digest": {
            "title": "当前版本可检查内容",
            "summary": (
                f"已读取 {len(points)} 份附件文本"
                if points
                else "没有可读取的附件证据"
            ),
            "key_points": points,
        },
        "normalized_result": normalized_result,
        "gaps": [
            {
                "issue": gap_issue,
                "severity": "MAJOR" if points else "BLOCKING",
                "source_refs": ["task:contract"],
            }
        ],
        "acceptance_advice": {
            "decision": "REVISE",
            "reasons": ["请负责人依据原始提交与来源人工验收"],
        },
        "source_coverage": {
            "used_refs": [
                "task:contract",
                "submission:claim",
                *(item["source_ref"] for item in attachments),
            ],
            "unreadable_or_uninspected": [
                item["source_ref"]
                for item in context["evidence"]["attachments"]
                if item not in attachments
            ]
            + [item["source_ref"] for item in context["evidence"]["links"]],
        },
    }
    result = validate_task_result(raw_result, context)
    return result, {
        "mode": "deterministic_local",
        "provider": "local",
        "model": None,
        "prompt_version": TASK_RESULT_PROMPT_VERSION,
        "input_hash": stable_hash(context),
        "usage": {},
    }


class BailianTaskResultProcessor:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
        timeout_seconds: int = 180,
        max_request_attempts: int = 3,
        retry_backoff_seconds: float = 0.5,
    ):
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not self.api_key:
            raise TaskResultProcessingError(
                "DASHSCOPE_API_KEY is not configured",
                error_code="AUTH_CONFIG",
                stage="MODEL_REQUEST",
            )
        self.model = model or os.getenv("BAILIAN_MODEL", DEFAULT_BAILIAN_MODEL)
        self.endpoint = endpoint or os.getenv(
            "DASHSCOPE_CHAT_COMPLETIONS_URL", DEFAULT_BAILIAN_ENDPOINT
        )
        self.timeout_seconds = timeout_seconds
        self.max_request_attempts = max(1, int(max_request_attempts))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))

    def _request_once(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "colwork-agent-p0/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            message = error.read().decode("utf-8", errors="replace")[:1000]
            error_code = (
                "RATE_LIMIT"
                if error.code == 429
                else "PROVIDER_5XX"
                if error.code >= 500
                else "PROVIDER_4XX"
            )
            raise TaskResultProcessingError(
                f"Bailian returned HTTP {error.code}: {message}",
                error_code=error_code,
                stage="MODEL_REQUEST",
                retryable=error.code == 429 or error.code >= 500,
            ) from error
        except json.JSONDecodeError as error:
            raise TaskResultProcessingError(
                "Bailian returned a non-JSON HTTP response",
                error_code="INVALID_JSON",
                stage="MODEL_RESPONSE",
                retryable=True,
            ) from error
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ) as error:
            raise TaskResultProcessingError(
                f"Bailian request failed: {error}",
                error_code="NETWORK_TIMEOUT",
                stage="MODEL_REQUEST",
                retryable=True,
            ) from error
        return response_payload

    def _request(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        for attempt in range(1, self.max_request_attempts + 1):
            try:
                response_payload = self._request_once(messages)
                response_payload["_adapter_request_attempts"] = attempt
                return response_payload
            except TaskResultProcessingError as error:
                if not error.retryable or attempt >= self.max_request_attempts:
                    raise
                time.sleep(self.retry_backoff_seconds * attempt)
        raise AssertionError("request retry loop must return or raise")

    @staticmethod
    def _content(response_payload: dict[str, Any]) -> str:
        try:
            content = response_payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise TaskResultProcessingError(
                "Bailian returned an invalid task-result response envelope",
                error_code="INVALID_JSON",
                stage="MODEL_RESPONSE",
                retryable=True,
            ) from error
        if not isinstance(content, str) or not content.strip():
            raise TaskResultProcessingError(
                "Bailian returned empty task-result content",
                error_code="INVALID_JSON",
                stage="MODEL_RESPONSE",
                retryable=True,
            )
        return content

    @staticmethod
    def _usage(response_payloads: list[dict[str, Any]]) -> dict[str, int | None]:
        """Totals are what the attempt costs; the first request is what the
        pre-call estimate can be compared against.

        A repair re-sends the whole prompt, so summing both requests conflates
        cost with prompt size.  Keeping them apart is what makes the estimate
        auditable instead of merely plausible.
        """

        totals: dict[str, int | None] = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            values = [
                payload.get("usage", {}).get(key)
                for payload in response_payloads
                if isinstance(payload.get("usage"), dict)
            ]
            numeric = [value for value in values if isinstance(value, int)]
            totals[key] = sum(numeric) if numeric else None
        totals["request_count"] = len(response_payloads)
        first_usage = next(
            (
                payload["usage"]
                for payload in response_payloads
                if isinstance(payload.get("usage"), dict)
            ),
            {},
        )
        first_prompt = first_usage.get("prompt_tokens")
        totals["first_request_prompt_tokens"] = (
            first_prompt if isinstance(first_prompt, int) else None
        )
        return totals

    def process(
        self, context: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        messages = [
            {"role": "system", "content": _system_prompt()},
            {
                "role": "user",
                "content": _USER_FRAMING + json.dumps(context, ensure_ascii=False),
            },
        ]
        response_payloads = [self._request(messages)]
        content = self._content(response_payloads[0])
        validation_errors: list[str] = []
        normalization_actions: list[str] = []
        repair_count = 0

        try:
            candidate, actions = normalize_task_result_shape(
                json.loads(content), context
            )
            normalization_actions.extend(actions)
            result = validate_task_result(candidate, context)
        except (json.JSONDecodeError, TaskResultProcessingError) as first_error:
            validation_errors.append(str(first_error))
            repair_count = 1
            repair_messages = [
                *messages,
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        "上一次输出未通过系统契约校验。只修复 JSON 结构和字段间的"
                        "直接冲突，不得增加输入中没有的事实。允许删除或替换非法 "
                        "source_refs，但只能使用输入中真实存在的来源标识；不得把未读取"
                        "链接或附件改写成已证实事实。"
                        "请完整返回修复后的 JSON 对象，不要解释。\n"
                        f"校验错误：{first_error}"
                    ),
                },
            ]
            repair_payload = self._request(repair_messages)
            response_payloads.append(repair_payload)
            repaired_content = self._content(repair_payload)
            try:
                repaired_candidate, actions = normalize_task_result_shape(
                    json.loads(repaired_content), context
                )
                normalization_actions.extend(actions)
                result = validate_task_result(repaired_candidate, context)
            except (json.JSONDecodeError, TaskResultProcessingError) as repair_error:
                validation_errors.append(str(repair_error))
                raise TaskResultProcessingError(
                    "Bailian task-result contract remained invalid after one repair: "
                    f"{repair_error}",
                    error_code="INVALID_SCHEMA",
                    stage="MODEL_RESPONSE",
                    retryable=False,
                ) from repair_error
        response_payload = response_payloads[-1]
        return result, {
            "mode": "bailian",
            "provider": "bailian",
            "model": response_payload.get("model", self.model),
            "prompt_version": TASK_RESULT_PROMPT_VERSION,
            "input_hash": stable_hash(context),
            "repair_count": repair_count,
            "request_attempts": sum(
                int(payload.get("_adapter_request_attempts", 1))
                for payload in response_payloads
            ),
            "normalization_actions": normalization_actions,
            "contract_validation_errors": validation_errors,
            "usage": self._usage(response_payloads),
        }
