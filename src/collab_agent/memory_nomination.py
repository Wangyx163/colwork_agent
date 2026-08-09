"""Nominate collaboration-memory labels from one task's audit facts.

The shape is borrowed rather than invented. LangMem's user-profile pattern
constrains extraction with a schema so the model can only fill declared fields,
and keeps one instance per person instead of an ever-growing collection; mem0
hands the "how does this new fact merge with what exists" question to the model
by showing it the current entries. Both are adapted here, with one deliberate
difference: mem0 lets the model decide ADD/UPDATE/DELETE, and this does not.
The model may only *nominate*. Confirming, replacing and rejecting stay with
the person the label is about, which is the constitutive rule of the feature.

So the schema is the lexicon -- twelve labels, three per topic -- and the model
picks from it or picks nothing. Everything it returns is then checked against
that list and against the evidence it was shown, before any of it reaches a
row. A nomination advances no state: it lands as PRIVATE_DRAFT like every
other candidate.

The counting rules stay as the deterministic floor. They can only ever reach
three of the twelve labels, which is why nomination exists, but they need no
provider, no network and no tokens -- so a demo runs on them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .memory_lexicon import MEMORY_TOPICS, SYSTEM_OBSERVED, memory_value


MEMORY_NOMINATION_PROMPT_VERSION = "memory-nomination.v1"


class NominationError(ValueError):
    """The model returned something that is not a nomination."""


@dataclass(frozen=True)
class Nomination:
    topic: str
    code: str
    #: Refs the model was shown and chose to cite. Never invented: anything
    #: outside the evidence it was given is dropped.
    evidence_refs: tuple[str, ...]


def observable_labels() -> list[dict[str, str]]:
    """The twelve labels a nomination may choose from, as flat rows.

    Flat on purpose: nesting topics under values made the model return topic
    objects with several codes, and the extra structure bought nothing -- a
    label is a pair.
    """

    return [
        {
            "topic": topic,
            "code": code,
            "means": label,
        }
        for topic, spec in MEMORY_TOPICS.items()
        if spec["origin"] == SYSTEM_OBSERVED
        for code, label, _ in spec["values"]
    ]


def nomination_messages(
    report: dict[str, Any], existing: list[dict[str, str]]
) -> list[dict[str, str]]:
    """What the model is shown: the labels, the facts, and what already holds.

    `existing` is the mem0 borrowing -- without it the model re-nominates a
    label the person already confirmed or already rejected, and the reader gets
    the same question a second time.
    """

    return [
        {
            "role": "system",
            "content": (
                "你在给一次已验收的任务打协作标注。\n"
                "只能从给定的标签表里选，不能自己造标签，不能改写标签含义。\n"
                "每条标注必须引用给定证据里的 id；引用不存在的 id 会被丢弃。\n"
                "没有把握就少选或不选——空结果是允许且常见的。\n"
                "禁止对人做评价，只描述这次任务里观察到的协作方式。\n"
                '返回 JSON：{"labels":[{"topic":"","code":"","evidence_refs":[""]}]}'
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "allowed_labels": observable_labels(),
                    "already_settled": existing,
                    "task_report": report,
                },
                ensure_ascii=False,
            ),
        },
    ]


class MemoryNominator:
    """Model-backed nomination, with every label checked against the lexicon."""

    def __init__(self, complete: Callable[[list[dict[str, str]]], str]) -> None:
        self.complete = complete

    def nominate(
        self,
        report: dict[str, Any],
        *,
        evidence_refs: set[str],
        existing: list[dict[str, str]] | None = None,
    ) -> list[Nomination]:
        raw = self.complete(nomination_messages(report, existing or []))
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise NominationError(
                f"nomination model returned non-JSON: {raw!r}"
            ) from error
        if not isinstance(payload, dict):
            raise NominationError("nomination model returned a non-object payload")

        settled = {
            (str(item.get("topic")), str(item.get("code")))
            for item in (existing or [])
        }
        nominations: list[Nomination] = []
        seen: set[str] = set()
        for entry in payload.get("labels") or []:
            if not isinstance(entry, dict):
                continue
            topic = str(entry.get("topic") or "").strip().upper()
            code = str(entry.get("code") or "").strip().upper()
            spec = MEMORY_TOPICS.get(topic)
            # A topic the person declares about themselves is not observable:
            # the model would be guessing at intent, which is the one thing
            # the lexicon's Group A/B split exists to prevent.
            if not spec or spec["origin"] != SYSTEM_OBSERVED:
                continue
            try:
                memory_value(topic, code)
            except ValueError:
                # Not a label -- a hallucination, or a code from another topic.
                continue
            # One label per topic. Two codes under the same topic are mutually
            # exclusive readings of the same behaviour; keeping both would ask
            # the person to confirm a contradiction.
            if topic in seen or (topic, code) in settled:
                continue
            cited = tuple(
                ref
                for ref in dict.fromkeys(
                    str(value) for value in (entry.get("evidence_refs") or [])
                )
                if ref in evidence_refs
            )
            # No surviving citation means nothing in this task supports the
            # label. Unsupported labels are exactly what this feature must not
            # put in front of somebody as a claim about them.
            if not cited:
                continue
            seen.add(topic)
            nominations.append(
                Nomination(topic=topic, code=code, evidence_refs=cited)
            )
        return nominations
