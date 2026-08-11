"""Extractors to compare against, so a score means something.

The project's claim is that a chunked, schema-validated, quote-aligned chain
beats a single prompt. That claim is only testable if the single prompt is
actually run on the same input, so it lives here as a first-class extractor
rather than as an assumption.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict
from typing import Any

from .extraction import DEFAULT_BAILIAN_ENDPOINT, DEFAULT_BAILIAN_MODEL
from .extraction_evaluation import LabelledMeeting, normalize
from .models import stable_hash

SINGLE_PROMPT_VERSION = "baseline-single-prompt.v1"

SINGLE_PROMPT = (
    "你从会议逐字稿中抽取行动项。只输出 JSON："
    '{"action_items":[{"title":"...","deliverable":"...","owner_name":null,'
    '"deadline_text":null,"deadline_iso":null,"source_quote":"逐字稿原句"}]}。'
    "source_quote 必须逐字来自输入。没有行动项时返回空数组。"
)


def single_prompt_extractor(
    *,
    api_key: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
    timeout_seconds: int = 180,
):
    """One call, one prompt, no chunking, no schema repair, no quote alignment.

    This is the honest baseline: what someone gets by pasting the transcript
    into a model and asking for action items.
    """

    resolved_key = api_key or os.getenv("DASHSCOPE_API_KEY")
    resolved_model = model or os.getenv("BAILIAN_MODEL", DEFAULT_BAILIAN_MODEL)
    resolved_endpoint = endpoint or os.getenv(
        "DASHSCOPE_CHAT_COMPLETIONS_URL", DEFAULT_BAILIAN_ENDPOINT
    )
    if not resolved_key:
        raise ValueError("DASHSCOPE_API_KEY is not configured")

    def extract(meeting: LabelledMeeting) -> list[dict[str, Any]]:
        body = {
            "model": resolved_model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SINGLE_PROMPT},
                {"role": "user", "content": meeting.transcript},
            ],
        }
        request = urllib.request.Request(
            resolved_endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {resolved_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        items = parsed.get("action_items") or parsed.get("items") or []
        # No repair pass on purpose: the baseline's failure modes are part of
        # what is being measured.
        return [item for item in items if isinstance(item, dict)]

    extract.run_signature = f"{SINGLE_PROMPT_VERSION}:{resolved_model}"
    return extract


ACTION_VERBS = (
    "准备", "提交", "整理", "完成", "跟进", "确认", "安排", "负责", "输出",
    "发给", "对接", "汇总", "拉群", "上线", "评审", "落地", "推进", "补充",
)
TIME_HINTS = (
    "今天", "明天", "后天", "本周", "下周", "周一", "周二", "周三", "周四",
    "周五", "周六", "周日", "月底", "月初", "下个月", "点前", "之前", "截止",
)


def keyword_extractor(meeting: LabelledMeeting) -> list[dict[str, Any]]:
    """Zero-model floor: a sentence with an action verb and a time expression.

    The corpus authors used exactly this heuristic to pre-select annotation
    candidates, so it is a meaningful floor rather than a strawman -- anything
    that cannot beat it is not earning its model cost.
    """

    items: list[dict[str, Any]] = []
    for sentence in meeting.sentences:
        has_verb = any(verb in sentence for verb in ACTION_VERBS)
        has_time = any(hint in sentence for hint in TIME_HINTS)
        if has_verb and has_time:
            items.append(
                {
                    "title": sentence[:60],
                    "deliverable": sentence[:60],
                    "owner_name": None,
                    "deadline_text": None,
                    "deadline_iso": None,
                    "source_quote": sentence,
                }
            )
    return items


keyword_extractor.run_signature = "keyword-floor.v1"


def replay_extractor(predictions_by_meeting: dict[str, list[dict[str, Any]]]):
    """Score a stored extraction run without calling any model again.

    Keeps the blind-test set usable repeatedly at zero token cost, and makes a
    reported number reproducible from a committed artefact.
    """

    def extract(meeting: LabelledMeeting) -> list[dict[str, Any]]:
        return list(predictions_by_meeting.get(meeting.meeting_id) or [])

    extract.run_signature = "replay:" + stable_hash(predictions_by_meeting)
    return extract


def rule_recall_extractor(*, output: str = "raw_candidates"):
    """Zero-model diagnostic for discovery recall versus draft routing.

    ``raw_candidates`` measures the wide first-stage net. ``draft_items`` uses
    the same candidates after deterministic evidence expansion and readiness
    routing. Both retain an artifact with workload counts; neither pretends
    that candidate count is sentence-level false positives.
    """

    if output not in {"raw_candidates", "draft_items"}:
        raise ValueError("output must be raw_candidates or draft_items")

    from .recall import (
        EVIDENCE_POLICY_VERSION,
        RULE_POLICY_VERSION,
        WINDOW_POLICY_VERSION,
        assert_emit_coverage,
        build_recall_windows,
        build_transcript_units,
        resolve_evidence,
        rule_recall,
    )

    def extract(meeting: LabelledMeeting) -> list[dict[str, Any]]:
        units = build_transcript_units(meeting.transcript)
        unit_by_id = {unit.unit_id: unit for unit in units}
        windows = build_recall_windows(units)
        coverage = assert_emit_coverage(units, windows)
        candidates = rule_recall(units)
        resolutions = [resolve_evidence(candidate, units) for candidate in candidates]
        selected = (
            resolutions
            if output == "raw_candidates"
            else [resolution for resolution in resolutions if resolution["sufficient"]]
        )
        items: list[dict[str, Any]] = []
        for resolution in selected:
            candidate = resolution["candidate"]
            source = next(
                (
                    unit_by_id[unit_id]
                    for unit_id in candidate.anchor_unit_ids
                    if unit_id in unit_by_id
                ),
                None,
            )
            if source is None:
                continue
            items.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "title": source.text[:60],
                    "deliverable": resolution["text"][:240],
                    "source_quote": source.text,
                    "source_timestamp": source.timestamp,
                }
            )
        draft_ready = sum(
            bool(resolution["sufficient"]) for resolution in resolutions
        )
        weak_signals = sum(
            bool(
                set(resolution["candidate"].quality_flags).intersection(
                    {
                        "WEAK_DECISION_SIGNAL",
                        "ASSIGNMENT_ONLY_SIGNAL",
                        "NEGATED_DECISION_SIGNAL",
                    }
                )
            )
            for resolution in resolutions
        )
        routing_reasons: dict[str, int] = {}
        for resolution in resolutions:
            reason = str(resolution.get("routing_reason") or "UNKNOWN")
            routing_reasons[reason] = routing_reasons.get(reason, 0) + 1
        extract.last_artifact = {
            "pipeline_status": "SUCCEEDED",
            "failures": [],
            "coverage": {
                **coverage,
                "windows_total": len(windows),
                "model_window_success_rate": None,
                "rule_candidates": len(candidates),
                "model_candidates": 0,
                "raw_candidates": len(candidates),
                "sufficient_candidates": draft_ready,
                "draft_ready_candidates": draft_ready,
                "weak_signal_candidates": weak_signals,
                "anchor_unit_references": sum(
                    len(candidate.anchor_unit_ids) for candidate in candidates
                ),
                "support_unit_references": sum(
                    len(candidate.support_unit_ids) for candidate in candidates
                ),
                "evidence_unit_references": sum(
                    len(resolution["evidence_unit_ids"])
                    for resolution in resolutions
                ),
                "evidence_bridge_unit_references": sum(
                    step.get("reason") == "LINKED_EVIDENCE_BRIDGE"
                    for resolution in resolutions
                    for step in resolution.get("expansion_steps") or []
                ),
                "model_support_trimmed_candidates": 0,
                "model_unknown_reference_candidates": 0,
                "model_unit_id_canonicalized_candidates": 0,
                "visible_context_anchor_recovered_candidates": 0,
                "visible_context_duplicate_merged_candidates": 0,
                "routing_reasons": routing_reasons,
                "draft_items": draft_ready,
                "review_hints": len(resolutions) - draft_ready,
            },
        }
        return items

    extract.last_artifact = None
    extract.run_signature = (
        f"rule-recall:{output}:{RULE_POLICY_VERSION}:"
        f"{EVIDENCE_POLICY_VERSION}:{WINDOW_POLICY_VERSION}"
    )
    return extract


def project_chain_extractor(
    *,
    meeting_date_default: str = "",
    checkpoint_dir: str | None = None,
    use_tools: bool = False,
    legacy: bool = False,
    window_policy: Any | None = None,
    output: str = "draft_items",
    structure_candidates: bool = True,
    discovery_workers: int = 1,
):
    """This project's real chain: chunking, schema validation, quote repair.

    `use_tools` switches the same chain onto the tool-calling prompt, so the
    two can be scored side by side on one corpus. Everything downstream of the
    model call is identical, which is what makes the comparison mean anything.
    """

    if output not in {"draft_items", "raw_candidates"}:
        raise ValueError("output must be draft_items or raw_candidates")
    if (legacy or use_tools) and (
        window_policy is not None
        or output != "draft_items"
        or not structure_candidates
    ):
        raise ValueError(
            "window/output/structuring controls apply only to recall-first"
        )

    if legacy or use_tools:
        from .extraction import BailianExtractor

        extractor = BailianExtractor(use_tools=use_tools)
    else:
        from .recall import (
            EVIDENCE_POLICY_VERSION,
            MODEL_CANDIDATE_POLICY_VERSION,
            RULE_POLICY_VERSION,
            RecallFirstExtractor,
        )

        extractor = RecallFirstExtractor(
            window_policy=window_policy,
            structure_candidates=structure_candidates,
            discovery_workers=discovery_workers,
        )

    def extract(meeting: LabelledMeeting) -> list[dict[str, Any]]:
        result = extractor.extract(
            meeting.transcript,
            meeting_date=meeting.meeting_date or meeting_date_default or None,
            checkpoint_dir=checkpoint_dir,
        )
        if output == "raw_candidates":
            units = {
                str(unit.get("unit_id")): unit
                for unit in result.get("units") or []
                if isinstance(unit, dict) and unit.get("unit_id")
            }
            items = []
            for candidate in result.get("raw_candidates") or []:
                if not isinstance(candidate, dict):
                    continue
                for unit_id in dict.fromkeys(
                    str(value)
                    for value in candidate.get("anchor_unit_ids") or []
                ):
                    source = units.get(unit_id)
                    if source is None:
                        continue
                    items.append(
                        {
                            "candidate_id": candidate.get("candidate_id"),
                            "title": str(source.get("text") or "")[:60],
                            "deliverable": str(source.get("text") or "")[:240],
                            "source_quote": str(source.get("text") or ""),
                            "source_timestamp": source.get("timestamp"),
                        }
                    )
        else:
            items = result.get("action_items") or []
        extract.last_artifact = result
        return [
            item if isinstance(item, dict) else json.loads(json.dumps(item))
            for item in items
        ]

    extract.last_artifact = None
    window_signature = (
        asdict(extractor.window_policy)
        if hasattr(extractor, "window_policy")
        else None
    )
    if window_signature is not None:
        extract.run_signature = (
            f"{extractor.prompt_version}:{MODEL_CANDIDATE_POLICY_VERSION}:"
            f"{RULE_POLICY_VERSION}:{EVIDENCE_POLICY_VERSION}:{output}:"
            f"{structure_candidates}:{extractor.model_support_character_budget}:"
            f"{stable_hash(window_signature)}"
        )
    else:
        extract.run_signature = (
            f"{extractor.prompt_version}:{output}:{use_tools}:{legacy}"
        )
    return extract
