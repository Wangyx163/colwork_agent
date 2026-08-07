from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from copy import deepcopy
from collections.abc import Callable
from typing import Any

from .extraction import DEFAULT_BAILIAN_ENDPOINT
from .context_budget import estimate_tokens, token_estimator_name
from .models import canonical_json, stable_hash


BALLOT_PROMPT_VERSION = "question-ballot-draft.v1"
DEFAULT_BALLOT_MODEL = "qwen-plus"
DEFAULT_BALLOT_TOKEN_BUDGET = 8_000


class QuestionBallotDraftError(RuntimeError):
    pass


def _ballot_token_budget() -> int:
    raw = os.getenv("COLWORK_BALLOT_DRAFT_TOKEN_BUDGET")
    if raw is None:
        return DEFAULT_BALLOT_TOKEN_BUDGET
    try:
        budget = int(raw)
    except ValueError as error:
        raise QuestionBallotDraftError(
            "COLWORK_BALLOT_DRAFT_TOKEN_BUDGET must be an integer"
        ) from error
    if budget <= 0:
        raise QuestionBallotDraftError(
            "COLWORK_BALLOT_DRAFT_TOKEN_BUDGET must be positive"
        )
    return budget


def _truncate_longest(
    bounded: list[dict[str, Any]],
    envelope: dict[str, Any],
    budget: int,
    *,
    reader: Callable[[dict[str, Any]], str],
    writer: Callable[[dict[str, Any], str], None],
) -> bool:
    """Binary-search the tail cut on whichever source holds the longest text."""

    candidates = [
        (len(reader(source)), source) for source in bounded if reader(source)
    ]
    if not candidates:
        return False
    _, source = max(candidates, key=lambda item: item[0])
    original = reader(source)
    low, high = 0, len(original)
    while low < high:
        middle = (low + high + 1) // 2
        writer(source, original[:middle])
        if estimate_tokens(envelope) <= budget:
            low = middle
        else:
            high = middle - 1
    writer(source, original[:low])
    return True


def _attachment_text(source: dict[str, Any]) -> str:
    texts = source.get("attachment_texts") or []
    return "".join(str(item.get("extracted_text") or "") for item in texts)


def _set_attachment_text(source: dict[str, Any], value: str) -> None:
    texts = source.get("attachment_texts") or []
    remaining = len(value)
    for item in texts:
        text = str(item.get("extracted_text") or "")
        item["extracted_text"] = text[: max(0, remaining)]
        remaining -= len(item["extracted_text"])


def _bound_model_sources(
    sources: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    budget = _ballot_token_budget()
    bounded = deepcopy(sources)
    envelope = {"sources": bounded}
    estimated_before = estimate_tokens(envelope)
    omitted_refs: list[str] = []
    strategies: list[str] = []
    for source in bounded:
        if estimate_tokens(envelope) <= budget:
            break
        if source.get("normalized_result") is not None:
            source["normalized_result"] = None
            omitted_refs.append(f'{source["source_ref"]}:normalized_result')
            if "DROP_DUPLICATE_NORMALIZED_RESULT" not in strategies:
                strategies.append("DROP_DUPLICATE_NORMALIZED_RESULT")
    # Trim in order of decreasing redundancy. The submitted content carries the
    # candidate questions themselves, so it is the last thing to lose.
    for label, reader, writer in (
        (
            "completion_report_tail",
            lambda source: str(source.get("completion_report") or ""),
            lambda source, value: source.update({"completion_report": value}),
        ),
        ("attachment_text_tail", _attachment_text, _set_attachment_text),
        (
            "submitted_content_tail",
            lambda source: str(source.get("submitted_content") or ""),
            lambda source, value: source.update({"submitted_content": value}),
        ),
    ):
        while estimate_tokens(envelope) > budget:
            if not _truncate_longest(
                bounded, envelope, budget, reader=reader, writer=writer
            ):
                break
            omitted_refs.append(label)
            strategy = f"TRUNCATE_LONGEST_{label.upper()}"
            if strategy not in strategies:
                strategies.append(strategy)
    estimated_after = estimate_tokens(envelope)
    if estimated_after > budget:
        raise QuestionBallotDraftError(
            "mandatory ballot source metadata exceeds the configured token budget"
        )
    return bounded, {
        "context_manifest_version": "context-manifest.v1",
        "purpose": "QUESTION_BALLOT_DRAFT",
        "token_budget": budget,
        "estimated_input_tokens_before": estimated_before,
        "estimated_input_tokens": estimated_after,
        "included_refs": [source["source_ref"] for source in bounded],
        "omitted_refs": omitted_refs,
        "truncation_strategy": strategies,
        "truncated": bool(omitted_refs),
        "estimator": token_estimator_name(),
    }


def _usage_summary(responses: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for response in responses:
        usage = response.get("usage") or {}
        for key in summary:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                summary[key] += value
    return summary


def _candidate_key(text: str) -> str:
    return re.sub(r"[\s，。！？?；;：:、‘’“”\"'（）()【】\[\]]+", "", text).lower()


def _clean_candidate(text: str) -> str:
    text = re.sub(
        r"^\s*(?:[-*•]+|(?:问题)?[（(]?\d{1,3}[）).、:]|[一二三四五六七八九十]+[、.])\s*",
        "",
        str(text or ""),
    ).strip(" \t-—;；,，。")
    return text[:1000]


def _text_candidates(text: str) -> list[str]:
    """Split a delivery body into candidate questions.

    A question list is written one item per line, so lines are the unit. When
    a body is prose instead, interrogative sentences are pulled out. Splitting
    further on inner punctuation is deliberately not done: it shreds a single
    sentence into fragments like "反应问题" and "促互动'标准".
    """

    text = str(text or "").replace("\r", "\n").strip()
    if not text:
        return []
    questions = [
        _clean_candidate(match)
        for match in re.findall(r"[^。！!？?\n]{2,300}[？?]", text)
    ]
    questions = [item for item in questions if 2 <= len(item) <= 1000]
    if len(questions) >= 2:
        return questions
    candidates: list[str] = list(questions)
    for line in re.split(r"\n+", text):
        cleaned = _clean_candidate(line)
        if 2 <= len(cleaned) <= 1000:
            candidates.append(cleaned)
    return candidates


def _source_texts(source: dict[str, Any]) -> list[str]:
    """Order inputs so the actual delivery outranks reports about it."""

    texts: list[str] = []
    submitted = str(source.get("submitted_content") or "")
    if submitted:
        texts.append(submitted)
    for item in source.get("attachment_texts") or []:
        extracted = str(item.get("extracted_text") or "")
        if extracted:
            texts.append(extracted)
    if texts:
        return texts
    texts.append(str(source.get("completion_report") or ""))
    normalized = source.get("normalized_result")
    if isinstance(normalized, dict):
        for key in (
            "executive_summary",
            "summary",
            "content",
            "completed_content",
        ):
            value = normalized.get(key)
            if isinstance(value, str):
                texts.append(value)
    return texts


def deterministic_ballot_options(
    upstream_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Extract inspectable candidates without a model; preserve accepted lineage."""

    by_key: dict[str, dict[str, Any]] = {}
    for source in upstream_results:
        action_item_id = str(source.get("action_item_id") or "")
        version_id = str(source.get("accepted_version_id") or "")
        source_ref = {
            "action_item_id": action_item_id,
            "version_id": version_id,
        }
        for text in _source_texts(source):
            for candidate in _text_candidates(text):
                key = _candidate_key(candidate)
                if len(key) < 2:
                    continue
                existing = by_key.get(key)
                if existing:
                    if source_ref not in existing["source_refs"]:
                        existing["source_refs"].append(source_ref)
                    continue
                by_key[key] = {
                    "option_id": f"q_{stable_hash({'text': key})[:12]}",
                    "text": candidate,
                    "source_action_item_id": action_item_id,
                    "source_refs": [source_ref],
                }
    return list(by_key.values())[:100]


def _normalize_model_payload(
    payload: Any,
    upstream_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        raw_options = payload
    elif isinstance(payload, dict):
        raw_options = (
            payload.get("options")
            or payload.get("questions")
            or payload.get("candidates")
            or []
        )
    else:
        raw_options = []
    source_by_ref = {
        str(item.get("source_ref")): item for item in upstream_results
    }
    by_key: dict[str, dict[str, Any]] = {}
    for raw in raw_options:
        if not isinstance(raw, dict):
            continue
        text = _clean_candidate(
            raw.get("text") or raw.get("question") or raw.get("content")
        )
        refs = raw.get("source_refs") or raw.get("sources") or raw.get("source_ref")
        if isinstance(refs, str):
            refs = [refs]
        if not isinstance(refs, list):
            refs = []
        resolved: list[dict[str, str]] = []
        for ref in refs:
            ref_value = str(ref.get("source_ref") if isinstance(ref, dict) else ref)
            source = source_by_ref.get(ref_value)
            if not source:
                continue
            resolved.append(
                {
                    "action_item_id": str(source["action_item_id"]),
                    "version_id": str(source["accepted_version_id"]),
                }
            )
        key = _candidate_key(text)
        if len(key) < 2 or not resolved:
            continue
        if key in by_key:
            for ref in resolved:
                if ref not in by_key[key]["source_refs"]:
                    by_key[key]["source_refs"].append(ref)
            continue
        by_key[key] = {
            "option_id": f"q_{stable_hash({'text': key})[:12]}",
            "text": text,
            "source_action_item_id": resolved[0]["action_item_id"],
            "source_refs": resolved,
        }
    options = list(by_key.values())[:100]
    if len(options) < 2:
        raise QuestionBallotDraftError(
            "model did not return at least two source-bound candidate questions"
        )
    return options


class BailianQuestionBallotDraftProcessor:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
        timeout_seconds: int = 90,
        transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not self.api_key and transport is None:
            raise QuestionBallotDraftError("DASHSCOPE_API_KEY is not configured")
        self.model = model or os.getenv(
            "BAILIAN_BALLOT_MODEL", DEFAULT_BALLOT_MODEL
        )
        self.endpoint = endpoint or os.getenv(
            "DASHSCOPE_CHAT_COMPLETIONS_URL", DEFAULT_BAILIAN_ENDPOINT
        )
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def _invoke(self, request_payload: dict[str, Any]) -> dict[str, Any]:
        if self.transport:
            return self.transport(request_payload)
        request = urllib.request.Request(
            self.endpoint,
            data=canonical_json(request_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as error:
            raise QuestionBallotDraftError(
                f"Bailian ballot-draft request failed: {error}"
            ) from error

    @staticmethod
    def _content(response: dict[str, Any]) -> str:
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise QuestionBallotDraftError(
                "Bailian ballot-draft response has no message content"
            ) from error
        if not isinstance(content, str) or not content.strip():
            raise QuestionBallotDraftError(
                "Bailian ballot-draft response content is empty"
            )
        return content.strip()

    def process(self, upstream_results: list[dict[str, Any]]) -> dict[str, Any]:
        raw_sources = [
            {
                "source_ref": item["source_ref"],
                "title": item["title"],
                "completion_report": item["completion_report"],
                "normalized_result": item.get("normalized_result"),
                "submitted_content": item.get("submitted_content") or "",
                "attachment_texts": [
                    {
                        "name": attachment.get("name"),
                        "extracted_text": attachment.get("extracted_text") or "",
                    }
                    for attachment in (item.get("attachment_texts") or [])
                ],
            }
            for item in upstream_results
        ]
        sources, context_manifest = _bound_model_sources(raw_sources)
        messages = [
            {
                "role": "system",
                "content": (
                    "你负责把多个已验收问题清单整理成投票草稿。候选问题来自每个来源的"
                    " submitted_content 和 attachment_texts，这些是已经过人工验收的成果正文；"
                    " completion_report 和 normalized_result 只是对成果的描述，不要把它们"
                    "当成候选问题。只抽取或轻微规范化原问题，做语义去重，不新增事实。"
                    "返回 JSON：{\"options\":[{\"text\":\"...\","
                    "\"source_refs\":[\"upstream:...\"]}]}。每项至少一个输入 source_ref。"
                ),
            },
            {"role": "user", "content": canonical_json({"sources": sources})},
        ]
        request_payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        repair_count = 0
        responses: list[dict[str, Any]] = []
        try:
            response = self._invoke(request_payload)
            responses.append(response)
            payload = json.loads(self._content(response))
            options = _normalize_model_payload(payload, upstream_results)
        except (json.JSONDecodeError, QuestionBallotDraftError) as first_error:
            repair_count = 1
            repair_payload = {
                **request_payload,
                "messages": [
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            "上次输出无法通过契约校验。请只返回规定 JSON，确保至少两个候选，"
                            "且每个 source_refs 都逐字来自输入。错误："
                            + str(first_error)
                        ),
                    },
                ],
            }
            try:
                response = self._invoke(repair_payload)
                responses.append(response)
                payload = json.loads(self._content(response))
                options = _normalize_model_payload(payload, upstream_results)
            except (json.JSONDecodeError, QuestionBallotDraftError) as error:
                raise QuestionBallotDraftError(
                    f"Bailian ballot-draft contract remained invalid after one repair: {error}"
                ) from error
        return {
            "options": options,
            "model": self.model,
            "prompt_version": BALLOT_PROMPT_VERSION,
            "repair_count": repair_count,
            "generation_mode": "BAILIAN_SEMANTIC_DEDUP",
            "input_hash": stable_hash({"sources": sources}),
            "context_manifest": context_manifest,
            "usage": _usage_summary(responses),
        }


def build_question_ballot_draft(
    upstream_results: list[dict[str, Any]], *, processing_mode: str
) -> dict[str, Any]:
    deterministic = deterministic_ballot_options(upstream_results)
    if processing_mode == "bailian":
        try:
            return BailianQuestionBallotDraftProcessor().process(upstream_results)
        except QuestionBallotDraftError as error:
            if len(deterministic) >= 2:
                return {
                    "options": deterministic,
                    "model": None,
                    "prompt_version": "question-ballot-rules.v1",
                    "repair_count": 0,
                    "generation_mode": "DETERMINISTIC_FALLBACK",
                    "fallback_reason": str(error)[:500],
                    "attempted_model": DEFAULT_BALLOT_MODEL,
                    "input_hash": stable_hash(
                        {"upstream_results": upstream_results}
                    ),
                    "context_manifest": {
                        "context_manifest_version": "context-manifest.v1",
                        "purpose": "QUESTION_BALLOT_DRAFT",
                        "included_refs": [
                            item.get("source_ref")
                            for item in upstream_results
                        ],
                        "truncated": False,
                        "truncation_strategy": [],
                    },
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                }
            raise
    if len(deterministic) < 2:
        raise QuestionBallotDraftError(
            "accepted upstream results contain fewer than two extractable candidates"
        )
    return {
        "options": deterministic,
        "model": None,
        "prompt_version": "question-ballot-rules.v1",
        "repair_count": 0,
        "generation_mode": "DETERMINISTIC_RULES",
        "input_hash": stable_hash({"upstream_results": upstream_results}),
        "context_manifest": {
            "context_manifest_version": "context-manifest.v1",
            "purpose": "QUESTION_BALLOT_DRAFT",
            "included_refs": [
                item.get("source_ref") for item in upstream_results
            ],
            "truncated": False,
            "truncation_strategy": [],
        },
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
