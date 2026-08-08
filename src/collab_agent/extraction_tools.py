"""Read-only transcript tools the extraction model may call.

Why these exist
---------------
Extraction used to be one shot: the model read a chunk and emitted candidates
whose `source_quote` and `source_timestamp` were then checked against the real
transcript by `align_source_evidence`. A candidate whose citation did not hold
up triggered a second "repair" request, and anything still unsupported was
dropped. That is recovery after the fact -- the model had already committed to
a quote it reconstructed from memory.

These tools invert the order. The model can look a line up *before* citing it,
and what the tool hands back is exactly the normalised utterance text and
timestamp that the validator will later compare against. Copying a returned
value is therefore verbatim by construction, where recalling one is not.

Boundaries
----------
Every tool is read-only and scoped to the same chunk the model was already
shown. They add no data the model did not already have; they only make it
addressable. Nothing here decides a task, an owner, a state or a schedule --
the output is still a candidate that must pass `validate_extraction`, still
needs human confirmation, and the roster still comes from outside.

Speakers are derived from the transcript rather than accepted as a parameter,
so extraction stays standalone and a "who said this" answer can never be
grounded in something the transcript does not support.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from .extraction import TRANSCRIPT_LINE_PATTERN


MAX_SEARCH_RESULTS = 8
MAX_CONTEXT_LINES = 5


def _normalize(text: str) -> str:
    """Collapse whitespace the way the evidence validator does.

    Matching on anything looser here would let the model copy a string that
    then fails validation, which is the exact failure these tools remove.
    """

    return re.sub(r"\s+", " ", str(text or "")).strip()


@dataclass(frozen=True)
class TranscriptLine:
    index: int
    timestamp: str
    speaker: str
    text: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "line": self.index,
            "timestamp": self.timestamp,
            "speaker": self.speaker,
            # Named `text` and normalised, because this is the string the
            # model is expected to copy into source_quote.
            "text": self.text,
        }


def parse_transcript(transcript: str) -> list[TranscriptLine]:
    """Parse "speaker(HH:MM:SS): text" lines, skipping anything unparseable.

    Unparseable lines are dropped rather than guessed at: a line with no
    timestamp cannot support a citation, so offering it would only invite one
    that fails validation.
    """

    lines: list[TranscriptLine] = []
    for raw in str(transcript or "").splitlines():
        match = TRANSCRIPT_LINE_PATTERN.match(raw.strip())
        if not match:
            continue
        lines.append(
            TranscriptLine(
                index=len(lines) + 1,
                timestamp=match.group("timestamp"),
                speaker=_normalize(match.group("speaker")),
                text=_normalize(match.group("text")),
            )
        )
    return lines


class TranscriptTools:
    """Dispatch table for the model-callable transcript lookups."""

    def __init__(self, transcript: str) -> None:
        self.lines = parse_transcript(transcript)
        self.call_log: list[dict[str, Any]] = []

    # ---- individual tools ---------------------------------------------

    def search_transcript(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Find lines containing `query`, falling back to fuzzy similarity.

        The exact pass runs first and alone when it hits, so a model that
        already knows the wording is never handed near-misses that might
        tempt it into citing the wrong line.
        """

        needle = _normalize(query)
        limit = max(1, min(int(limit or 5), MAX_SEARCH_RESULTS))
        if not needle:
            return {"query": query, "match_type": "none", "results": []}

        exact = [line for line in self.lines if needle in line.text]
        if exact:
            return {
                "query": needle,
                "match_type": "exact",
                "results": [line.as_payload() for line in exact[:limit]],
            }

        scored: list[tuple[float, TranscriptLine]] = []
        for line in self.lines:
            ratio = SequenceMatcher(None, needle, line.text).ratio()
            if ratio >= 0.4:
                scored.append((ratio, line))
        scored.sort(key=lambda pair: (-pair[0], pair[1].index))
        return {
            "query": needle,
            "match_type": "fuzzy" if scored else "none",
            # The score rides along so the model can tell a near-quote from a
            # coincidence rather than treating every hit as confirmation.
            "results": [
                {**line.as_payload(), "similarity": round(ratio, 3)}
                for ratio, line in scored[:limit]
            ],
        }

    def get_context(
        self, timestamp: str, before: int = 2, after: int = 2
    ) -> dict[str, Any]:
        """Return the lines around a timestamp.

        Assignment often spans turns -- one person asks, another accepts -- so
        deciding whether a line is really an action item usually needs its
        neighbours.
        """

        wanted = _normalize(timestamp)
        anchors = [line for line in self.lines if line.timestamp == wanted]
        if not anchors:
            return {
                "timestamp": wanted,
                "found": False,
                "results": [],
                "note": "该时间戳不在本片段中；请改用 search_transcript",
            }
        before = max(0, min(int(before or 0), MAX_CONTEXT_LINES))
        after = max(0, min(int(after or 0), MAX_CONTEXT_LINES))
        start = max(1, anchors[0].index - before)
        end = min(len(self.lines), anchors[-1].index + after)
        window = [line for line in self.lines if start <= line.index <= end]
        return {
            "timestamp": wanted,
            "found": True,
            "results": [
                {**line.as_payload(), "is_anchor": line.timestamp == wanted}
                for line in window
            ],
        }

    def list_speakers(self) -> dict[str, Any]:
        """Who actually speaks in this chunk, with how much they say.

        This is the only defensible source for `owner_name` at extraction
        time: the meeting roster is not available here, and a name the
        transcript never shows cannot be evidenced.
        """

        counts: dict[str, int] = {}
        for line in self.lines:
            counts[line.speaker] = counts.get(line.speaker, 0) + 1
        return {
            "speakers": [
                {"name": name, "utterances": count}
                for name, count in sorted(
                    counts.items(), key=lambda pair: (-pair[1], pair[0])
                )
            ],
            "note": "只有这些人在本片段发言；不要把未出现的人写成 owner_name",
        }

    # ---- dispatch ------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one tool call. Unknown names and bad arguments return an error
        payload rather than raising, so one malformed call costs a round trip
        instead of failing the whole extraction.
        """

        handlers = {
            "search_transcript": lambda a: self.search_transcript(
                str(a.get("query", "")), a.get("limit", 5)
            ),
            "get_context": lambda a: self.get_context(
                str(a.get("timestamp", "")), a.get("before", 2), a.get("after", 2)
            ),
            "list_speakers": lambda _a: self.list_speakers(),
        }
        handler = handlers.get(name)
        if handler is None:
            result: dict[str, Any] = {
                "error": f"未知工具 {name}；可用：{sorted(handlers)}"
            }
        else:
            try:
                result = handler(arguments or {})
            except (TypeError, ValueError) as error:
                result = {"error": f"参数无效：{error}"}
        self.call_log.append(
            {"tool": name, "arguments": arguments, "ok": "error" not in result}
        )
        return result


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_transcript",
            "description": (
                "在本片段逐字稿中按内容查找发言。返回逐字原文和时间戳，"
                "可直接用作 source_quote 与 source_timestamp。"
                "写入任何引用之前都应先用它确认原文存在。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要查找的原话片段或关键词",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"返回条数，1-{MAX_SEARCH_RESULTS}，默认 5",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_context",
            "description": (
                "返回某个时间戳前后的发言，用于判断一句话是不是真的在派活或认领任务。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "timestamp": {
                        "type": "string",
                        "description": "HH:MM:SS 格式的时间戳",
                    },
                    "before": {"type": "integer", "description": "向前几行，默认 2"},
                    "after": {"type": "integer", "description": "向后几行，默认 2"},
                },
                "required": ["timestamp"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_speakers",
            "description": (
                "列出本片段的发言人。填写 owner_name 前应先确认此人确实发过言。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
