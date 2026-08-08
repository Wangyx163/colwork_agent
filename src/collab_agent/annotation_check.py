"""Validate a hand-annotated meeting file before it is used as ground truth.

Annotation is expensive and its failure mode is silent: a quote that was
retyped instead of copied still looks fine to a human, but the scorer cannot
locate it, so the item quietly scores as a miss and drags the reported number
down for a reason that has nothing to do with the extractor.

Every check here answers one question: would this annotation still mean what
the annotator intended once the evaluation harness reads it?
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .extraction import ITEM_TYPES
from .extraction_evaluation import normalize, split_sentences
from .models import read_text_file

TIMESTAMP_IN_TRANSCRIPT = r"\d{2}:\d{2}:\d{2}"

REQUIRED_CASE_FIELDS = (
    "case_id",
    "meeting_date",
    "participants",
    "transcript",
    "expected",
)
REQUIRED_ITEM_FIELDS = (
    "title",
    "item_type",
    "deliverable",
    "owner_name",
    "deadline_text",
    "deadline_iso",
    "source_timestamp",
    "source_quote",
    "collaborator_names",
)


class AnnotationProblem(dict):
    """One problem, carrying enough location to fix it without searching."""

    def __init__(self, *, case: str, item: int | None, level: str, message: str):
        super().__init__(case=case, item=item, level=level, message=message)


def _timestamps_in(transcript: str) -> set[str]:
    import re

    return set(re.findall(TIMESTAMP_IN_TRANSCRIPT, transcript))


def check_case(case: dict[str, Any]) -> list[AnnotationProblem]:
    case_id = str(case.get("case_id") or "<no case_id>")
    problems: list[AnnotationProblem] = []

    def problem(message: str, *, item: int | None = None, level: str = "ERROR") -> None:
        problems.append(
            AnnotationProblem(case=case_id, item=item, level=level, message=message)
        )

    for field in REQUIRED_CASE_FIELDS:
        if field not in case:
            problem(f"缺少字段 {field}")
        elif case[field] in (None, ""):
            # Present-but-null is as unusable as absent, and it is the easier
            # mistake to make when a template is filled in by hand.
            problem(f"{field} 为空；字段存在但没有值，等同于缺失")
    transcript = str(case.get("transcript") or "")
    if not transcript.strip():
        problem("transcript 为空，无法定位任何引文")
        return problems

    participants = [str(name).strip() for name in case.get("participants") or []]
    if not participants:
        problem("participants 为空；参会名单是权限边界，必须显式给出")
    if "blind" not in case:
        problem(
            "未声明 blind；最终报数只使用 blind=true 的会议，缺这个字段无法判断",
            level="WARNING",
        )

    sentences = split_sentences(transcript)
    normalized = [normalize(sentence) for sentence in sentences]
    timestamps = _timestamps_in(transcript)
    # A second annotation landing on a sentence that is already taken usually
    # means one quote was pasted twice, or two items were split off one line
    # without narrowing their quotes.
    claimed: dict[int, int] = {}

    expected = case.get("expected") or []
    if not isinstance(expected, list):
        problem("expected 必须是数组")
        return problems
    if not expected:
        problem(
            "这场会议没有任何标注条目；如果确实没有行动项，这是有效的负样本，"
            "请在 annotator_note 里写明以示区分",
            level="WARNING",
        )

    for index, item in enumerate(expected):
        if not isinstance(item, dict):
            problem("条目必须是对象", item=index)
            continue
        for field in REQUIRED_ITEM_FIELDS:
            if field not in item:
                problem(f"缺少字段 {field}", item=index)

        item_type = str(item.get("item_type") or "").strip().upper()
        if item_type not in ITEM_TYPES:
            problem(
                f"item_type={item.get('item_type')!r} 不合法，只能是 {list(ITEM_TYPES)}",
                item=index,
            )

        timestamp = str(item.get("source_timestamp") or "").strip()
        if timestamp and timestamp not in timestamps:
            problem(
                f"source_timestamp {timestamp} 在逐字稿里不存在；"
                "时间戳必须逐字取自原文",
                item=index,
            )

        quote = str(item.get("source_quote") or "").strip()
        if not quote:
            problem("source_quote 为空", item=index)
            continue
        normalized_quote = normalize(quote)
        if len(normalized_quote) < 4:
            problem(
                f"source_quote 太短（{quote!r}）；不足 4 个有效字符无法可靠定位",
                item=index,
            )
            continue
        # Mirrors the harness matcher: an item cites one sentence, and only its
        # best match is credited.
        best_index: int | None = None
        best_length = 0
        for position, sentence in enumerate(normalized):
            if not sentence:
                continue
            if sentence == normalized_quote:
                best_index, best_length = position, len(sentence)
                break
            if sentence in normalized_quote or normalized_quote in sentence:
                overlap = min(len(sentence), len(normalized_quote))
                if overlap > best_length:
                    best_index, best_length = position, overlap
        if best_index is None:
            problem(
                f"source_quote 无法在逐字稿中定位：{quote[:60]!r}。"
                "请直接从逐字稿复制粘贴，不要改写或拼接",
                item=index,
            )
            continue
        if best_index in claimed:
            # A sentence really can carry two actions («你做 A，他做 B»), and the
            # guide allows sharing the quote. The warning stays because the same
            # shape is also what a duplicated paste looks like, and only the
            # annotator can tell the two apart -- but it must not read as a rule
            # violation, and it must say what the consequence is.
            problem(
                f"与条目 {claimed[best_index]} 引用同一句：{sentences[best_index][:50]!r}。"
                "若确实是一句里的两件事，这是允许的，条目级评测会分别计分；"
                "但句级评测按句计数，这两条只算一句金标。"
                "若是误粘贴，请修正",
                item=index,
                level="WARNING",
            )
        else:
            claimed[best_index] = index

        owner = item.get("owner_name")
        if owner not in (None, "") and participants:
            if str(owner).strip() not in participants:
                problem(
                    f"owner_name={owner!r} 不在 participants 里；"
                    "系统不接受名单外的负责人",
                    item=index,
                )

        collaborators = item.get("collaborator_names")
        if collaborators is None:
            collaborators = []
        if not isinstance(collaborators, list):
            problem("collaborator_names 必须是数组", item=index)
        else:
            for name in collaborators:
                if participants and str(name).strip() not in participants:
                    problem(
                        f"collaborator_names 里的 {name!r} 不在 participants 里",
                        item=index,
                    )
                if owner not in (None, "") and str(name).strip() == str(owner).strip():
                    problem(
                        f"{name!r} 同时是 owner 和协作者；一个人只占一个角色",
                        item=index,
                    )
            if collaborators and owner in (None, ""):
                problem(
                    "有协作者但没有 owner；若原文只说了几个人一起做而没有主责，"
                    "把其中原文最先/最明确被指派的人填为 owner，其余填协作者",
                    item=index,
                    level="WARNING",
                )

        deadline_iso = item.get("deadline_iso")
        if deadline_iso not in (None, ""):
            from .models import parse_time

            try:
                parse_time(str(deadline_iso))
            except Exception:
                problem(
                    f"deadline_iso={deadline_iso!r} 不是合法 ISO-8601", item=index
                )

    return problems


def check_annotation_file(path: str | Path) -> dict[str, Any]:
    payload = json.loads(read_text_file(path))
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        return {
            "path": str(path),
            "valid": False,
            "problems": [
                AnnotationProblem(
                    case="<file>",
                    item=None,
                    level="ERROR",
                    message="cases 缺失或为空",
                )
            ],
            "summary": {},
        }

    problems: list[AnnotationProblem] = []
    summary = {
        "cases": len(cases),
        "blind_cases": 0,
        "annotated_items": 0,
        "task_items": 0,
        "commitment_items": 0,
        "items_with_owner": 0,
        "items_with_deadline": 0,
        "items_with_collaborators": 0,
        "sentences": 0,
    }
    for case in cases:
        problems.extend(check_case(case))
        if case.get("blind"):
            summary["blind_cases"] += 1
        summary["sentences"] += len(split_sentences(str(case.get("transcript") or "")))
        for item in case.get("expected") or []:
            if not isinstance(item, dict):
                continue
            summary["annotated_items"] += 1
            if str(item.get("item_type") or "").upper() == "COMMITMENT":
                summary["commitment_items"] += 1
            else:
                summary["task_items"] += 1
            if item.get("owner_name") not in (None, ""):
                summary["items_with_owner"] += 1
            if item.get("deadline_iso") not in (None, ""):
                summary["items_with_deadline"] += 1
            if item.get("collaborator_names"):
                summary["items_with_collaborators"] += 1
    if summary["sentences"]:
        summary["positive_sentence_rate"] = round(
            summary["annotated_items"] / summary["sentences"], 4
        )
    errors = [item for item in problems if item["level"] == "ERROR"]
    return {
        "path": str(path),
        "valid": not errors,
        "error_count": len(errors),
        "warning_count": len(problems) - len(errors),
        "problems": problems,
        "summary": summary,
    }
