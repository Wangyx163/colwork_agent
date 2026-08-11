"""Recall-first meeting extraction.

The legacy extractor asks a model to discover, structure and validate an
action item in one pass.  That is convenient, but every strict requirement is
also a place where a weak candidate can disappear.  This module separates the
internal responsibilities while keeping one product-level result:

* stable transcript units and emit-owned sliding windows;
* one broad model recall pass, unioned with a deterministic rule net;
* transcript-global, bounded evidence expansion;
* per-candidate structuring and validation;
* unresolved evidence retained as ``review_hints`` rather than discarded.

``action_items`` is retained as a compatibility alias of ``draft_items``.
Review hints are deliberately not action-item domain entities.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .extraction import (
    ACTION_ITEM_EXTRACTION_PROMPT_VERSION,
    BailianExtractor,
    DEFAULT_BAILIAN_MODEL,
    ExtractionError,
    normalize_extraction_payload,
    validate_extraction,
)
from .models import stable_hash


RECALL_PROMPT_VERSION = "meeting-recall-first.v2.3"
WINDOW_POLICY_VERSION = "emit-owned-char-window.v2"
RULE_POLICY_VERSION = "meeting-action-lexicon.zh-en.v3"
EVIDENCE_POLICY_VERSION = "adaptive-global-evidence.v3"
MODEL_CANDIDATE_POLICY_VERSION = "model-candidate-normalization.v4"
DEFAULT_MODEL_SUPPORT_CHARACTERS = 600

_LINE = re.compile(
    r"^(?P<speaker>.+?)[(（](?P<timestamp>\d{1,2}:\d{2}:\d{2})[)）]\s*[:：]\s*(?P<text>.*)$"
)
_SENTENCE = re.compile(r".*?(?:[。！？!?；;]|$)", re.S)
_SPACE = re.compile(r"\s+")

# This net is intentionally broad.  Its job is to keep plausible evidence in
# the pipeline, not to decide whether a coordinator should dispatch a task.
_ACTION = re.compile(
    r"(?:负责|跟进|推进|完成|提交|整理|输出|发送|发给|提供|确认|对接|联系|"
    r"更新|修改|补充|准备|安排|处理|检查|排查|调研|评估|制定|搭建|上线|"
    r"测试|汇总|邀请|通知|交付|支持|协助|配合|复盘|落实|开发|设计|撰写|"
    r"写|做|看一下|看下|回复|同步|给出|产出|action|follow[ -]?up|send|"
    r"submit|prepare|update|review|check|investigate|deliver|draft|build)",
    re.I,
)
_ASSIGNMENT = re.compile(
    r"(?:我来|我会|我去|我负责|交给|由.+?负责|你来|你们来|请|麻烦|需要|"
    r"要不|记得|务必|别忘了|let me|I(?:'ll| will)|please|need to)",
    re.I,
)
_STRONG_DECISION = re.compile(
    r"(?:决定|确定|定了|上线时间|we(?:'ll| will)|decided)",
    re.I,
)
# These words are useful discovery signals, but are highly polysemous in
# spoken meetings. They stay in the recall net and require corroboration before
# a candidate may become a draft item.
_WEAK_DECISION = re.compile(
    r"(?:就按|采用|选择|同意|通过|不再|改成|agreed|go with)",
    re.I,
)
_DECISION = re.compile(
    rf"(?:{_STRONG_DECISION.pattern}|{_WEAK_DECISION.pattern})",
    re.I,
)
_NEGATED_DECISION = re.compile(
    r"(?:不|不能|无法|尚未|还没|没有|未|难以)\s*(?:决定|确定)|(?:决定|确定)不了",
    re.I,
)
_TIME = re.compile(
    r"(?:今天|明天|后天|本周|这周|下周|周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"月底|月初|年底|尽快|稍后|会后|之前|以前|截止|到\s*\d|\d{1,2}\s*[月日号点]|"
    r"\d{1,2}:\d{2}|today|tomorrow|this week|next week|by\s+\w+|deadline)",
    re.I,
)
# Product recall extensions found in the fixed 15-meeting audit. They are
# intentionally generic operational language, not AMC-A sentence memorisation.
_ACTION = re.compile(
    rf"(?:{_ACTION.pattern}|(?:招聘|招人|招募|开设|集合|报备))",
    re.I,
)
_TIME = re.compile(
    rf"(?:{_TIME.pattern}|(?:早晨|上午|中午|下午|晚上|第二天|每天|每周|每月|定期|"
    r"一周一次|隔(?:一)?周|提前\s*[一二三四五六七八九十\d]+\s*天))",
    re.I,
)
_ACCEPTANCE = re.compile(
    r"(?:可以|好的|好|行|没问题|收到|同意|确认|就这样|按这个|ok(?:ay)?|sure|agreed)",
    re.I,
)
_SHORT_CONFIRM = re.compile(
    r"^(?:好(?:的)?|行|可以|没问题|收到|明白|知道了|对|嗯|ok(?:ay)?|sure|yes)[。！!\s]*$",
    re.I,
)
_PRONOUN = re.compile(r"(?:这个|那个|这件事|那件事|它|这块|这边|上述|前面|其|这项)")
_TOPIC = re.compile(r"(?:下一个议题|下个话题|换个话题|接下来讨论|next topic)", re.I)
_CONDITION = re.compile(r"(?:如果|假如|只要|等到|取决于|前提是|provided|if\s+|when\s+)", re.I)
_QUESTION_OR_HYPOTHETICAL = re.compile(
    r"(?:[？?]|是否|要不要|能不能|可不可以|是不是|怎么(?:办|做|看)?|为什么|"
    r"谁来|哪(?:个|些)|假如|假设|可能会|考虑(?:一下)?|倾向于)",
    re.I,
)
_CONTENT_NOISE = re.compile(
    r"(?:我|我们|你|你们|他|他们|她|她们|这个|那个|这件事|那件事|这块|这边|"
    r"一下|一些|一个|目前|现在|然后|就是|其实|可能|应该|觉得|认为|先|再|"
    r"继续|进行|相关|方面|事情|东西|什么|怎么样|吧|呢|吗|啊|呀|的|了)",
    re.I,
)

SHORT_CONFIRM_LOOKBACK_CHARACTERS = 600


@dataclass(frozen=True)
class TranscriptUnit:
    unit_id: str
    index: int
    line_index: int
    part_index: int
    speaker: str | None
    timestamp: str
    text: str

    @property
    def rendered(self) -> str:
        speaker = self.speaker or "UNKNOWN"
        return f"{self.unit_id}\t{speaker}\t{self.timestamp}\t{self.text}"


@dataclass(frozen=True)
class RecallWindow:
    window_id: str
    left_context_ids: tuple[str, ...]
    emit_unit_ids: tuple[str, ...]
    right_context_ids: tuple[str, ...]

    @property
    def all_unit_ids(self) -> tuple[str, ...]:
        return self.left_context_ids + self.emit_unit_ids + self.right_context_ids


@dataclass
class RawCandidate:
    candidate_id: str
    anchor_unit_ids: list[str]
    support_unit_ids: list[str] = field(default_factory=list)
    kind_hints: list[str] = field(default_factory=list)
    reason_code: str = "PLAUSIBLE_ACTION"
    trigger_sources: list[str] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WindowPolicy:
    total_characters: int = 3200
    left_characters: int = 800
    emit_characters: int = 1600
    right_characters: int = 800
    max_unit_characters: int = 240

    def __post_init__(self) -> None:
        if min(
            self.total_characters,
            self.left_characters,
            self.emit_characters,
            self.right_characters,
            self.max_unit_characters,
        ) <= 0:
            raise ValueError("window character budgets must be positive")
        if self.left_characters + self.emit_characters + self.right_characters > self.total_characters:
            raise ValueError("window sub-budgets exceed total_characters")


def _timestamp_for_line(line_index: int) -> str:
    hours, remainder = divmod(line_index, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _split_long_piece(text: str, maximum: int) -> list[str]:
    text = text.strip()
    if len(text) <= maximum:
        return [text] if text else []
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + maximum)
        if end < len(text):
            boundary = max(
                text.rfind("，", start, end),
                text.rfind(",", start, end),
                text.rfind("、", start, end),
                text.rfind(" ", start, end),
            )
            if boundary > start + maximum // 2:
                end = boundary + 1
        pieces.append(text[start:end].strip())
        start = end
    return [piece for piece in pieces if piece]


def build_transcript_units(
    transcript: str, *, max_unit_characters: int = 240
) -> list[TranscriptUnit]:
    """Create stable, sentence-like units without losing source-line identity."""

    units: list[TranscriptUnit] = []
    for line_index, raw_line in enumerate(str(transcript or "").splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        match = _LINE.match(line)
        if match:
            speaker = match.group("speaker").strip() or None
            timestamp = match.group("timestamp")
            if len(timestamp.split(":")[0]) == 1:
                timestamp = "0" + timestamp
            text = match.group("text").strip()
        else:
            speaker = None
            timestamp = _timestamp_for_line(line_index)
            text = line
        sentence_parts = [
            piece.strip()
            for piece in _SENTENCE.findall(text)
            if piece and piece.strip()
        ]
        if not sentence_parts and text:
            sentence_parts = [text]
        part_index = 0
        for sentence in sentence_parts:
            for piece in _split_long_piece(sentence, max_unit_characters):
                unit_id = f"u{line_index:06d}.{part_index:03d}"
                units.append(
                    TranscriptUnit(
                        unit_id=unit_id,
                        index=len(units),
                        line_index=line_index,
                        part_index=part_index,
                        speaker=speaker,
                        timestamp=timestamp,
                        text=piece,
                    )
                )
                part_index += 1
    return units


def _take_by_budget(
    units: Sequence[TranscriptUnit], indices: Iterable[int], budget: int
) -> list[TranscriptUnit]:
    selected: list[TranscriptUnit] = []
    used = 0
    for index in indices:
        unit = units[index]
        cost = len(unit.rendered) + 1
        if selected and used + cost > budget:
            break
        selected.append(unit)
        used += cost
        if used >= budget:
            break
    return selected


def build_recall_windows(
    units: Sequence[TranscriptUnit], policy: WindowPolicy | None = None
) -> list[RecallWindow]:
    """Build windows where every unit belongs to exactly one emit zone."""

    policy = policy or WindowPolicy()
    windows: list[RecallWindow] = []
    emit_start = 0
    while emit_start < len(units):
        emit_units = _take_by_budget(
            units, range(emit_start, len(units)), policy.emit_characters
        )
        if not emit_units:
            emit_units = [units[emit_start]]
        emit_end = emit_start + len(emit_units)
        left = list(
            reversed(
                _take_by_budget(
                    units,
                    range(emit_start - 1, -1, -1),
                    policy.left_characters,
                )
            )
        )
        right = _take_by_budget(
            units, range(emit_end, len(units)), policy.right_characters
        )
        window_id = "w_" + stable_hash(
            [
                WINDOW_POLICY_VERSION,
                [unit.rendered for unit in left],
                [unit.rendered for unit in emit_units],
                [unit.rendered for unit in right],
            ]
        )[:16]
        windows.append(
            RecallWindow(
                window_id=window_id,
                left_context_ids=tuple(unit.unit_id for unit in left),
                emit_unit_ids=tuple(unit.unit_id for unit in emit_units),
                right_context_ids=tuple(unit.unit_id for unit in right),
            )
        )
        emit_start = emit_end
    return windows


def assert_emit_coverage(
    units: Sequence[TranscriptUnit], windows: Sequence[RecallWindow]
) -> dict[str, Any]:
    expected = [unit.unit_id for unit in units]
    emitted = [unit_id for window in windows for unit_id in window.emit_unit_ids]
    counts = {unit_id: emitted.count(unit_id) for unit_id in expected}
    missing = [unit_id for unit_id, count in counts.items() if count == 0]
    duplicate = [unit_id for unit_id, count in counts.items() if count > 1]
    if missing or duplicate or emitted != expected:
        raise ExtractionError(
            f"invalid emit coverage: missing={missing}, duplicate={duplicate}",
            error_code="INVALID_WINDOW_COVERAGE",
            stage="UNIT_WINDOWING",
        )
    return {
        "units_total": len(expected),
        "units_emitted_once": len(emitted),
        "missing_emit_units": [],
        "duplicate_emit_units": [],
        "coverage_rate": 1.0 if expected else 1.0,
    }


def _candidate_id(
    anchor_ids: Sequence[str], kind_hints: Sequence[str], reason: str
) -> str:
    return "rc_" + stable_hash(
        [list(anchor_ids), sorted(kind_hints), _SPACE.sub(" ", reason).strip()]
    )[:20]


def _decision_signal_strength(text: str) -> str | None:
    strong = bool(_STRONG_DECISION.search(text))
    weak = bool(_WEAK_DECISION.search(text))
    if strong and not _NEGATED_DECISION.search(text):
        return "STRONG"
    if strong or weak:
        return "WEAK"
    return None


def _find_confirmed_unit(
    units: Sequence[TranscriptUnit],
    confirmation_index: int,
    *,
    character_budget: int = SHORT_CONFIRM_LOOKBACK_CHARACTERS,
) -> TranscriptUnit | None:
    """Find a confirmation's referent inside the nearest substantive turn.

    Unit counts are unstable because one speaker turn may split into many
    sentence units. We therefore skip a run of acknowledgements, then inspect
    only the immediately preceding source line up to a character budget. This
    captures multi-sentence proposals without attaching a generic "嗯" to an
    action several conversational turns away.
    """

    index = confirmation_index - 1
    used = 0
    while index >= 0 and _SHORT_CONFIRM.fullmatch(units[index].text.strip()):
        used += len(units[index].rendered) + 1
        if used > character_budget:
            return None
        index -= 1
    if index < 0:
        return None
    source_line = units[index].line_index
    while index >= 0 and units[index].line_index == source_line:
        unit = units[index]
        cost = len(unit.rendered) + 1
        if used + cost > character_budget or _TOPIC.search(unit.text):
            break
        used += cost
        if _ACTION.search(unit.text) or _DECISION.search(unit.text):
            return unit
        index -= 1
    return None


def _classify_candidate_anchor(
    candidate: RawCandidate, unit_by_id: dict[str, TranscriptUnit]
) -> None:
    anchor_text = " ".join(
        unit_by_id[unit_id].text
        for unit_id in candidate.anchor_unit_ids
        if unit_id in unit_by_id
    )
    strength = _decision_signal_strength(anchor_text)
    if strength == "WEAK":
        candidate.quality_flags = list(
            dict.fromkeys(candidate.quality_flags + ["WEAK_DECISION_SIGNAL"])
        )
    if _NEGATED_DECISION.search(anchor_text):
        candidate.quality_flags = list(
            dict.fromkeys(candidate.quality_flags + ["NEGATED_DECISION_SIGNAL"])
        )
    if (
        _ASSIGNMENT.search(anchor_text)
        and not _ACTION.search(anchor_text)
        and not _DECISION.search(anchor_text)
    ):
        candidate.quality_flags = list(
            dict.fromkeys(candidate.quality_flags + ["ASSIGNMENT_ONLY_SIGNAL"])
        )


def _normalize_candidate_context(
    candidate: RawCandidate, units: Sequence[TranscriptUnit]
) -> RawCandidate:
    """Make confirmations supporting evidence rather than task-card anchors."""

    unit_by_id = {unit.unit_id: unit for unit in units}
    confirmation_ids = [
        unit_id
        for unit_id in candidate.anchor_unit_ids
        if unit_id in unit_by_id
        and _SHORT_CONFIRM.fullmatch(unit_by_id[unit_id].text.strip())
    ]
    if confirmation_ids:
        substantive = [
            unit_id
            for unit_id in candidate.anchor_unit_ids
            if unit_id not in confirmation_ids
        ]
        if not substantive:
            first_confirmation = min(
                (unit_by_id[unit_id] for unit_id in confirmation_ids),
                key=lambda unit: unit.index,
            )
            referent = _find_confirmed_unit(units, first_confirmation.index)
            if referent is not None:
                substantive = [referent.unit_id]
        if substantive:
            candidate.anchor_unit_ids = list(dict.fromkeys(substantive))
            candidate.support_unit_ids = list(
                dict.fromkeys(candidate.support_unit_ids + confirmation_ids)
            )
            candidate.quality_flags = list(
                dict.fromkeys(
                    candidate.quality_flags + ["SHORT_CONFIRMATION_SUPPORT"]
                )
            )
            candidate.candidate_id = _candidate_id(
                candidate.anchor_unit_ids,
                candidate.kind_hints,
                candidate.reason_code,
            )
        else:
            candidate.quality_flags = list(
                dict.fromkeys(
                    candidate.quality_flags
                    + ["SHORT_CONFIRMATION_WITHOUT_REFERENT"]
                )
            )
    _classify_candidate_anchor(candidate, unit_by_id)
    return candidate


def rule_recall(units: Sequence[TranscriptUnit]) -> list[RawCandidate]:
    candidates: list[RawCandidate] = []
    for unit in units:
        text = unit.text
        action = bool(_ACTION.search(text))
        assigned = bool(_ASSIGNMENT.search(text))
        decision = bool(_DECISION.search(text))
        temporal = bool(_TIME.search(text))
        short_confirmation = bool(_SHORT_CONFIRM.fullmatch(text.strip()))
        # A bare acknowledgement is evidence about an earlier utterance. It
        # must never create a card anchored on "嗯/好/行" itself.
        if short_confirmation:
            continue
        if not (
            decision
            or (action and (assigned or temporal))
            or (assigned and len(text) >= 8)
        ):
            continue
        kinds = ["DECISION"] if decision else ["TASK"]
        if not decision and temporal and _ACCEPTANCE.search(text):
            kinds.append("COMMITMENT")
        reason = (
            "WEAK_DECISION_SIGNAL"
            if _decision_signal_strength(text) == "WEAK"
            else "DECISION_SIGNAL"
            if decision
            else "ACTION_ASSIGNMENT_OR_TIME_SIGNAL"
        )
        candidate = RawCandidate(
                candidate_id=_candidate_id([unit.unit_id], kinds, reason),
                anchor_unit_ids=[unit.unit_id],
                kind_hints=kinds,
                reason_code=reason,
                trigger_sources=["RULE"],
            )
        candidates.append(_normalize_candidate_context(candidate, units))

    # Attach each short confirmation to the nearest earlier action/decision.
    # If that referent was too weak to enter the ordinary rule net, preserve it
    # as a new candidate anchored on the action-bearing sentence, not the reply.
    for confirmation in units:
        if not _SHORT_CONFIRM.fullmatch(confirmation.text.strip()):
            continue
        referent = _find_confirmed_unit(units, confirmation.index)
        if referent is None:
            continue
        matches = [
            candidate
            for candidate in candidates
            if referent.unit_id in candidate.anchor_unit_ids
        ]
        if matches:
            for candidate in matches:
                candidate.support_unit_ids = list(
                    dict.fromkeys(
                        candidate.support_unit_ids + [confirmation.unit_id]
                    )
                )
                candidate.quality_flags = list(
                    dict.fromkeys(
                        candidate.quality_flags + ["SHORT_CONFIRMATION_SUPPORT"]
                    )
                )
            continue
        decision = bool(_DECISION.search(referent.text))
        kinds = ["DECISION"] if decision else ["TASK"]
        reason = "CONFIRMED_DECISION_SIGNAL" if decision else "CONFIRMED_ACTION_SIGNAL"
        candidate = RawCandidate(
            candidate_id=_candidate_id([referent.unit_id], kinds, reason),
            anchor_unit_ids=[referent.unit_id],
            support_unit_ids=[confirmation.unit_id],
            kind_hints=kinds,
            reason_code=reason,
            trigger_sources=["RULE"],
            quality_flags=["SHORT_CONFIRMATION_SUPPORT"],
        )
        candidates.append(_normalize_candidate_context(candidate, units))
    return candidates


def _parse_json_object(value: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ExtractionError(
            "recall model returned invalid JSON",
            error_code="INVALID_JSON",
            stage="RECALL_MODEL_RESPONSE",
            retryable=True,
        ) from error
    if not isinstance(parsed, dict):
        raise ExtractionError("recall model response must be a JSON object")
    return parsed


def _window_prompt(
    window: RecallWindow, unit_by_id: dict[str, TranscriptUnit]
) -> list[dict[str, str]]:
    def render(ids: Sequence[str]) -> str:
        return "\n".join(unit_by_id[unit_id].rendered for unit_id in ids) or "(none)"

    system = """You discover possible meeting follow-ups with recall as the priority.
Return JSON only: {"candidates":[{"anchor_unit_ids":["u..."],"support_unit_ids":["u..."],"kind_hints":["TASK","DECISION"],"reason_code":"..."}]}.
Each kind_hints value must be exactly TASK, COMMITMENT, or DECISION; include one or more applicable values and never return a pipe-delimited enum string.
Use only unit IDs supplied below. You may read all three zones, but every candidate must have at least one anchor in EMIT_ZONE. Context IDs may only be support.
Anchor only the minimal EMIT units that state the possible task, commitment, or decision. support_unit_ids is only for a directly linked confirmation or coreference needed to interpret the anchor; do not copy a topical span, restatements, rationale, or general context into support. A later deterministic stage expands evidence globally. Emit separate candidates for distinct possible follow-ups.
Keep uncertain possibilities; do not structure fields, quote text, merge distinct candidates, or reject for missing owner/deadline."""
    user = (
        "LEFT_CONTEXT\n"
        + render(window.left_context_ids)
        + "\n\nEMIT_ZONE\n"
        + render(window.emit_unit_ids)
        + "\n\nRIGHT_CONTEXT\n"
        + render(window.right_context_ids)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _model_candidates(
    payload: dict[str, Any],
    *,
    window: RecallWindow,
    unit_by_id: dict[str, TranscriptUnit],
    support_character_budget: int = DEFAULT_MODEL_SUPPORT_CHARACTERS,
) -> tuple[list[RawCandidate], list[dict[str, Any]]]:
    raw = payload.get("candidates")
    failures: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return [], [
            {
                "stage": "MODEL_DISCOVERY_VALIDATION",
                "window_id": window.window_id,
                "error": "candidates must be an array",
            }
        ]
    emit_ids = set(window.emit_unit_ids)
    visible_ids = set(window.all_unit_ids)
    candidates: list[RawCandidate] = []

    def canonical_unit_id(value: Any) -> tuple[str, bool]:
        raw_value = str(value)
        if raw_value in unit_by_id:
            return raw_value, False
        match = re.fullmatch(r"u(\d+)\.(\d+)", raw_value, re.I)
        if match:
            normalized = f"u{int(match.group(1)):06d}.{int(match.group(2)):03d}"
            if normalized in unit_by_id:
                return normalized, True
        return raw_value, False

    def bounded_support(
        anchors: Sequence[str], support: Sequence[str]
    ) -> tuple[list[str], bool]:
        anchor_indices = [unit_by_id[value].index for value in anchors]
        ranked = sorted(
            enumerate(support),
            key=lambda pair: (
                min(
                    abs(unit_by_id[pair[1]].index - anchor_index)
                    for anchor_index in anchor_indices
                ),
                pair[0],
            ),
        )
        selected: set[str] = set()
        used = 0
        for _, unit_id in ranked:
            cost = len(unit_by_id[unit_id].rendered) + 1
            if used + cost > support_character_budget:
                continue
            selected.add(unit_id)
            used += cost
        return [value for value in support if value in selected], len(selected) < len(
            support
        )

    for index, item in enumerate(raw):
        try:
            if not isinstance(item, dict):
                raise ValueError("candidate must be an object")
            quality_flags: list[str] = []
            normalized_anchors = [
                canonical_unit_id(value)
                for value in item.get("anchor_unit_ids") or []
            ]
            raw_anchors = list(
                dict.fromkeys(value for value, _ in normalized_anchors)
            )
            if any(changed for _, changed in normalized_anchors):
                quality_flags.append("MODEL_UNIT_ID_CANONICALIZED")
            anchors = [
                value
                for value in raw_anchors
                if value in visible_ids and value in unit_by_id
            ]
            if len(anchors) < len(raw_anchors):
                quality_flags.append("UNKNOWN_MODEL_ANCHOR_DROPPED")
            if not anchors:
                raise ValueError("candidate has no valid visible anchor")
            if not set(anchors).intersection(emit_ids):
                quality_flags.append("VISIBLE_CONTEXT_ANCHOR_RECOVERED")
            normalized_support = [
                canonical_unit_id(value)
                for value in item.get("support_unit_ids") or []
            ]
            raw_support = list(
                dict.fromkeys(value for value, _ in normalized_support)
            )
            if any(changed for _, changed in normalized_support):
                quality_flags = list(
                    dict.fromkeys(
                        quality_flags + ["MODEL_UNIT_ID_CANONICALIZED"]
                    )
                )
            support = [
                value
                for value in raw_support
                if value in visible_ids
                and value in unit_by_id
                and value not in anchors
            ]
            if len(support) < len(
                [value for value in raw_support if value not in anchors]
            ):
                quality_flags.append("UNKNOWN_MODEL_SUPPORT_DROPPED")
            support, support_trimmed = bounded_support(anchors, support)
            if support_trimmed:
                quality_flags.append("MODEL_SUPPORT_CHARACTER_BUDGET_TRIMMED")
            raw_kinds = item.get("kind_hints") or ["TASK"]
            if isinstance(raw_kinds, str):
                raw_kinds = [raw_kinds]
            kinds = list(
                dict.fromkeys(
                    part
                    for value in raw_kinds
                    for part in re.split(r"[|,/]", str(value).upper())
                    if part in {"TASK", "COMMITMENT", "DECISION"}
                )
            ) or ["TASK"]
            reason = str(item.get("reason_code") or "MODEL_PLAUSIBLE_ACTION").strip()
            candidates.append(
                RawCandidate(
                    candidate_id=_candidate_id(anchors, kinds, reason),
                    anchor_unit_ids=anchors,
                    support_unit_ids=support,
                    kind_hints=kinds,
                    reason_code=reason,
                    trigger_sources=["MODEL"],
                    quality_flags=quality_flags,
                )
            )
        except (TypeError, ValueError) as error:
            failures.append(
                {
                    "stage": "MODEL_DISCOVERY_VALIDATION",
                    "window_id": window.window_id,
                    "candidate_index": index,
                    "error": str(error),
                }
            )
    return candidates, failures


def _window_checkpoint_path(
    checkpoint_root: Path,
    window: RecallWindow,
    *,
    model: str | None,
) -> Path:
    """Version a provider response by every input that can change it."""

    identity = stable_hash(
        [
            RECALL_PROMPT_VERSION,
            model or "CUSTOM_COMPLETE_JSON",
            window.window_id,
        ]
    )[:16]
    return checkpoint_root / f"recall-{window.window_id}-{identity}.json"


def union_candidates(candidates: Iterable[RawCandidate]) -> list[RawCandidate]:
    """Union sources while removing only byte-for-byte semantic duplicates."""

    merged: dict[tuple[Any, ...], RawCandidate] = {}
    cross_source: dict[tuple[Any, ...], RawCandidate] = {}
    model_semantic: dict[tuple[Any, ...], RawCandidate] = {}
    for candidate in candidates:
        key = (
            tuple(candidate.anchor_unit_ids),
            tuple(candidate.support_unit_ids),
            tuple(sorted(candidate.kind_hints)),
            candidate.reason_code.casefold(),
        )
        existing = merged.get(key)
        source_key = (
            tuple(candidate.anchor_unit_ids),
            tuple(sorted(candidate.kind_hints)),
        )
        model_peer = model_semantic.get(source_key)
        candidate_is_model = set(candidate.trigger_sources) == {"MODEL"}
        recovery_flag = "VISIBLE_CONTEXT_ANCHOR_RECOVERED"
        if (
            existing is None
            and candidate_is_model
            and model_peer is not None
            and (
                recovery_flag in candidate.quality_flags
                or recovery_flag in model_peer.quality_flags
            )
        ):
            model_peer.support_unit_ids = list(
                dict.fromkeys(
                    model_peer.support_unit_ids + candidate.support_unit_ids
                )
            )
            combined_flags = list(
                dict.fromkeys(model_peer.quality_flags + candidate.quality_flags)
            )
            if not (
                recovery_flag in model_peer.quality_flags
                and recovery_flag in candidate.quality_flags
            ):
                combined_flags = [
                    value for value in combined_flags if value != recovery_flag
                ]
                combined_flags.append("VISIBLE_CONTEXT_DUPLICATE_MERGED")
            model_peer.quality_flags = combined_flags
            continue
        complementary = cross_source.get(source_key)
        if (
            existing is None
            and complementary is not None
            and set(complementary.trigger_sources) != set(candidate.trigger_sources)
            and set(complementary.trigger_sources + candidate.trigger_sources)
            == {"MODEL", "RULE"}
        ):
            complementary.support_unit_ids = list(
                dict.fromkeys(
                    complementary.support_unit_ids + candidate.support_unit_ids
                )
            )
            complementary.trigger_sources = ["MODEL", "RULE"]
            continue
        if existing is None:
            merged[key] = candidate
            cross_source[source_key] = candidate
            if candidate_is_model:
                model_semantic.setdefault(source_key, candidate)
            continue
        existing_was_recovered = recovery_flag in existing.quality_flags
        candidate_was_recovered = recovery_flag in candidate.quality_flags
        existing.trigger_sources = list(
            dict.fromkeys(existing.trigger_sources + candidate.trigger_sources)
        )
        existing.quality_flags = list(
            dict.fromkeys(existing.quality_flags + candidate.quality_flags)
        )
        if (
            existing_was_recovered or candidate_was_recovered
        ) and not (existing_was_recovered and candidate_was_recovered):
            existing.quality_flags = [
                value
                for value in existing.quality_flags
                if value != recovery_flag
            ]
            if candidate_is_model:
                existing.quality_flags.append("VISIBLE_CONTEXT_DUPLICATE_MERGED")
    return list(merged.values())


def _semantic_facts(text: str) -> set[str]:
    facts: set[str] = set()
    if _ACTION.search(text):
        facts.add("action")
    decision_strength = _decision_signal_strength(text)
    if decision_strength:
        facts.add("decision")
        facts.add(f"{decision_strength.lower()}_decision")
    if _ASSIGNMENT.search(text):
        facts.add("acceptance_or_owner")
        facts.add("owner_or_request")
    if _ACCEPTANCE.search(text):
        facts.add("acceptance")
    if _TIME.search(text):
        facts.add("time")
    if _CONDITION.search(text):
        facts.add("condition")
    if _QUESTION_OR_HYPOTHETICAL.search(text):
        facts.add("question_or_hypothetical")

    # Content beyond trigger words is only a proxy for an object/result. Keep
    # it deterministic, but strip common conversational filler so "需要做什么"
    # cannot become a task merely because three unrelated characters remain.
    residue = _ACTION.sub("", text)
    residue = _ASSIGNMENT.sub("", residue)
    residue = _DECISION.sub("", residue)
    residue = _TIME.sub("", residue)
    residue = _CONTENT_NOISE.sub("", residue)
    residue = re.sub(r"[\W_]+", "", residue, flags=re.UNICODE)
    if len(residue) >= 2:
        facts.add("object_or_result")
    return facts


def _draft_readiness(
    candidate: RawCandidate,
    *,
    anchor_text: str,
    evidence_text: str,
    context_text: str,
    linked_support_text: str,
) -> tuple[bool, str]:
    """Route retained evidence to a task draft or a non-task review hint.

    Discovery remains broad. This gate only decides whether the available
    evidence is safe to materialize as an ActionItem before human review.
    """

    anchor_facts = _semantic_facts(anchor_text)
    facts = _semantic_facts(evidence_text)
    context_facts = _semantic_facts(context_text)
    linked_support_facts = _semantic_facts(linked_support_text)
    external_acceptance = bool(
        _SHORT_CONFIRM.search(linked_support_text)
        or "acceptance" in linked_support_facts
    )

    if "SHORT_CONFIRMATION_WITHOUT_REFERENT" in candidate.quality_flags:
        return False, "SHORT_CONFIRMATION_WITHOUT_REFERENT"
    if "NEGATED_DECISION_SIGNAL" in candidate.quality_flags:
        return False, "NEGATED_DECISION_REQUIRES_REVIEW"
    if "ASSIGNMENT_ONLY_SIGNAL" in candidate.quality_flags:
        return False, "ASSIGNMENT_WITHOUT_ANCHORED_ACTION"

    questioned = "question_or_hypothetical" in anchor_facts
    if questioned and not external_acceptance:
        return False, "QUESTION_OR_HYPOTHETICAL_UNCONFIRMED"

    contextual_referent = bool(
        _PRONOUN.search(anchor_text)
        and context_facts.intersection({"object_or_result", "action"})
    )
    decision_has_content = bool(
        anchor_facts.intersection({"object_or_result", "action"})
        or linked_support_facts.intersection({"object_or_result", "action"})
        or contextual_referent
    )
    if "DECISION" in candidate.kind_hints:
        if "strong_decision" in anchor_facts:
            return (
                (True, "DRAFT_READY_STRONG_DECISION")
                if decision_has_content
                else (False, "DECISION_CONTENT_MISSING")
            )
        if "weak_decision" in anchor_facts:
            corroborated = bool(
                anchor_facts.intersection({"action", "time", "condition"})
                or linked_support_facts.intersection(
                    {"action", "acceptance", "time", "condition"}
                )
                or external_acceptance
                or contextual_referent
            )
            if decision_has_content and corroborated:
                return True, "DRAFT_READY_CORROBORATED_WEAK_DECISION"
            return False, "WEAK_DECISION_UNCORROBORATED"
        return False, "DECISION_SIGNAL_NOT_ANCHORED"

    if "COMMITMENT" in candidate.kind_hints and "TASK" not in candidate.kind_hints:
        anchored_event = bool(
            anchor_facts.intersection({"action", "strong_decision"})
        )
        accepted = bool(
            facts.intersection({"acceptance_or_owner", "time", "condition"})
            or external_acceptance
        )
        commitment_has_content = bool(
            "object_or_result" in anchor_facts
            or "object_or_result" in linked_support_facts
            or contextual_referent
        )
        if anchored_event and commitment_has_content and accepted:
            return True, "DRAFT_READY_COMMITMENT"
        return False, "COMMITMENT_EVIDENCE_INCOMPLETE"

    # Context may supply the object/result for an elliptical utterance, but it
    # may not lend an unrelated action to an assignment-only anchor.
    if "action" not in anchor_facts:
        return False, "ACTION_NOT_ANCHORED"
    task_has_object = bool(
        "object_or_result" in anchor_facts
        or "object_or_result" in linked_support_facts
        or contextual_referent
    )
    if not task_has_object:
        return False, "ACTION_OBJECT_OR_RESULT_MISSING"
    return True, "DRAFT_READY_TASK"


def resolve_evidence(
    candidate: RawCandidate,
    units: Sequence[TranscriptUnit],
    *,
    hard_character_budget: int = 1800,
) -> dict[str, Any]:
    """Resolve local ambiguity against the complete transcript unit list."""

    candidate = _normalize_candidate_context(candidate, units)
    by_id = {unit.unit_id: unit for unit in units}
    selected = {
        by_id[unit_id].index
        for unit_id in candidate.anchor_unit_ids + candidate.support_unit_ids
        if unit_id in by_id
    }
    if not selected:
        return {
            "candidate": candidate,
            "evidence_unit_ids": [],
            "context_unit_ids": [],
            "text": "",
            "sufficient": False,
            "routing_reason": "NO_RESOLVABLE_EVIDENCE",
            "facts": [],
            "expansion_steps": [],
        }
    linked_seed_indices = sorted(selected)

    anchor_text = " ".join(by_id[value].text for value in candidate.anchor_unit_ids if value in by_id)
    fragile = (
        len(_semantic_facts(anchor_text)) <= 1
        or bool(_SHORT_CONFIRM.fullmatch(anchor_text.strip()))
        or bool(_PRONOUN.search(anchor_text))
        or (_TIME.search(anchor_text) is not None and _ACTION.search(anchor_text) is None)
    )
    expansion_steps: list[dict[str, Any]] = []

    def current_text() -> str:
        return " ".join(units[index].text for index in sorted(selected))

    def supporting_text() -> str:
        anchor_ids = set(candidate.anchor_unit_ids)
        return " ".join(
            units[index].text
            for index in sorted(selected)
            if units[index].unit_id not in anchor_ids
        )

    def linked_support_text() -> str:
        support_ids = set(candidate.support_unit_ids)
        return " ".join(
            unit.text
            for unit_id, unit in by_id.items()
            if unit_id in support_ids
        )

    def readiness() -> tuple[bool, str]:
        return _draft_readiness(
            candidate,
            anchor_text=anchor_text,
            evidence_text=current_text(),
            context_text=supporting_text(),
            linked_support_text=linked_support_text(),
        )

    def used_characters() -> int:
        return sum(len(units[index].rendered) + 1 for index in selected)

    def add(index: int, reason: str) -> bool:
        if index < 0 or index >= len(units) or index in selected:
            return False
        cost = len(units[index].rendered) + 1
        if used_characters() + cost > hard_character_budget:
            return False
        selected.add(index)
        expansion_steps.append({"unit_id": units[index].unit_id, "reason": reason})
        return True

    # A linked anchor/support pair defines evidence points, not permission to
    # delete what was said between them. Fill short, topic-contiguous gaps when
    # the complete interval still fits the same hard evidence budget.
    for left, right in zip(linked_seed_indices, linked_seed_indices[1:]):
        bridge = [
            index for index in range(left + 1, right) if index not in selected
        ]
        if not bridge or any(_TOPIC.search(units[index].text) for index in bridge):
            continue
        bridge_cost = sum(len(units[index].rendered) + 1 for index in bridge)
        if used_characters() + bridge_cost > hard_character_budget:
            continue
        for index in bridge:
            add(index, "LINKED_EVIDENCE_BRIDGE")

    # "Only one fact" gets the same first treatment as an explicit fragile
    # form. Pronouns and confirmations are backward references, so they do not
    # pull an unrelated future utterance into draft readiness.
    if fragile:
        lower, upper = min(selected), max(selected)
        add(lower - 1, "INITIAL_PREVIOUS")
        if not (
            _SHORT_CONFIRM.fullmatch(anchor_text.strip())
            or _PRONOUN.search(anchor_text)
        ):
            add(upper + 1, "INITIAL_NEXT")

    left_cursor = min(selected) - 1
    right_cursor = max(selected) + 1
    left_only = bool(
        _SHORT_CONFIRM.fullmatch(anchor_text.strip()) or _PRONOUN.search(anchor_text)
    )
    anchor_facts = _semantic_facts(anchor_text)
    if "weak_decision" in anchor_facts:
        anchor_can_be_completed = bool(
            _PRONOUN.search(anchor_text)
            or candidate.support_unit_ids
            or "action" in anchor_facts
        )
    elif "strong_decision" in anchor_facts or "action" in anchor_facts:
        anchor_can_be_completed = bool(
            _PRONOUN.search(anchor_text) or candidate.support_unit_ids
        )
    else:
        anchor_can_be_completed = False
    ready, routing_reason = readiness()
    while not ready and anchor_can_be_completed:
        order = ("left",) if left_only else ("right", "left")
        changed = False
        for direction in order:
            cursor = left_cursor if direction == "left" else right_cursor
            if not 0 <= cursor < len(units):
                continue
            boundary = bool(_TOPIC.search(units[cursor].text))
            reason = f"ADAPTIVE_{direction.upper()}" + ("_SOFT_TOPIC_BOUNDARY" if boundary else "")
            if add(cursor, reason):
                changed = True
            if direction == "left":
                left_cursor -= 1
            else:
                right_cursor += 1
            ready, routing_reason = readiness()
            if ready:
                break
        if not changed:
            break

    ready, routing_reason = readiness()

    evidence_indices = sorted(selected)
    anchor_set = set(candidate.anchor_unit_ids)
    return {
        "candidate": candidate,
        "evidence_unit_ids": [units[index].unit_id for index in evidence_indices],
        "context_unit_ids": [
            units[index].unit_id
            for index in evidence_indices
            if units[index].unit_id not in anchor_set
        ],
        "text": current_text(),
        "sufficient": ready,
        "routing_reason": routing_reason,
        "facts": sorted(_semantic_facts(current_text())),
        "expansion_steps": expansion_steps,
    }


def _source_for_resolution(
    resolution: dict[str, Any], unit_by_id: dict[str, TranscriptUnit]
) -> TranscriptUnit:
    candidate: RawCandidate = resolution["candidate"]
    for unit_id in candidate.anchor_unit_ids + resolution["evidence_unit_ids"]:
        if unit_id in unit_by_id:
            return unit_by_id[unit_id]
    raise ExtractionError("resolved candidate has no source unit")


def _deadline_text(text: str) -> str | None:
    match = _TIME.search(text)
    return match.group(0) if match else None


def _fallback_draft(
    resolution: dict[str, Any], unit_by_id: dict[str, TranscriptUnit]
) -> dict[str, Any]:
    candidate: RawCandidate = resolution["candidate"]
    source = _source_for_resolution(resolution, unit_by_id)
    text = _SPACE.sub(" ", resolution["text"]).strip()
    title = re.sub(r"^[，,。；;：:\s]+|[。！？!?；;\s]+$", "", source.text).strip()
    if len(title) > 48:
        title = title[:48].rstrip("，,：:；;")
    owner = (
        source.speaker
        if source.speaker and re.search(r"(?:我来|我会|我负责|I(?:'ll| will))", source.text, re.I)
        else None
    )
    deadline = _deadline_text(text)
    item_type = "COMMITMENT" if "COMMITMENT" in candidate.kind_hints or "DECISION" in candidate.kind_hints else "TASK"
    return {
        "candidate_id": candidate.candidate_id,
        "title": title or "待负责人补充的会议行动",
        "item_type": item_type,
        "deliverable": text[:240] or source.text,
        "owner_name": owner,
        "deadline_text": deadline,
        "deadline_iso": None,
        "source_timestamp": source.timestamp,
        "source_quote": source.text[:160],
        "confidence": 0.62 if "MODEL" in candidate.trigger_sources else 0.54,
        "needs_confirmation": True,
        "uncertainties": ["召回优先候选，需由会议负责人确认任务定义"],
        "collaborator_names": [],
        "evidence_unit_ids": resolution["evidence_unit_ids"],
        "context_unit_ids": resolution["context_unit_ids"],
        "trigger_sources": candidate.trigger_sources,
        "reason_code": candidate.reason_code,
    }


def _review_hint(
    resolution: dict[str, Any],
    unit_by_id: dict[str, TranscriptUnit],
    *,
    reason: str = "MINIMUM_SEMANTICS_UNRESOLVED",
) -> dict[str, Any]:
    candidate: RawCandidate = resolution["candidate"]
    source = _source_for_resolution(resolution, unit_by_id)
    return {
        "hint_id": "hint_" + stable_hash([candidate.candidate_id, reason])[:20],
        "candidate_id": candidate.candidate_id,
        "status": "OPEN",
        "reason_code": reason,
        "source_timestamp": source.timestamp,
        "source_quote": source.text[:160],
        "evidence_text": resolution["text"][:600],
        "anchor_unit_ids": candidate.anchor_unit_ids,
        "evidence_unit_ids": resolution["evidence_unit_ids"],
        "context_unit_ids": resolution["context_unit_ids"],
        "facts_found": resolution["facts"],
        "kind_hints": candidate.kind_hints,
        "trigger_sources": candidate.trigger_sources,
        "quality_flags": candidate.quality_flags,
        "expansion_steps": resolution["expansion_steps"],
    }


def _structure_prompt(
    resolutions: Sequence[dict[str, Any]],
    unit_by_id: dict[str, TranscriptUnit],
    meeting_date: str | None,
) -> list[dict[str, str]]:
    rows = []
    for resolution in resolutions:
        candidate: RawCandidate = resolution["candidate"]
        rows.append(
            {
                "candidate_id": candidate.candidate_id,
                "kind_hints": candidate.kind_hints,
                "evidence": [
                    {
                        "unit_id": unit_id,
                        "speaker": unit_by_id[unit_id].speaker,
                        "timestamp": unit_by_id[unit_id].timestamp,
                        "text": unit_by_id[unit_id].text,
                    }
                    for unit_id in resolution["evidence_unit_ids"]
                ],
            }
        )
    system = """You structure already-discovered meeting action candidates. You are not doing another recall sweep.
Return JSON only: {"items":[{"candidate_id":"rc_...","title":"...","item_type":"TASK|COMMITMENT","deliverable":"...","owner_name":null,"deadline_text":null,"deadline_iso":null,"confidence":0.0,"needs_confirmation":true,"uncertainties":[],"collaborator_names":[]}]}.
Return at most one item per supplied candidate_id and never create a new candidate. Do not output source quotes or timestamps: the system reconstructs those deterministically. Do not invent owners, collaborators, dates or deliverables. Missing owner or ISO deadline requires needs_confirmation=true. Keep a broad candidate when the evidence can support action+object/result; uncertainty belongs in uncertainties, not silent deletion."""
    user = json.dumps(
        {"meeting_date": meeting_date, "candidates": rows},
        ensure_ascii=False,
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _validated_structured_item(
    raw: Any,
    resolution: dict[str, Any],
    unit_by_id: dict[str, TranscriptUnit],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("structured item must be an object")
    candidate: RawCandidate = resolution["candidate"]
    if str(raw.get("candidate_id") or "") != candidate.candidate_id:
        raise ValueError("structured item candidate_id does not match")
    source = _source_for_resolution(resolution, unit_by_id)
    item = dict(raw)
    item["source_timestamp"] = source.timestamp
    item["source_quote"] = source.text[:160]
    normalized, _ = normalize_extraction_payload({"action_items": [item]})
    validated = validate_extraction(normalized)
    if len(validated) != 1:
        raise ValueError("structured item did not produce one valid action item")
    result = asdict(validated[0])
    result.update(
        {
            "candidate_id": candidate.candidate_id,
            "evidence_unit_ids": resolution["evidence_unit_ids"],
            "context_unit_ids": resolution["context_unit_ids"],
            "trigger_sources": candidate.trigger_sources,
            "reason_code": candidate.reason_code,
        }
    )
    return result


class RecallFirstExtractor:
    """High-recall extractor with isolated failures and inspectable stages."""

    def __init__(
        self,
        *,
        client: BailianExtractor | None = None,
        complete_json: Callable[[list[dict[str, str]]], str | dict[str, Any]] | None = None,
        model: str | None = None,
        window_policy: WindowPolicy | None = None,
        evidence_character_budget: int = 1800,
        model_support_character_budget: int = DEFAULT_MODEL_SUPPORT_CHARACTERS,
        structure_batch_size: int = 12,
        structure_candidates: bool = True,
        discovery_workers: int = 1,
    ) -> None:
        if client is not None and complete_json is not None:
            raise ValueError("pass either client or complete_json, not both")
        self.client = client
        self._complete = complete_json
        self.model_override = model
        self.window_policy = window_policy or WindowPolicy()
        self.evidence_character_budget = max(400, int(evidence_character_budget))
        self.model_support_character_budget = max(
            120,
            min(
                int(model_support_character_budget),
                self.evidence_character_budget,
            ),
        )
        self.structure_batch_size = max(1, int(structure_batch_size))
        self.structure_candidates = bool(structure_candidates)
        self.discovery_workers = max(1, int(discovery_workers))
        self.last_artifact: dict[str, Any] | None = None

    @property
    def prompt_version(self) -> str:
        return RECALL_PROMPT_VERSION

    @property
    def model(self) -> str | None:
        if self.client is not None:
            return self.client.model
        return self.model_override

    def _complete_json(self, messages: list[dict[str, str]]) -> str | dict[str, Any]:
        if self._complete is not None:
            return self._complete(messages)
        if self.client is None:
            self.client = BailianExtractor(model=self.model_override, use_tools=False)
        return self.client.complete_json(messages)

    def extract(
        self,
        transcript: str,
        *,
        meeting_date: str | None = None,
        checkpoint_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        transcript_hash = stable_hash(transcript)
        usage_start = (
            len(getattr(self.client, "json_usage_records", []))
            if self.client is not None
            else 0
        )
        units = build_transcript_units(
            transcript, max_unit_characters=self.window_policy.max_unit_characters
        )
        windows = build_recall_windows(units, self.window_policy)
        coverage = assert_emit_coverage(units, windows)
        unit_by_id = {unit.unit_id: unit for unit in units}
        failures: list[dict[str, Any]] = []
        model_candidates: list[RawCandidate] = []
        model_windows_succeeded = 0
        checkpoints = Path(checkpoint_dir) if checkpoint_dir else None
        if checkpoints:
            checkpoints.mkdir(parents=True, exist_ok=True)

        if self._complete is None and self.client is None:
            self.client = BailianExtractor(model=self.model_override, use_tools=False)

        def discover_window(
            window: RecallWindow,
        ) -> tuple[list[RawCandidate], list[dict[str, Any]], bool]:
            checkpoint = (
                _window_checkpoint_path(
                    checkpoints,
                    window,
                    model=(
                        self.model or DEFAULT_BAILIAN_MODEL
                        if self._complete is None
                        else self.model
                    ),
                )
                if checkpoints
                else None
            )
            try:
                if checkpoint and checkpoint.exists():
                    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
                else:
                    payload = _parse_json_object(
                        self._complete_json(_window_prompt(window, unit_by_id))
                    )
                    if checkpoint:
                        checkpoint.write_text(
                            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                found, invalid = _model_candidates(
                    payload,
                    window=window,
                    unit_by_id=unit_by_id,
                    support_character_budget=self.model_support_character_budget,
                )
                return found, invalid, True
            except Exception as error:  # one window degrades, not the meeting
                return [], [
                    {
                        "stage": "MODEL_DISCOVERY",
                        "window_id": window.window_id,
                        "error": str(error)[:500],
                    }
                ], False

        if self.discovery_workers == 1:
            discovered = map(discover_window, windows)
            for found, issues, succeeded in discovered:
                model_candidates.extend(found)
                failures.extend(issues)
                model_windows_succeeded += int(succeeded)
        else:
            with ThreadPoolExecutor(max_workers=self.discovery_workers) as pool:
                for found, issues, succeeded in pool.map(discover_window, windows):
                    model_candidates.extend(found)
                    failures.extend(issues)
                    model_windows_succeeded += int(succeeded)

        model_candidates = [
            _normalize_candidate_context(candidate, units)
            for candidate in model_candidates
        ]
        rule_candidates = rule_recall(units)
        candidates = union_candidates([*model_candidates, *rule_candidates])
        resolutions = [
            resolve_evidence(
                candidate,
                units,
                hard_character_budget=self.evidence_character_budget,
            )
            for candidate in candidates
        ]
        sufficient = [value for value in resolutions if value["sufficient"]]
        review_hints = [
            _review_hint(
                value,
                unit_by_id,
                reason=str(value.get("routing_reason") or "MINIMUM_SEMANTICS_UNRESOLVED"),
            )
            for value in resolutions
            if not value["sufficient"] and value["evidence_unit_ids"]
        ]

        # Start from a deterministic draft for every sufficient candidate.
        # Model structuring can improve it, but cannot make it disappear.
        drafts_by_candidate = {
            value["candidate"].candidate_id: _fallback_draft(value, unit_by_id)
            for value in sufficient
        }
        structure_offsets = (
            range(0, len(sufficient), self.structure_batch_size)
            if self.structure_candidates
            else ()
        )
        for offset in structure_offsets:
            batch = sufficient[offset : offset + self.structure_batch_size]
            try:
                response = _parse_json_object(
                    self._complete_json(
                        _structure_prompt(batch, unit_by_id, meeting_date)
                    )
                )
                raw_items = response.get("items")
                if not isinstance(raw_items, list):
                    raise ValueError("structure response.items must be an array")
                resolution_by_id = {
                    value["candidate"].candidate_id: value for value in batch
                }
                seen_ids: set[str] = set()
                for index, raw in enumerate(raw_items):
                    candidate_id = str(raw.get("candidate_id") or "") if isinstance(raw, dict) else ""
                    resolution = resolution_by_id.get(candidate_id)
                    if resolution is None:
                        failures.append(
                            {
                                "stage": "STRUCTURE_VALIDATION",
                                "candidate_index": index,
                                "error": "unknown candidate_id",
                            }
                        )
                        continue
                    try:
                        drafts_by_candidate[candidate_id] = _validated_structured_item(
                            raw, resolution, unit_by_id
                        )
                        seen_ids.add(candidate_id)
                    except (ExtractionError, TypeError, ValueError) as error:
                        failures.append(
                            {
                                "stage": "STRUCTURE_VALIDATION",
                                "candidate_id": candidate_id,
                                "error": str(error),
                            }
                        )
                for candidate_id in set(resolution_by_id) - seen_ids:
                    failures.append(
                        {
                            "stage": "STRUCTURE_VALIDATION",
                            "candidate_id": candidate_id,
                            "error": "model omitted candidate; deterministic draft retained",
                        }
                    )
            except Exception as error:
                failures.append(
                    {
                        "stage": "MODEL_STRUCTURING",
                        "candidate_ids": [
                            value["candidate"].candidate_id for value in batch
                        ],
                        "error": str(error)[:500],
                    }
                )

        # Exact domain identity only. Raw candidates remain available so this
        # compatibility projection never erases recall evidence.
        draft_items: list[dict[str, Any]] = []
        identities: set[tuple[str, str, str]] = set()
        for draft in drafts_by_candidate.values():
            identity = (
                str(draft.get("title") or "").casefold(),
                str(draft.get("owner_name") or "").casefold(),
                str(draft.get("source_timestamp") or ""),
            )
            if identity in identities:
                continue
            identities.add(identity)
            draft_items.append(draft)

        routing_reasons: dict[str, int] = {}
        for resolution in resolutions:
            reason = str(resolution.get("routing_reason") or "UNKNOWN")
            routing_reasons[reason] = routing_reasons.get(reason, 0) + 1

        usage_records = (
            list(getattr(self.client, "json_usage_records", []))[usage_start:]
            if self.client is not None
            else []
        )
        usage = {
            key: (
                sum(
                    int(record[key])
                    for record in usage_records
                    if isinstance(record.get(key), int)
                )
                if any(isinstance(record.get(key), int) for record in usage_records)
                else None
            )
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }

        degraded = bool(failures) or model_windows_succeeded < len(windows)
        artifact = {
            "provider": "bailian",
            "model": self.model,
            "prompt_version": RECALL_PROMPT_VERSION,
            "input_sha256": transcript_hash,
            "pipeline_status": "DEGRADED" if degraded else "SUCCEEDED",
            "policies": {
                "window": WINDOW_POLICY_VERSION,
                "window_configuration": asdict(self.window_policy),
                "discovery_workers": self.discovery_workers,
                "model_candidate": MODEL_CANDIDATE_POLICY_VERSION,
                "model_support_character_budget": (
                    self.model_support_character_budget
                ),
                "rules": RULE_POLICY_VERSION,
                "evidence": EVIDENCE_POLICY_VERSION,
                "legacy_compatibility": ACTION_ITEM_EXTRACTION_PROMPT_VERSION,
            },
            "units": [asdict(unit) for unit in units],
            "windows": [asdict(window) for window in windows],
            "raw_candidates": [asdict(candidate) for candidate in candidates],
            "draft_items": draft_items,
            "review_hints": review_hints,
            "action_items": draft_items,
            "failures": failures,
            "usage": usage,
            "token_calls": usage_records,
            "coverage": {
                **coverage,
                "windows_total": len(windows),
                "model_windows_succeeded": model_windows_succeeded,
                "model_window_success_rate": (
                    round(model_windows_succeeded / len(windows), 4)
                    if windows
                    else 1.0
                ),
                "rule_candidates": len(rule_candidates),
                "model_candidates": len(model_candidates),
                "raw_candidates": len(candidates),
                "sufficient_candidates": len(sufficient),
                "draft_ready_candidates": len(sufficient),
                "weak_signal_candidates": sum(
                    bool(
                        set(value["candidate"].quality_flags).intersection(
                            {
                                "WEAK_DECISION_SIGNAL",
                                "ASSIGNMENT_ONLY_SIGNAL",
                                "NEGATED_DECISION_SIGNAL",
                            }
                        )
                    )
                    for value in resolutions
                ),
                "anchor_unit_references": sum(
                    len(candidate.anchor_unit_ids) for candidate in candidates
                ),
                "support_unit_references": sum(
                    len(candidate.support_unit_ids) for candidate in candidates
                ),
                "evidence_unit_references": sum(
                    len(value["evidence_unit_ids"]) for value in resolutions
                ),
                "evidence_bridge_unit_references": sum(
                    step.get("reason") == "LINKED_EVIDENCE_BRIDGE"
                    for value in resolutions
                    for step in value.get("expansion_steps") or []
                ),
                "model_support_trimmed_candidates": sum(
                    "MODEL_SUPPORT_CHARACTER_BUDGET_TRIMMED"
                    in candidate.quality_flags
                    for candidate in model_candidates
                ),
                "model_unknown_reference_candidates": sum(
                    bool(
                        {
                            "UNKNOWN_MODEL_ANCHOR_DROPPED",
                            "UNKNOWN_MODEL_SUPPORT_DROPPED",
                        }.intersection(candidate.quality_flags)
                    )
                    for candidate in model_candidates
                ),
                "model_unit_id_canonicalized_candidates": sum(
                    "MODEL_UNIT_ID_CANONICALIZED" in candidate.quality_flags
                    for candidate in candidates
                ),
                "visible_context_anchor_recovered_candidates": sum(
                    "VISIBLE_CONTEXT_ANCHOR_RECOVERED"
                    in candidate.quality_flags
                    for candidate in candidates
                ),
                "visible_context_duplicate_merged_candidates": sum(
                    "VISIBLE_CONTEXT_DUPLICATE_MERGED"
                    in candidate.quality_flags
                    for candidate in candidates
                ),
                "routing_reasons": routing_reasons,
                "draft_items": len(draft_items),
                "review_hints": len(review_hints),
            },
            "summary": {
                "total": len(draft_items),
                "review_hints": len(review_hints),
                "needs_confirmation": sum(
                    bool(item.get("needs_confirmation")) for item in draft_items
                ),
                "candidate_failures": len(failures),
            },
            "invocation": {
                "capability_type": "MODEL_AND_RULES",
                "principal": {
                    "actor_id": "SYSTEM",
                    "episode_id": None,
                    "roles": ["SYSTEM"],
                    "auth_source": "INTERNAL_SERVICE",
                },
                "purpose": (
                    "RECALL_FIRST_ACTION_ITEM_EXTRACTION"
                    if self.structure_candidates
                    else "RECALL_FIRST_CANDIDATE_DISCOVERY_EVALUATION"
                ),
                "field_allowlist": ["transcript", "meeting_date"],
                "entity_versions": {"transcript_sha256": transcript_hash},
                "prompt_version": RECALL_PROMPT_VERSION,
                "skill_version": None,
                "input_hash": stable_hash(
                    [
                        transcript_hash,
                        meeting_date,
                        RECALL_PROMPT_VERSION,
                        asdict(self.window_policy),
                        MODEL_CANDIDATE_POLICY_VERSION,
                        self.model_support_character_budget,
                        RULE_POLICY_VERSION,
                        EVIDENCE_POLICY_VERSION,
                        self.structure_candidates,
                    ]
                ),
                "output_status": "DEGRADED" if degraded else "SUCCEEDED",
            },
        }
        self.last_artifact = artifact
        return artifact
