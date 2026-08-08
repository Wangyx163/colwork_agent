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
from typing import Any

from .extraction import DEFAULT_BAILIAN_ENDPOINT, DEFAULT_BAILIAN_MODEL
from .extraction_evaluation import LabelledMeeting, normalize

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


def replay_extractor(predictions_by_meeting: dict[str, list[dict[str, Any]]]):
    """Score a stored extraction run without calling any model again.

    Keeps the blind-test set usable repeatedly at zero token cost, and makes a
    reported number reproducible from a committed artefact.
    """

    def extract(meeting: LabelledMeeting) -> list[dict[str, Any]]:
        return list(predictions_by_meeting.get(meeting.meeting_id) or [])

    return extract


def project_chain_extractor(
    *,
    meeting_date_default: str = "",
    checkpoint_dir: str | None = None,
    use_tools: bool = False,
):
    """This project's real chain: chunking, schema validation, quote repair.

    `use_tools` switches the same chain onto the tool-calling prompt, so the
    two can be scored side by side on one corpus. Everything downstream of the
    model call is identical, which is what makes the comparison mean anything.
    """

    from .extraction import BailianExtractor

    extractor = BailianExtractor(use_tools=use_tools)

    def extract(meeting: LabelledMeeting) -> list[dict[str, Any]]:
        result = extractor.extract(
            meeting.transcript,
            meeting_date=meeting.meeting_date or meeting_date_default or None,
            checkpoint_dir=checkpoint_dir,
        )
        items = result.get("action_items") or []
        return [
            item if isinstance(item, dict) else json.loads(json.dumps(item))
            for item in items
        ]

    return extract
