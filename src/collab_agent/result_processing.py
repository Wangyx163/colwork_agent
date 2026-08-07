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


RESULT_ORGANIZATION_PROMPT_VERSION = "final-organization.v2"


class ResultProcessingError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "PROCESSING_FAILED",
        stage: str = "FINAL_ORGANIZATION",
        retryable: bool = False,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.stage = stage
        self.retryable = retryable


def build_deterministic_report(source_bundle: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a readable, lossless report when semantic processing is unavailable."""
    sections: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for source in source_bundle:
        payload = source["payload"]
        accepted = source.get("accepted_task_result")
        if not isinstance(accepted, dict) or not accepted.get(
            "accepted_task_result_id"
        ):
            raise ResultProcessingError(
                "every source must contain a frozen accepted task result"
            )
        normalized = accepted.get("normalized_result")
        normalized = normalized if isinstance(normalized, dict) else {}
        summary = str(
            accepted.get("completion_report")
            or normalized.get("executive_summary")
            or payload.get("summary")
            or "已完成交付"
        ).strip()
        normalized_points = normalized.get("key_points") or []
        detail = "\n".join(
            str(item.get("text") or "").strip()
            for item in normalized_points
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        )
        if not detail:
            detail = str(payload.get("content") or "").strip()
        if not detail:
            detail = "\n\n".join(
                str(item.get("extracted_text") or "").strip()
                for item in source.get("attachments") or []
                if item.get("extracted_text")
            )
        version_id = source["version_id"]
        accepted_result_id = accepted["accepted_task_result_id"]
        sections.append(
            {
                "action_item_id": source["action_item_id"],
                "heading": str(normalized.get("title") or source["title"]),
                "summary": summary,
                "detail": detail,
                "links": list(payload.get("links") or []),
                "attachments": list(source.get("attachments") or []),
                "source_version_id": version_id,
                "accepted_task_result_id": accepted_result_id,
            }
        )
        findings.append(
            {
                "text": summary,
                "source_version_ids": [version_id],
                "source_result_ids": [accepted_result_id],
            }
        )
    return {
        "title": "会议行动项成果汇总",
        "executive_summary": f"本次共完成并验收 {len(source_bundle)} 项行动成果。",
        "key_findings": findings,
        "sections": sections,
        "risks_or_gaps": [],
        "recommended_next_steps": [],
    }


def normalize_organized_report_shape(
    candidate: Any, source_bundle: list[dict[str, Any]]
) -> tuple[Any, list[str]]:
    """Fill deterministic report fields while preserving semantic validation."""
    if not isinstance(candidate, dict):
        return candidate, []
    report = deepcopy(candidate)
    actions: list[str] = []
    deterministic = build_deterministic_report(source_bundle)
    source_by_action = {
        source["action_item_id"]: source for source in source_bundle
    }
    deterministic_sections = {
        section["action_item_id"]: section for section in deterministic["sections"]
    }

    if not str(report.get("title") or "").strip():
        report["title"] = deterministic["title"]
        actions.append("title<-deterministic_title")
    if not str(report.get("executive_summary") or "").strip():
        report["executive_summary"] = deterministic["executive_summary"]
        actions.append("executive_summary<-accepted_source_count")

    raw_sections = report.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        report["sections"] = deepcopy(deterministic["sections"])
        actions.append("sections<-deterministic_accepted_sources")
    else:
        for raw in raw_sections:
            if not isinstance(raw, dict):
                continue
            action_id = str(raw.get("action_item_id") or "")
            if action_id not in source_by_action:
                continue
            source = source_by_action[action_id]
            fallback = deterministic_sections[action_id]
            if not str(raw.get("heading") or "").strip():
                raw["heading"] = fallback["heading"]
                actions.append(f"sections.{action_id}.heading<-task_title")
            if not str(raw.get("summary") or "").strip():
                raw["summary"] = fallback["summary"]
                actions.append(f"sections.{action_id}.summary<-accepted_result")
            if raw.get("detail") is None:
                raw["detail"] = fallback["detail"]
                actions.append(f"sections.{action_id}.detail<-accepted_result")
            if not raw.get("source_version_id"):
                raw["source_version_id"] = source["version_id"]
                actions.append(f"sections.{action_id}.version_id<-source")
            accepted = source["accepted_task_result"]
            if not raw.get("accepted_task_result_id"):
                raw["accepted_task_result_id"] = accepted[
                    "accepted_task_result_id"
                ]
                actions.append(f"sections.{action_id}.result_id<-source")

    result_by_version = {
        source["version_id"]: source["accepted_task_result"][
            "accepted_task_result_id"
        ]
        for source in source_bundle
    }
    for field in ("key_findings", "risks_or_gaps", "recommended_next_steps"):
        if report.get(field) is None:
            report[field] = []
            actions.append(f"{field}<-empty_array")
            continue
        if not isinstance(report.get(field), list):
            continue
        for item in report[field]:
            if not isinstance(item, dict) or item.get("source_result_ids") is not None:
                continue
            version_ids = item.get("source_version_ids")
            if isinstance(version_ids, list) and set(version_ids) <= set(
                result_by_version
            ):
                item["source_result_ids"] = sorted(
                    {result_by_version[version_id] for version_id in version_ids}
                )
                actions.append(f"{field}.source_result_ids<-source_versions")
    return report, actions


def validate_organized_report(
    report: dict[str, Any], source_bundle: list[dict[str, Any]]
) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise ResultProcessingError("organized report must be an object")
    title = str(report.get("title") or "").strip()
    executive_summary = str(report.get("executive_summary") or "").strip()
    if not title or not executive_summary:
        raise ResultProcessingError("organized report title and summary are required")

    source_by_action = {item["action_item_id"]: item for item in source_bundle}
    if len(source_by_action) != len(source_bundle):
        raise ResultProcessingError("source bundle contains duplicate action items")
    result_by_version: dict[str, str] = {}
    for index, source in enumerate(source_bundle):
        accepted = source.get("accepted_task_result")
        if not isinstance(accepted, dict):
            raise ResultProcessingError(
                f"source_bundle[{index}] is missing accepted_task_result"
            )
        accepted_result_id = str(
            accepted.get("accepted_task_result_id") or ""
        ).strip()
        if not accepted_result_id:
            raise ResultProcessingError(
                f"source_bundle[{index}] is missing accepted_task_result_id"
            )
        result_by_version[source["version_id"]] = accepted_result_id
    known_versions = {item["version_id"] for item in source_bundle}
    raw_sections = report.get("sections")
    if not isinstance(raw_sections, list):
        raise ResultProcessingError("organized report sections must be an array")
    sections: list[dict[str, Any]] = []
    seen_actions: set[str] = set()
    for index, raw in enumerate(raw_sections):
        if not isinstance(raw, dict):
            raise ResultProcessingError(f"sections[{index}] must be an object")
        action_id = str(raw.get("action_item_id") or "")
        if action_id not in source_by_action or action_id in seen_actions:
            raise ResultProcessingError(f"sections[{index}] has an invalid action_item_id")
        source = source_by_action[action_id]
        if raw.get("source_version_id") != source["version_id"]:
            raise ResultProcessingError(f"sections[{index}] cites a stale version")
        expected_result_id = result_by_version[source["version_id"]]
        if raw.get("accepted_task_result_id") != expected_result_id:
            raise ResultProcessingError(
                f"sections[{index}] cites a stale accepted task result"
            )
        heading = str(raw.get("heading") or source["title"]).strip()
        summary = str(raw.get("summary") or "").strip()
        detail = str(raw.get("detail") or "").strip()
        if not heading or not summary:
            raise ResultProcessingError(f"sections[{index}] is missing heading or summary")
        sections.append(
            {
                "action_item_id": action_id,
                "heading": heading,
                "summary": summary,
                "detail": detail,
                "links": list(source["payload"].get("links") or []),
                "attachments": list(source.get("attachments") or []),
                "source_version_id": source["version_id"],
                "accepted_task_result_id": expected_result_id,
            }
        )
        seen_actions.add(action_id)
    if seen_actions != set(source_by_action):
        raise ResultProcessingError("organized report must contain every accepted action item")

    def cited_items(field: str) -> list[dict[str, Any]]:
        raw_items = report.get(field) or []
        if not isinstance(raw_items, list):
            raise ResultProcessingError(f"{field} must be an array")
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                raise ResultProcessingError(f"{field}[{index}] must be an object")
            text = str(raw.get("text") or "").strip()
            citations = raw.get("source_version_ids") or []
            if (
                not text
                or not isinstance(citations, list)
                or not citations
                or not set(citations) <= known_versions
            ):
                raise ResultProcessingError(f"{field}[{index}] has invalid citations")
            expected_result_ids = sorted(
                {result_by_version[version_id] for version_id in citations}
            )
            result_citations = raw.get("source_result_ids") or []
            if (
                not isinstance(result_citations, list)
                or sorted(set(result_citations)) != expected_result_ids
            ):
                raise ResultProcessingError(
                    f"{field}[{index}] has invalid accepted-result citations"
                )
            normalized.append(
                {
                    "text": text,
                    "source_version_ids": sorted(set(citations)),
                    "source_result_ids": expected_result_ids,
                }
            )
        return normalized

    return {
        "title": title,
        "executive_summary": executive_summary,
        "key_findings": cited_items("key_findings"),
        "sections": sections,
        "risks_or_gaps": cited_items("risks_or_gaps"),
        "recommended_next_steps": cited_items("recommended_next_steps"),
    }


def _system_prompt() -> str:
    return """你是办公项目成果整理器。输入只包含已经通过负责人验收的任务成果。

请把多项成果整理成一份可直接供项目负责人审批的中文终稿，严格遵守：
1. 只输出 JSON，不要 Markdown。
2. 不得补充输入中没有的事实、数字、链接或结论。
3. 每项关键发现、风险和下一步必须同时引用真实 source_version_ids 与对应的 source_result_ids。
4. sections 必须一项任务对应一节，action_item_id、source_version_id 与 accepted_task_result_id 原样返回且不可遗漏。
5. 可以消除重复表述、调整顺序、提炼共同结论和指出输入中明确存在的缺口。
6. 必须比较任务标题、交付物和实际正文；内容与任务不匹配时不得把它包装成有效结论，应在该章节和 risks_or_gaps 中明确指出。
7. 无法由输入支持的风险或下一步返回空数组。

返回结构：
{
  "title": "终稿标题",
  "executive_summary": "面向负责人的总体摘要",
  "key_findings": [{"text": "关键发现", "source_version_ids": ["version_id"], "source_result_ids": ["accepted_task_result_id"]}],
  "sections": [{
    "action_item_id": "原值",
    "heading": "章节标题",
    "summary": "本节结论",
    "detail": "整理后的正文",
    "source_version_id": "该任务的原值",
    "accepted_task_result_id": "该任务冻结验收结果的原值"
  }],
  "risks_or_gaps": [{"text": "风险或缺口", "source_version_ids": ["version_id"], "source_result_ids": ["accepted_task_result_id"]}],
  "recommended_next_steps": [{"text": "后续建议", "source_version_ids": ["version_id"], "source_result_ids": ["accepted_task_result_id"]}]
}"""


class BailianResultOrganizer:
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
            raise ResultProcessingError("DASHSCOPE_API_KEY is not configured")
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
            raise ResultProcessingError(
                f"Bailian returned HTTP {error.code}: {message}",
                error_code=(
                    "RATE_LIMIT"
                    if error.code == 429
                    else "PROVIDER_5XX"
                    if error.code >= 500
                    else "PROVIDER_4XX"
                ),
                stage="MODEL_REQUEST",
                retryable=error.code == 429 or error.code >= 500,
            ) from error
        except json.JSONDecodeError as error:
            raise ResultProcessingError(
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
            raise ResultProcessingError(
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
            except ResultProcessingError as error:
                if not error.retryable or attempt >= self.max_request_attempts:
                    raise
                time.sleep(self.retry_backoff_seconds * attempt)
        raise AssertionError("request retry loop must return or raise")

    @staticmethod
    def _content(response_payload: dict[str, Any]) -> str:
        try:
            content = response_payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ResultProcessingError(
                "Bailian returned an invalid report response envelope"
            ) from error
        if not isinstance(content, str) or not content.strip():
            raise ResultProcessingError("Bailian returned empty report content")
        return content

    @staticmethod
    def _usage(response_payloads: list[dict[str, Any]]) -> dict[str, int | None]:
        totals: dict[str, int | None] = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            values = [
                payload.get("usage", {}).get(key)
                for payload in response_payloads
                if isinstance(payload.get("usage"), dict)
            ]
            numeric = [value for value in values if isinstance(value, int)]
            totals[key] = sum(numeric) if numeric else None
        return totals

    def organize(
        self, source_bundle: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        messages = [
            {"role": "system", "content": _system_prompt()},
            {
                "role": "user",
                "content": "请整理以下已验收成果：\n"
                + json.dumps(source_bundle, ensure_ascii=False),
            },
        ]
        response_payloads = [self._request(messages)]
        content = self._content(response_payloads[0])
        validation_errors: list[str] = []
        normalization_actions: list[str] = []
        repair_count = 0
        try:
            candidate, actions = normalize_organized_report_shape(
                json.loads(content), source_bundle
            )
            normalization_actions.extend(actions)
            report = validate_organized_report(candidate, source_bundle)
        except (json.JSONDecodeError, ResultProcessingError) as first_error:
            validation_errors.append(str(first_error))
            repair_count = 1
            repair_messages = [
                *messages,
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        "上一次输出未通过系统契约校验。只修复 JSON 结构、必填字段"
                        "和来源引用，不得增加输入中没有的事实。请完整返回修复后的 "
                        "JSON 对象，不要解释。\n"
                        f"校验错误：{first_error}"
                    ),
                },
            ]
            repair_payload = self._request(repair_messages)
            response_payloads.append(repair_payload)
            repaired_content = self._content(repair_payload)
            try:
                repaired_candidate, actions = normalize_organized_report_shape(
                    json.loads(repaired_content), source_bundle
                )
                normalization_actions.extend(actions)
                report = validate_organized_report(
                    repaired_candidate, source_bundle
                )
            except (json.JSONDecodeError, ResultProcessingError) as repair_error:
                validation_errors.append(str(repair_error))
                raise ResultProcessingError(
                    "Bailian report contract remained invalid after one repair: "
                    f"{repair_error}"
                ) from repair_error
        response_payload = response_payloads[-1]
        return report, {
            "mode": "bailian",
            "provider": "bailian",
            "model": response_payload.get("model", self.model),
            "prompt_version": RESULT_ORGANIZATION_PROMPT_VERSION,
            "input_hash": stable_hash(source_bundle),
            "repair_count": repair_count,
            "request_attempts": sum(
                int(payload.get("_adapter_request_attempts", 1))
                for payload in response_payloads
            ),
            "normalization_actions": normalization_actions,
            "contract_validation_errors": validation_errors,
            "usage": self._usage(response_payloads),
        }
