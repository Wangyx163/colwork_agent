from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .models import read_text_file, stable_hash


DEFAULT_BAILIAN_ENDPOINT = (
    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
)
DEFAULT_BAILIAN_MODEL = "qwen-plus"
ACTION_ITEM_EXTRACTION_PROMPT_VERSION = "meeting-action-items.v1.4"
# The tool-calling variant is a separate version rather than an edit of v1.4,
# so evaluation numbers from the two stay comparable and either can be run
# against the same corpus without invalidating the other's history.
ACTION_ITEM_EXTRACTION_TOOLS_PROMPT_VERSION = "meeting-action-items.tools.v2.0"
MAX_TOOL_ROUNDS = 6
ITEM_TYPES = ("TASK", "COMMITMENT")
TIMESTAMP_PATTERN = re.compile(r"^\d{2}:\d{2}:\d{2}$")
TRANSCRIPT_LINE_PATTERN = re.compile(
    r"^(?P<speaker>.+?)\((?P<timestamp>\d{2}:\d{2}:\d{2})\):\s*(?P<text>.*)$"
)


class ExtractionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "EXTRACTION_ERROR",
        stage: str = "EXTRACTION",
        retryable: bool = False,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.stage = stage
        self.retryable = retryable


@dataclass(frozen=True)
class ExtractedActionItem:
    title: str
    # TASK produces something; COMMITMENT is a time point the meeting agreed
    # to with no artefact of its own. Older extractions carry no type, so the
    # default keeps them readable.
    item_type: str
    deliverable: str
    owner_name: str | None
    deadline_text: str | None
    deadline_iso: str | None
    source_timestamp: str
    source_quote: str
    confidence: float
    needs_confirmation: bool
    uncertainties: list[str]
    collaborator_names: list[str]


def normalize_extraction_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Repair only fail-closed structural contradictions in model JSON.

    This deliberately does not invent task semantics, confidence, or source
    evidence. Those fields remain subject to strict contract validation.
    """
    normalized = deepcopy(payload)
    raw_items = normalized.get("action_items")
    actions: list[str] = []
    if not isinstance(raw_items, list):
        return normalized, actions

    unresolved_message = "负责人或截止时间未完整明确，系统强制进入人工确认"
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        prefix = f"action_items[{index}]"

        for field in ("owner_name", "deadline_text", "deadline_iso"):
            if field not in item:
                item[field] = None
                actions.append(f"{prefix}.{field}:missing_to_null")
            elif isinstance(item[field], str) and not item[field].strip():
                item[field] = None
                actions.append(f"{prefix}.{field}:blank_to_null")

        raw_type = str(item.get("item_type") or "").strip().upper()
        if raw_type not in ITEM_TYPES:
            # Everything extracted before v1.4 was a deliverable-bearing task.
            item["item_type"] = "TASK"
            if raw_type:
                actions.append(f"{prefix}.item_type:invalid_to_task")
            else:
                actions.append(f"{prefix}.item_type:missing_to_task")
        else:
            item["item_type"] = raw_type
        if "needs_confirmation" not in item:
            item["needs_confirmation"] = True
            actions.append(f"{prefix}.needs_confirmation:missing_to_true")
        elif not isinstance(item["needs_confirmation"], bool):
            item["needs_confirmation"] = True
            actions.append(f"{prefix}.needs_confirmation:invalid_to_true")

        uncertainties = item.get("uncertainties")
        if "uncertainties" not in item or uncertainties is None:
            item["uncertainties"] = []
            actions.append(f"{prefix}.uncertainties:missing_to_empty_list")
        elif isinstance(uncertainties, str):
            item["uncertainties"] = (
                [uncertainties.strip()] if uncertainties.strip() else []
            )
            actions.append(f"{prefix}.uncertainties:string_to_list")
        elif isinstance(uncertainties, list):
            cleaned = [
                value.strip()
                for value in uncertainties
                if isinstance(value, str) and value.strip()
            ]
            if cleaned != uncertainties:
                item["uncertainties"] = cleaned
                actions.append(f"{prefix}.uncertainties:invalid_values_removed")
        else:
            item["uncertainties"] = []
            actions.append(f"{prefix}.uncertainties:invalid_to_empty_list")

        owner_unresolved = item.get("owner_name") in (None, "")
        deadline_unresolved = item.get("deadline_iso") in (None, "")
        if (owner_unresolved or deadline_unresolved) and not item.get(
            "needs_confirmation"
        ):
            item["needs_confirmation"] = True
            actions.append(
                f"{prefix}.needs_confirmation:false_to_true_for_unresolved_fields"
            )
        if owner_unresolved or deadline_unresolved:
            if unresolved_message not in item["uncertainties"]:
                item["uncertainties"].append(unresolved_message)

    return normalized, actions


def validate_extraction(payload: dict[str, Any]) -> list[ExtractedActionItem]:
    raw_items = payload.get("action_items")
    if not isinstance(raw_items, list):
        raise ExtractionError("response.action_items must be an array")
    validated: list[ExtractedActionItem] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise ExtractionError(f"action_items[{index}] must be an object")
        required = {
            "title",
            "deliverable",
            "owner_name",
            "deadline_text",
            "deadline_iso",
            "source_timestamp",
            "source_quote",
            "confidence",
            "needs_confirmation",
            "uncertainties",
        }
        missing = sorted(required - set(item))
        if missing:
            raise ExtractionError(f"action_items[{index}] missing fields: {missing}")
        title = str(item["title"]).strip()
        item_type = str(item.get("item_type") or "TASK").strip().upper()
        if item_type not in ITEM_TYPES:
            raise ExtractionError(
                f"action_items[{index}].item_type must be one of {list(ITEM_TYPES)}"
            )
        deliverable = str(item["deliverable"]).strip()
        timestamp = str(item["source_timestamp"]).strip()
        quote = str(item["source_quote"]).strip()
        if not title or not deliverable or not quote:
            raise ExtractionError(f"action_items[{index}] has an empty required string")
        if not TIMESTAMP_PATTERN.fullmatch(timestamp):
            raise ExtractionError(
                f"action_items[{index}].source_timestamp must be HH:MM:SS"
            )
        confidence = float(item["confidence"])
        if not 0.0 <= confidence <= 1.0:
            raise ExtractionError(f"action_items[{index}].confidence must be 0..1")
        uncertainties = item["uncertainties"]
        if not isinstance(uncertainties, list) or not all(
            isinstance(value, str) for value in uncertainties
        ):
            raise ExtractionError(
                f"action_items[{index}].uncertainties must be a string array"
            )
        raw_collaborators = item.get("collaborator_names", [])
        if not isinstance(raw_collaborators, list) or not all(
            isinstance(value, str) for value in raw_collaborators
        ):
            raise ExtractionError(
                f"action_items[{index}].collaborator_names must be a string array"
            )
        collaborator_names = list(
            dict.fromkeys(
                value.strip() for value in raw_collaborators if value.strip()
            )
        )
        owner = item["owner_name"]
        deadline_text = item["deadline_text"]
        deadline_iso = item["deadline_iso"]
        owner = str(owner).strip() if owner not in (None, "") else None
        deadline_text = (
            str(deadline_text).strip() if deadline_text not in (None, "") else None
        )
        deadline_iso = (
            str(deadline_iso).strip() if deadline_iso not in (None, "") else None
        )
        if not isinstance(item["needs_confirmation"], bool):
            raise ExtractionError(
                f"action_items[{index}].needs_confirmation must be boolean"
            )
        needs_confirmation = item["needs_confirmation"]
        if (owner is None or deadline_iso is None) and not needs_confirmation:
            raise ExtractionError(
                f"action_items[{index}] must need confirmation when owner/deadline is null"
            )
        identity = (title.casefold(), owner or "", timestamp)
        if identity in seen:
            continue
        seen.add(identity)
        validated.append(
            ExtractedActionItem(
                title=title,
                item_type=item_type,
                deliverable=deliverable,
                owner_name=owner,
                deadline_text=deadline_text,
                deadline_iso=deadline_iso,
                source_timestamp=timestamp,
                source_quote=quote[:160],
                confidence=round(confidence, 3),
                needs_confirmation=needs_confirmation,
                uncertainties=[value.strip() for value in uncertainties if value.strip()],
                collaborator_names=collaborator_names,
            )
        )
    return validated


def validate_source_evidence(
    items: list[ExtractedActionItem], transcript: str
) -> None:
    utterances: dict[str, list[str]] = {}
    for line in transcript.splitlines():
        match = TRANSCRIPT_LINE_PATTERN.match(line.strip())
        if match:
            utterances.setdefault(match.group("timestamp"), []).append(
                re.sub(r"\s+", " ", match.group("text")).strip()
            )
    for index, item in enumerate(items):
        quote = re.sub(r"\s+", " ", item.source_quote).strip()
        matching_utterances = utterances.get(item.source_timestamp, [])
        if not any(quote in utterance for utterance in matching_utterances):
            raise ExtractionError(
                f"action_items[{index}] source quote is not present at "
                f"{item.source_timestamp}",
                error_code="INVALID_CITATION",
                stage="SOURCE_EVIDENCE_ALIGNMENT",
                retryable=True,
            )


def align_source_evidence(
    items: list[ExtractedActionItem], transcript: str
) -> tuple[list[ExtractedActionItem], int]:
    utterances: dict[str, list[str]] = {}
    for line in transcript.splitlines():
        match = TRANSCRIPT_LINE_PATTERN.match(line.strip())
        if match:
            utterances.setdefault(match.group("timestamp"), []).append(
                re.sub(r"\s+", " ", match.group("text")).strip()
            )
    aligned: list[ExtractedActionItem] = []
    repair_count = 0
    for item in items:
        quote = re.sub(r"\s+", " ", item.source_quote).strip()
        candidates = utterances.get(item.source_timestamp, [])
        if any(quote in utterance for utterance in candidates):
            aligned.append(item)
            continue
        exact_locations = [
            timestamp
            for timestamp, timestamp_utterances in utterances.items()
            if any(quote in utterance for utterance in timestamp_utterances)
        ]
        if len(exact_locations) == 1:
            repaired_uncertainties = list(item.uncertainties)
            repaired_uncertainties.append(
                "source_timestamp 已由系统按唯一逐字引文对齐"
            )
            aligned.append(
                replace(
                    item,
                    source_timestamp=exact_locations[0],
                    uncertainties=repaired_uncertainties,
                )
            )
            repair_count += 1
            continue
        fuzzy_matches: list[tuple[int, str, str, Any]] = []
        for timestamp, timestamp_utterances in utterances.items():
            for utterance in timestamp_utterances:
                match = SequenceMatcher(
                    None, quote, utterance, autojunk=False
                ).find_longest_match()
                fuzzy_matches.append((match.size, timestamp, utterance, match))
        fuzzy_matches.sort(key=lambda value: value[0], reverse=True)
        best_size, best_timestamp, best_utterance, best_match = (
            fuzzy_matches[0] if fuzzy_matches else (0, "", "", None)
        )
        second_size = fuzzy_matches[1][0] if len(fuzzy_matches) > 1 else 0
        same_timestamp = best_timestamp == item.source_timestamp
        minimum_anchor = 8 if same_timestamp else 12
        coverage = best_size / max(1, min(len(quote), len(best_utterance)))
        unique_enough = same_timestamp or best_size >= second_size + 4
        if (
            best_match is None
            or best_size < minimum_anchor
            or coverage < 0.3
            or not unique_enough
        ):
            raise ExtractionError(
                "cannot align source quote for "
                f"{item.title!r} at {item.source_timestamp}; "
                f"model_quote={quote!r}; transcript_utterances={candidates!r}",
                error_code="INVALID_CITATION",
                stage="SOURCE_EVIDENCE_ALIGNMENT",
                retryable=True,
            )
        start = max(0, best_match.b - 48)
        end = min(len(best_utterance), start + 160)
        if best_match.b + best_match.size > end:
            end = min(len(best_utterance), best_match.b + best_match.size)
            start = max(0, end - 160)
        exact_quote = best_utterance[start:end].strip()
        repaired_uncertainties = list(item.uncertainties)
        repaired_uncertainties.append(
            "source_quote/source_timestamp 已由系统按唯一长原文锚点对齐"
        )
        aligned.append(
            replace(
                item,
                source_timestamp=best_timestamp,
                source_quote=exact_quote,
                uncertainties=repaired_uncertainties,
            )
        )
        repair_count += 1
    return aligned, repair_count


def align_owner_evidence(
    items: list[ExtractedActionItem], transcript: str
) -> tuple[list[ExtractedActionItem], int]:
    """Remove owner hints that are not supported by assignment or commitment text."""
    utterances: dict[str, list[tuple[str, str]]] = {}
    for line in transcript.splitlines():
        match = TRANSCRIPT_LINE_PATTERN.match(line.strip())
        if match:
            utterances.setdefault(match.group("timestamp"), []).append(
                (
                    match.group("speaker").strip(),
                    re.sub(r"\s+", " ", match.group("text")).strip(),
                )
            )
    aligned: list[ExtractedActionItem] = []
    repair_count = 0
    first_person_commitment = re.compile(
        r"我(?:来|会|去|再|负责|准备|整理|确认|询问|问|拍|发|提交|完善|可以辅助)"
    )
    assignment_verbs = re.compile(
        r"(?:负责|去|来|整理|准备|确认|询问|问|拍|发|提交|完善|起(?:一|另)个|做)"
    )
    for item in items:
        if item.owner_name is None:
            aligned.append(item)
            continue
        quote = re.sub(r"\s+", " ", item.source_quote).strip()
        matching = [
            (speaker, utterance)
            for speaker, utterance in utterances.get(item.source_timestamp, [])
            if quote in utterance
        ]
        supported = False
        for speaker, utterance in matching:
            if item.owner_name == speaker and first_person_commitment.search(utterance):
                supported = True
                break
            owner_position = utterance.find(item.owner_name)
            if owner_position >= 0 and assignment_verbs.search(
                utterance[owner_position + len(item.owner_name) :]
            ):
                supported = True
                break
        if supported:
            aligned.append(item)
            continue
        repaired_uncertainties = list(item.uncertainties)
        repaired_uncertainties.append(
            "owner_name 缺少明确指派或第一人称承诺，已改为待负责人确认"
        )
        aligned.append(
            replace(
                item,
                owner_name=None,
                needs_confirmation=True,
                uncertainties=repaired_uncertainties,
            )
        )
        repair_count += 1
    return aligned, repair_count


def align_collaboration_evidence(
    items: list[ExtractedActionItem],
    transcript: str,
    *,
    authorized_speaker: str | None = None,
) -> tuple[list[ExtractedActionItem], int]:
    """Keep collaborators only when the cited utterance explicitly supports them."""
    utterances: dict[str, list[tuple[str, str]]] = {}
    for line in transcript.splitlines():
        match = TRANSCRIPT_LINE_PATTERN.match(line.strip())
        if match:
            utterances.setdefault(match.group("timestamp"), []).append(
                (
                    match.group("speaker").strip(),
                    re.sub(r"\s+", " ", match.group("text")).strip(),
                )
            )
    cooperation_expression = re.compile(
        r"(?:一起|一块|共同|协作|合作|配合|辅助|搭档|联手)"
    )
    aligned: list[ExtractedActionItem] = []
    repair_count = 0
    for item in items:
        if not item.collaborator_names:
            aligned.append(item)
            continue
        quote = re.sub(r"\s+", " ", item.source_quote).strip()
        matching = [
            (speaker, utterance)
            for speaker, utterance in utterances.get(item.source_timestamp, [])
            if quote in utterance
        ]
        supported = any(
            (
                authorized_speaker is None
                or speaker.casefold() == authorized_speaker.casefold()
            )
            and
            cooperation_expression.search(utterance)
            and all(name in utterance for name in item.collaborator_names)
            for speaker, utterance in matching
        )
        if supported:
            aligned.append(item)
            continue
        uncertainties = list(item.uncertainties)
        uncertainties.append(
            "collaborator_names 缺少会议组织人在同一条原文中的明确合作指派，已按单人任务处理"
        )
        aligned.append(
            replace(
                item,
                collaborator_names=[],
                needs_confirmation=True,
                uncertainties=uncertainties,
            )
        )
        repair_count += 1
    return aligned, repair_count


def _system_prompt() -> str:
    return """你是办公会议行动项抽取器。请从逐字稿中找出会后需要执行、调研、产出、提交、发布或跟进的具体行动。

严格规则：
1. 只输出 JSON，不要 Markdown。
2. 不得猜测负责人或截止时间；逐字稿未明确时必须填 null。
3. 讨论中的想法、愿景、已完成事项、单纯介绍不是行动项。
4. 相同任务只保留一项，优先引用最明确的原句。
5. source_timestamp 必须逐字使用输入中的 HH:MM:SS。
6. source_quote 必须是输入中的短原文，不超过 160 个字符。
7. owner_name 或 deadline_iso 任一为空时，needs_confirmation 必须为 true。
8. deadline_iso 仅在日期可根据逐字稿和会议日期唯一确定时填写 ISO-8601；否则为 null，并在 uncertainties 说明。
9. deliverable 只能使用原文明确的形式和数量；原文未说“报告/指南/至少几条”时，不得自行补充，用中性描述并加入 uncertainties。
10. source_quote 必须引用直接包含行动动作、交付物或明确接受任务的发言；“行”“OK”“可以”等短确认不能单独作为任务证据。若一人布置任务、另一人只做短确认，优先引用布置任务的原句。
11. 发言人不等于负责人。只有原文明说“某人负责/某人去做”，或发言人明确说“我来/我会/我负责/我去做”时，owner_name 才能填写；“我觉得可以”“可以尝试”等建议不是认领。
12. 排除会议中已经当场执行的动作（例如正在敲屏幕、滚动文档、现场问 GPT）以及纯讨论、假设和选题建议；只有会议后仍需完成并产生结果、提交、跟进或实际执行的行动才抽取。
13. collaborator_names 只记录同一条任务证据中明确说出的合作关系，例如“甲和乙一起做/乙配合甲/甲与乙协作”；不得把参会、讨论、群内查看、投票或普通建议推断成合作。没有明确合作关系时必须为 []。
14. item_type 取 TASK 或 COMMITMENT，二选一：
    - TASK：会后要做出东西或完成动作，有可验收的交付内容。deliverable 按第 9 条填写。
    - COMMITMENT：本次会议当场约定的时间节点，没有可交付物本身。例如“争取后天再拍板”“月底三十号一大早出发”“周五之前给答复”。
      这类只有时间承诺、没有产出物，按 TASK 的交付物要求会被漏掉，但它同样需要进入会议纪要。
      COMMITMENT 的 deliverable 填对该节点的中性描述（如“完成方案拍板”“团队出发”），不得编造产出物。
15. COMMITMENT 必须同时满足三个条件，否则不要抽取：
    a. 原文含明确时间点或期限（后天、月底三十号、周五之前、下午三点）；
    b. 是本次会议当场做出的约定或决定，不是在陈述惯例、流程或既有安排；
    c. 承诺主体是本次与会方（我们/咱们/你/某人），不是泛指的第三方。
    以下一律不抽取：会议背景与前提（“今年公司要组织员工出去游玩”）、
    对惯例或流程的描述（“早上男方要去接亲”“北方一般六七点接女方”）、
    含“可能/大概/也许”的模糊意向（“近期可能要办一些活动”）。

返回对象必须是：
{
  "action_items": [
    {
      "title": "简短任务名",
      "item_type": "TASK 或 COMMITMENT",
      "deliverable": "可验收的交付内容",
      "owner_name": "逐字稿中明确的人名或 null",
      "deadline_text": "原文截止描述或 null",
      "deadline_iso": "ISO-8601 或 null",
      "source_timestamp": "HH:MM:SS",
      "source_quote": "原文证据",
      "confidence": 0.0,
      "needs_confirmation": true,
      "uncertainties": ["仍需确认的问题"],
      "collaborator_names": ["原文明确指定的合作者人名"]
    }
  ]
}"""


def _tools_system_prompt() -> str:
    """v1.4's rules, plus the obligation to verify a citation before writing it.

    Only the evidence discipline differs from `_system_prompt`. Every semantic
    rule about what counts as an action item is reused verbatim, so a
    difference in evaluation scores can be attributed to the tools rather than
    to a quietly reworded task definition.
    """

    return (
        _system_prompt()
        + """

本次你可以调用工具查阅逐字稿。请按以下顺序工作：

A. 先通读片段，列出你怀疑是行动项的地方。
B. 对每一条候选，在写出 source_quote 之前，必须先用 search_transcript 查到那句话。
   把工具返回的 text 和 timestamp 原样复制进 source_quote 和 source_timestamp，
   不要凭记忆重写、不要改写数量词或语气词。工具返回什么就写什么。
C. 无法判断一句话是布置任务还是随口讨论时，用 get_context 看它前后几句再决定。
D. 准备填写 owner_name 前，先用 list_speakers 确认这个人确实在本片段发过言；
   没查到就填 null，不要猜。
E. 如果 search_transcript 找不到你想引用的话（match_type 为 none，
   或只有低相似度的 fuzzy 结果），说明这条证据不存在，直接放弃该候选，
   不要退而求其次引用一句相近的话。

查证完成后，只输出最终 JSON，不要解释查证过程，不要输出 Markdown 代码块。"""
    )


class BailianExtractor:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
        timeout_seconds: int = 180,
        max_request_attempts: int = 3,
        retry_backoff_seconds: float = 1.0,
        max_chunk_characters: int = 3500,
        chunk_overlap_lines: int = 6,
        use_tools: bool = False,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
    ):
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not self.api_key:
            raise ExtractionError("DASHSCOPE_API_KEY is not configured")
        self.model = model or os.getenv("BAILIAN_MODEL", DEFAULT_BAILIAN_MODEL)
        self.endpoint = endpoint or os.getenv(
            "DASHSCOPE_CHAT_COMPLETIONS_URL", DEFAULT_BAILIAN_ENDPOINT
        )
        self.timeout_seconds = timeout_seconds
        self.max_request_attempts = max(1, int(max_request_attempts))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.max_chunk_characters = max(1000, int(max_chunk_characters))
        self.chunk_overlap_lines = max(0, int(chunk_overlap_lines))
        self.use_tools = bool(use_tools)
        self.max_tool_rounds = max(1, int(max_tool_rounds))

    @property
    def prompt_version(self) -> str:
        """Which prompt this instance actually ran, for the run record."""

        return (
            ACTION_ITEM_EXTRACTION_TOOLS_PROMPT_VERSION
            if self.use_tools
            else ACTION_ITEM_EXTRACTION_PROMPT_VERSION
        )

    def _request_body(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build the chat-completions body.

        Separated from the network call so the tools/response_format rule
        below can be asserted directly rather than through a mock.
        """

        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "enable_thinking": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
            # `response_format: json_object` is deliberately omitted while
            # tools are offered: a turn that calls a tool has no JSON body to
            # produce, and forcing the format makes providers reject the call
            # or emit an empty object instead of the tool call. The final
            # answer is parsed leniently instead.
        else:
            body["response_format"] = {"type": "json_object"}
        return body

    def _request_once(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body = self._request_body(messages, tools=tools)
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
            raise ExtractionError(
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
            raise ExtractionError(
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
            raise ExtractionError(
                f"Bailian request failed: {error}",
                error_code="NETWORK_TIMEOUT",
                stage="MODEL_REQUEST",
                retryable=True,
            ) from error
        return response_payload

    def _request(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        for attempt in range(1, self.max_request_attempts + 1):
            try:
                response_payload = self._request_once(messages, tools=tools)
                response_payload["_adapter_request_attempts"] = attempt
                return response_payload
            except ExtractionError as error:
                if not error.retryable or attempt >= self.max_request_attempts:
                    raise
                time.sleep(self.retry_backoff_seconds * attempt)
        raise AssertionError("request retry loop must return or raise")

    @staticmethod
    def _model_payload(response_payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        try:
            content = response_payload["choices"][0]["message"]["content"]
            model_payload = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise ExtractionError(
                "Bailian returned an invalid chat/JSON response",
                error_code="INVALID_JSON",
                stage="MODEL_RESPONSE",
                retryable=True,
            ) from error
        if not isinstance(content, str) or not isinstance(model_payload, dict):
            raise ExtractionError(
                "Bailian returned an invalid action-item payload",
                error_code="INVALID_SCHEMA",
                stage="MODEL_RESPONSE",
                retryable=True,
            )
        return content, model_payload

    @staticmethod
    def _lenient_model_payload(
        response_payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Parse a final answer produced without `response_format` enforcement.

        Only fenced-code stripping is tolerated. Anything looser would start
        inventing structure, and this payload still has to pass
        `validate_extraction` unchanged.
        """

        try:
            content = response_payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ExtractionError(
                "Bailian returned no assistant content",
                error_code="INVALID_JSON",
                stage="MODEL_RESPONSE",
                retryable=True,
            ) from error
        text = str(content or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            model_payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise ExtractionError(
                "Bailian returned a non-JSON final answer",
                error_code="INVALID_JSON",
                stage="MODEL_RESPONSE",
                retryable=True,
            ) from error
        if not isinstance(model_payload, dict):
            raise ExtractionError(
                "Bailian returned an invalid action-item payload",
                error_code="INVALID_SCHEMA",
                stage="MODEL_RESPONSE",
                retryable=True,
            )
        return text, model_payload

    def _run_tool_loop(
        self,
        messages: list[dict[str, Any]],
        tools: "TranscriptTools",
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        """Let the model look things up, then take its final JSON answer.

        Returns the final content, the parsed payload, every response payload
        seen (so token accounting stays whole), and a summary of what was
        looked up.
        """

        from .extraction_tools import TOOL_SCHEMAS

        conversation = list(messages)
        response_payloads: list[dict[str, Any]] = []
        rounds = 0

        for rounds in range(1, self.max_tool_rounds + 1):
            response_payload = self._request(conversation, tools=TOOL_SCHEMAS)
            response_payloads.append(response_payload)
            try:
                message = response_payload["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as error:
                raise ExtractionError(
                    "Bailian returned no message in a tool round",
                    error_code="INVALID_JSON",
                    stage="MODEL_RESPONSE",
                    retryable=True,
                ) from error

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                content, model_payload = self._lenient_model_payload(response_payload)
                return (
                    content,
                    model_payload,
                    response_payloads,
                    {
                        "tool_rounds": rounds,
                        "tool_calls": list(tools.call_log),
                        "hit_round_limit": False,
                    },
                )

            # The assistant turn must be echoed back verbatim, tool_calls and
            # all, or the provider cannot match the tool results to it.
            conversation.append(message)
            for call in tool_calls:
                function = call.get("function") or {}
                raw_arguments = function.get("arguments") or "{}"
                try:
                    arguments = json.loads(raw_arguments)
                except (json.JSONDecodeError, TypeError):
                    # A malformed argument string is the model's mistake to
                    # see and correct, not grounds to abandon the extraction.
                    arguments = {}
                result = tools.call(str(function.get("name", "")), arguments)
                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

        # Out of rounds while still looking things up. Ask once for the answer
        # with no tools offered, so a verbose run still yields candidates
        # instead of throwing away the work already done.
        conversation.append(
            {
                "role": "user",
                "content": "查证到此为止。现在只输出最终 JSON，不要再调用工具。",
            }
        )
        final_payload = self._request(conversation)
        response_payloads.append(final_payload)
        content, model_payload = self._lenient_model_payload(final_payload)
        return (
            content,
            model_payload,
            response_payloads,
            {
                "tool_rounds": rounds,
                "tool_calls": list(tools.call_log),
                "hit_round_limit": True,
            },
        )

    @staticmethod
    def _semantic_identity(item: ExtractedActionItem) -> tuple[Any, ...]:
        return (
            item.title,
            item.deliverable,
            item.owner_name,
            tuple(item.collaborator_names),
            item.deadline_text,
            item.deadline_iso,
            item.confidence,
            item.needs_confirmation,
        )

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

    def _transcript_chunks(self, transcript: str) -> list[str]:
        lines = [line.strip() for line in transcript.splitlines() if line.strip()]
        if not lines:
            return []
        chunks: list[str] = []
        start = 0
        while start < len(lines):
            end = start
            characters = 0
            while end < len(lines):
                line_characters = len(lines[end]) + 1
                if end > start and characters + line_characters > self.max_chunk_characters:
                    break
                characters += line_characters
                end += 1
            chunks.append("\n".join(lines[start:end]))
            if end >= len(lines):
                break
            start = max(start + 1, end - self.chunk_overlap_lines)
        return chunks

    def _extract_chunk(
        self,
        transcript_chunk: str,
        *,
        full_transcript: str,
        date_context: str,
        chunk_index: int,
        chunk_count: int,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    _tools_system_prompt() if self.use_tools else _system_prompt()
                ),
            },
            {
                "role": "user",
                "content": (
                    "请以 json 格式抽取以下会议逐字稿片段中的行动项。"
                    "只抽取本片段有直接证据的行动，不补全其他片段。"
                    f"会议日期：{date_context}；片段：{chunk_index}/{chunk_count}。"
                    f"\n\n逐字稿片段：\n{transcript_chunk}"
                ),
            },
        ]
        tool_summary: dict[str, Any] | None = None
        if self.use_tools:
            from .extraction_tools import TranscriptTools

            # Scoped to this chunk, matching the instruction above: tools that
            # could reach the whole transcript would let the model cite
            # evidence from a chunk it was told not to extract from.
            tools = TranscriptTools(transcript_chunk)
            content, model_payload, response_payloads, tool_summary = (
                self._run_tool_loop(messages, tools)
            )
        else:
            response_payloads = [self._request(messages)]
            content, model_payload = self._model_payload(response_payloads[0])
        model_payload, normalization_actions = normalize_extraction_payload(
            model_payload
        )
        items = validate_extraction(model_payload)
        original_items = list(items)
        evidence_model_repair_count = 0
        evidence_rejected_count = 0
        try:
            items, evidence_repairs = align_source_evidence(items, full_transcript)
        except ExtractionError as first_error:
            if first_error.error_code != "INVALID_CITATION":
                raise
            evidence_model_repair_count = 1
            repair_messages = [
                *messages,
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        "上一次 JSON 中至少一条 source_timestamp/source_quote 未通过"
                        "逐字稿原文校验。请只做证据修复：title、deliverable、owner_name、"
                        "collaborator_names、"
                        "deadline_text、deadline_iso、confidence、needs_confirmation 必须"
                        "原样保留；source_quote 必须逐字复制一条直接包含行动或明确接受"
                        "任务的发言，并使用该行时间戳。若某候选没有这样的单行证据，"
                        "从 action_items 删除它。不得新增候选。完整返回 JSON，不要解释。\n"
                        f"校验错误：{first_error}"
                    ),
                },
            ]
            repair_payload = self._request(repair_messages)
            response_payloads.append(repair_payload)
            _, repaired_model_payload = self._model_payload(repair_payload)
            repaired_model_payload, repair_normalization_actions = (
                normalize_extraction_payload(repaired_model_payload)
            )
            normalization_actions.extend(repair_normalization_actions)
            repaired_items = validate_extraction(repaired_model_payload)
            remaining_identities = [
                self._semantic_identity(item) for item in original_items
            ]
            for repaired_item in repaired_items:
                identity = self._semantic_identity(repaired_item)
                if identity not in remaining_identities:
                    raise ExtractionError(
                        "evidence repair changed or added an action-item candidate",
                        error_code="INVALID_SCHEMA",
                        stage="SOURCE_EVIDENCE_REPAIR",
                        retryable=False,
                    )
                remaining_identities.remove(identity)
            evidence_rejected_count = len(remaining_identities)
            try:
                items, evidence_repairs = align_source_evidence(
                    repaired_items, full_transcript
                )
            except ExtractionError as repair_error:
                raise ExtractionError(
                    "source evidence remained invalid after one repair: "
                    f"{repair_error}",
                    error_code="INVALID_CITATION",
                    stage="SOURCE_EVIDENCE_REPAIR",
                    retryable=False,
                ) from repair_error
        validate_source_evidence(items, full_transcript)
        items, collaboration_evidence_repairs = align_collaboration_evidence(
            items, full_transcript
        )
        items, owner_evidence_repairs = align_owner_evidence(
            items, full_transcript
        )
        return {
            "items": items,
            "response_payloads": response_payloads,
            "evidence_repairs": evidence_repairs,
            "evidence_model_repair_count": evidence_model_repair_count,
            "evidence_rejected_count": evidence_rejected_count,
            "collaboration_evidence_repairs": collaboration_evidence_repairs,
            "owner_evidence_repairs": owner_evidence_repairs,
            "normalization_actions": normalization_actions,
            "tool_summary": tool_summary,
        }

    def extract(
        self,
        transcript: str,
        *,
        meeting_date: str | None = None,
        checkpoint_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        if not transcript.strip():
            raise ExtractionError("transcript must not be empty")
        date_context = meeting_date or "未知；不得据此猜测具体日期"
        chunks = self._transcript_chunks(transcript)
        transcript_hash = stable_hash(transcript)
        checkpoint_root = Path(checkpoint_dir) if checkpoint_dir is not None else None
        if checkpoint_root is not None:
            checkpoint_root.mkdir(parents=True, exist_ok=True)
        all_items: list[ExtractedActionItem] = []
        response_payloads: list[dict[str, Any]] = []
        evidence_repairs = 0
        evidence_model_repair_count = 0
        evidence_rejected_count = 0
        collaboration_evidence_repairs = 0
        owner_evidence_repairs = 0
        normalization_actions: list[str] = []
        tool_summaries: list[dict[str, Any]] = []
        checkpoint_hit_count = 0
        for index, chunk in enumerate(chunks, start=1):
            checkpoint_key = stable_hash(
                {
                    "transcript_sha256": transcript_hash,
                    "meeting_date": meeting_date,
                    # Mode-aware, so a tools run never reuses a checkpoint
                    # written by the one-shot prompt or the other way round.
                    "prompt_version": self.prompt_version,
                    "model": self.model,
                    "chunk_index": index,
                    "chunk_count": len(chunks),
                    "chunk_sha256": stable_hash(chunk),
                }
            )
            checkpoint_path = (
                checkpoint_root / f"{index:03d}-{checkpoint_key}.json"
                if checkpoint_root is not None
                else None
            )
            chunk_result: dict[str, Any] | None = None
            if checkpoint_path is not None and checkpoint_path.exists():
                try:
                    cached = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                    if cached.get("checkpoint_key") == checkpoint_key:
                        cached_items = validate_extraction(
                            {"action_items": cached.get("action_items")}
                        )
                        validate_source_evidence(cached_items, transcript)
                        (
                            cached_items,
                            current_collaboration_repairs,
                        ) = align_collaboration_evidence(cached_items, transcript)
                        cached_items, current_owner_repairs = align_owner_evidence(
                            cached_items, transcript
                        )
                        chunk_result = {
                            "items": cached_items,
                            "response_payloads": list(
                                cached.get("response_payloads") or []
                            ),
                            "evidence_repairs": int(
                                cached.get("evidence_repairs", 0)
                            ),
                            "evidence_model_repair_count": int(
                                cached.get("evidence_model_repair_count", 0)
                            ),
                            "evidence_rejected_count": int(
                                cached.get("evidence_rejected_count", 0)
                            ),
                            "collaboration_evidence_repairs": int(
                                cached.get("collaboration_evidence_repairs", 0)
                            )
                            + current_collaboration_repairs,
                            "owner_evidence_repairs": int(
                                cached.get("owner_evidence_repairs", 0)
                            )
                            + current_owner_repairs,
                            "normalization_actions": list(
                                cached.get("normalization_actions") or []
                            ),
                            "tool_summary": cached.get("tool_summary"),
                        }
                        if not chunk_result["response_payloads"]:
                            chunk_result = None
                        else:
                            checkpoint_hit_count += 1
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    json.JSONDecodeError,
                    ExtractionError,
                ):
                    chunk_result = None
            if chunk_result is None:
                try:
                    chunk_result = self._extract_chunk(
                        chunk,
                        full_transcript=transcript,
                        date_context=date_context,
                        chunk_index=index,
                        chunk_count=len(chunks),
                    )
                except ExtractionError as error:
                    raise ExtractionError(
                        f"transcript chunk {index}/{len(chunks)} failed: {error}",
                        error_code=error.error_code,
                        stage=error.stage,
                        retryable=error.retryable,
                    ) from error
                if checkpoint_path is not None:
                    checkpoint_payload = {
                        "checkpoint_key": checkpoint_key,
                        "action_items": [
                            asdict(item) for item in chunk_result["items"]
                        ],
                        "response_payloads": chunk_result["response_payloads"],
                        "evidence_repairs": chunk_result["evidence_repairs"],
                        "evidence_model_repair_count": chunk_result[
                            "evidence_model_repair_count"
                        ],
                        "evidence_rejected_count": chunk_result[
                            "evidence_rejected_count"
                        ],
                        "collaboration_evidence_repairs": chunk_result[
                            "collaboration_evidence_repairs"
                        ],
                        "owner_evidence_repairs": chunk_result[
                            "owner_evidence_repairs"
                        ],
                        "normalization_actions": chunk_result[
                            "normalization_actions"
                        ],
                        "tool_summary": chunk_result.get("tool_summary"),
                    }
                    temporary_path = checkpoint_path.with_suffix(".tmp")
                    temporary_path.write_text(
                        json.dumps(
                            checkpoint_payload, ensure_ascii=False, indent=2
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    temporary_path.replace(checkpoint_path)
            all_items.extend(chunk_result["items"])
            response_payloads.extend(chunk_result["response_payloads"])
            evidence_repairs += int(chunk_result["evidence_repairs"])
            evidence_model_repair_count += int(
                chunk_result["evidence_model_repair_count"]
            )
            evidence_rejected_count += int(
                chunk_result["evidence_rejected_count"]
            )
            collaboration_evidence_repairs += int(
                chunk_result["collaboration_evidence_repairs"]
            )
            owner_evidence_repairs += int(
                chunk_result["owner_evidence_repairs"]
            )
            normalization_actions.extend(chunk_result["normalization_actions"])
            chunk_tool_summary = chunk_result.get("tool_summary")
            if chunk_tool_summary:
                tool_summaries.append({"chunk": index, **chunk_tool_summary})
        items = validate_extraction(
            {"action_items": [asdict(item) for item in all_items]}
        )
        validate_source_evidence(items, transcript)
        response_payload = response_payloads[-1]
        invocation_input_hash = stable_hash(
            {
                "transcript_sha256": transcript_hash,
                "meeting_date": meeting_date,
            }
        )
        return {
            "schema_version": "1.0",
            "provider": "bailian",
            "model": response_payload.get("model", self.model),
            "prompt_version": self.prompt_version,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "input_sha256": transcript_hash,
            "input_characters": len(transcript),
            "chunk_count": len(chunks),
            "checkpoint_hit_count": checkpoint_hit_count,
            "normalization_count": len(normalization_actions),
            "normalization_actions": normalization_actions,
            # Present only for a tools run. Every lookup is recorded so the
            # evidence in a candidate can be traced to the query that found
            # it, the same way source_quote traces to the transcript.
            "tool_use": (
                {
                    "prompt_version": self.prompt_version,
                    "total_rounds": sum(
                        int(summary.get("tool_rounds", 0))
                        for summary in tool_summaries
                    ),
                    "total_calls": sum(
                        len(summary.get("tool_calls") or [])
                        for summary in tool_summaries
                    ),
                    "failed_calls": sum(
                        1
                        for summary in tool_summaries
                        for call in (summary.get("tool_calls") or [])
                        if not call.get("ok")
                    ),
                    "chunks_hitting_round_limit": sum(
                        1
                        for summary in tool_summaries
                        if summary.get("hit_round_limit")
                    ),
                    "per_chunk": tool_summaries,
                }
                if tool_summaries
                else None
            ),
            "usage": self._usage(response_payloads),
            "model_call_count": len(response_payloads),
            "transport_attempt_count": sum(
                int(payload.get("_adapter_request_attempts", 1))
                for payload in response_payloads
            ),
            "invocation": {
                "capability_type": "MODEL",
                "principal": {
                    "actor_id": "SYSTEM",
                    "episode_id": None,
                    "roles": ["SYSTEM"],
                    "auth_source": "INTERNAL_SERVICE",
                },
                "purpose": "ACTION_ITEM_EXTRACTION",
                "field_allowlist": ["transcript", "meeting_date"],
                "entity_versions": {
                    "transcript_sha256": transcript_hash,
                },
                "prompt_version": self.prompt_version,
                "skill_version": None,
                "input_hash": invocation_input_hash,
                "output_status": "SUCCEEDED",
            },
            "action_items": [asdict(item) for item in items],
            "summary": {
                "total": len(items),
                "needs_confirmation": sum(item.needs_confirmation for item in items),
                "missing_owner": sum(item.owner_name is None for item in items),
                "missing_deadline": sum(item.deadline_iso is None for item in items),
                "evidence_repairs": evidence_repairs,
                "evidence_model_repair_count": evidence_model_repair_count,
                "evidence_rejected_count": evidence_rejected_count,
                "collaboration_evidence_repairs": collaboration_evidence_repairs,
                "owner_evidence_repairs": owner_evidence_repairs,
                "normalization_count": len(normalization_actions),
            },
        }


def extract_file(
    input_path: str | Path,
    output_path: str | Path,
    *,
    model: str | None = None,
    meeting_date: str | None = None,
    use_tools: bool = False,
) -> dict[str, Any]:
    source = Path(input_path)
    destination = Path(output_path)
    checkpoint_dir = destination.parent / ".checkpoints" / destination.stem
    transcript = read_text_file(source)
    result = BailianExtractor(model=model, use_tools=use_tools).extract(
        transcript,
        meeting_date=meeting_date,
        checkpoint_dir=checkpoint_dir,
    )
    result["source"] = {
        "filename": source.name,
        "path": str(source.resolve()),
        "uploaded_raw_with_user_authorization": True,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result
