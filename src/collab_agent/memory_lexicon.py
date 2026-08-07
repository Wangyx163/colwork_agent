"""Controlled vocabulary for the collaboration user manual.

Why a fixed lexicon rather than free text
-----------------------------------------
This is a controlled vocabulary in the library-science sense (MeSH, Dublin
Core).  Free text cannot be aggregated across meetings, cannot define what
"replacing the same topic" means, leaks project- and person-specific detail
when shown to a colleague, and cannot be versioned so that an old entry stays
interpretable.  Every one of those four is a requirement here.

Why these words and not others
------------------------------
Two rules constrain what may enter the lexicon:

1. Observation, not evaluation (Rosenberg, Nonviolent Communication).  An entry
   describes a working preference, never a person.  The operational test is
   **reversibility**: swap any two values within a topic; if the result reads as
   worse rather than merely different, the pair is illegal.  "Reply quickly" vs
   "reply slowly" fails.  "Conclusion first" vs "context first" passes.
2. Self-disclosure, not feedback (Johari window).  Confirming an entry moves it
   from the hidden quadrant to the open one, and only its subject may make that
   move.  The system may notice a candidate; it may never assert one.

Group A entries are proposed by the system from audit facts.  Group B entries
describe what someone wants from others, which the system cannot observe
without guessing at intent, so they are self-declared only.  Group B also gives
a newcomer a manual on day one, before any history exists.
"""

from __future__ import annotations

from typing import Any


MEMORY_LEXICON_VERSION = "collaboration-memory-lexicon.v2"

SYSTEM_OBSERVED = "SYSTEM_OBSERVED"
SELF_DECLARED = "SELF_DECLARED"

MEMORY_TOPICS: dict[str, dict[str, Any]] = {
    # ---- Group A: how I work (system proposes from audit facts) ----
    "DELIVERY_RHYTHM": {
        "origin": SYSTEM_OBSERVED,
        "title": "交付节奏",
        "prompt": "关于交付节奏，你更接近哪一种？",
        "values": (
            ("DRAFT_FIRST", "我习惯先给可讨论的草稿", "早点把粗版发给我，一起改比等成品快"),
            ("ITERATIVE_REVIEW", "我习惯分版本逐步交付", "可以按阶段来看，不用等全部做完"),
            ("COMPLETE_FIRST", "我习惯想清楚后一次交付", "给我一点完整时间，中途追进度帮助不大"),
        ),
    },
    "PROGRESS_SIGNAL": {
        "origin": SYSTEM_OBSERVED,
        "title": "进展同步",
        "prompt": "关于同步进展，你更接近哪一种？",
        "values": (
            ("QUICK_SIGNAL", "我习惯用简短信号勤同步", "看我的状态就知道进度，不用单独问"),
            ("MILESTONE_ONLY", "我习惯只在关键节点同步", "没消息通常就是正常推进"),
            ("RISK_FIRST", "我习惯风险和阻塞优先同步", "我说话时通常是有事需要处理"),
        ),
    },
    "HELP_SEEKING": {
        "origin": SYSTEM_OBSERVED,
        "title": "求助习惯",
        "prompt": "关于求助，你更接近哪一种？",
        "values": (
            ("ASK_WHEN_BLOCKED", "我卡住会尽早开口", "我提出来时通常是真的需要帮忙"),
            ("TRY_FIRST", "我习惯先自己试一轮再问", "我沉默一段时间是在试，不是没动"),
            ("OPTIONS_INCLUDED", "我求助时会带上候选方案", "可以直接帮我选，不用从头想"),
        ),
    },
    # ---- Group B: what I need from others (self-declared only) ----
    "BRIEF_DETAIL": {
        "origin": SELF_DECLARED,
        "title": "我需要多少背景",
        "prompt": "派活给你时，你希望拿到多少信息？",
        "values": (
            ("CONCLUSION_AND_BOUNDS", "给我结论和边界就够", "说清楚要什么、不要什么即可"),
            ("CHECKLIST", "给我清单和拆分更好用", "拆成条目我上手更快"),
            ("CONTEXT_FIRST", "我需要先了解完整背景", "先讲来龙去脉，我才知道怎么取舍"),
        ),
    },
    "FEEDBACK_STYLE": {
        "origin": SELF_DECLARED,
        "title": "我希望怎么收到反馈",
        "prompt": "对你的成果提意见时，你希望怎么说？",
        "values": (
            ("DIRECT", "我希望直接指出问题", "不用铺垫，说问题就行"),
            ("GOAL_FIRST", "我希望先对齐目标再谈问题", "先说清楚要达成什么，我好判断改哪里"),
            ("CONCRETE_SUGGESTION", "我希望给出具体修改建议", "说明期望的样子比说哪里没做好更有用"),
        ),
    },
    "SYNC_PREFERENCE": {
        "origin": SELF_DECLARED,
        "title": "我偏好的沟通方式",
        "prompt": "需要和你确认事情时，你更偏好哪种方式？",
        "values": (
            ("ASYNC_TEXT", "我以异步文字沟通为主", "留言就好，我会集中回"),
            ("SYNC_AT_MILESTONE", "我希望关键节点当面或语音", "重要节点约一次比来回打字快"),
            ("INTERRUPTIBLE", "我随时可以被打断", "有事直接找我，不用攒着"),
        ),
    },
}

# Preserved so entries confirmed under v1 remain readable after the upgrade.
LEGACY_TOPIC_ALIASES = {
    "DELIVERY_STYLE": "DELIVERY_RHYTHM",
    "UPDATE_STYLE": "PROGRESS_SIGNAL",
    "ASSISTANCE_STYLE": "HELP_SEEKING",
    "INFORMATION_STYLE": "BRIEF_DETAIL",
}
LEGACY_CODE_ALIASES = {
    ("HELP_SEEKING", "CONTEXT_INCLUDED"): "OPTIONS_INCLUDED",
    ("BRIEF_DETAIL", "CONCLUSION_FIRST"): "CONCLUSION_AND_BOUNDS",
}

MEMORY_LEXICON: dict[str, tuple[tuple[str, str], ...]] = {
    topic: tuple((code, label) for code, label, _ in spec["values"])
    for topic, spec in MEMORY_TOPICS.items()
}


def canonical_topic(topic: str) -> str:
    normalized = str(topic or "").strip().upper()
    return LEGACY_TOPIC_ALIASES.get(normalized, normalized)


def topic_origin(topic: str) -> str:
    spec = MEMORY_TOPICS.get(canonical_topic(topic))
    if not spec:
        raise ValueError("unknown collaboration memory topic")
    return str(spec["origin"])


def memory_lexicon_payload() -> dict[str, Any]:
    return {
        "version": MEMORY_LEXICON_VERSION,
        "topics": [
            {
                "topic": topic,
                "origin": spec["origin"],
                "title": spec["title"],
                "prompt": spec["prompt"],
                "values": [
                    {
                        "code": code,
                        "label": label,
                        "collaborator_hint": hint,
                    }
                    for code, label, hint in spec["values"]
                ],
            }
            for topic, spec in MEMORY_TOPICS.items()
        ],
    }


def memory_value(topic: str, code: str) -> dict[str, str]:
    normalized_topic = canonical_topic(topic)
    normalized_code = str(code or "").strip().upper()
    normalized_code = LEGACY_CODE_ALIASES.get(
        (normalized_topic, normalized_code), normalized_code
    )
    spec = MEMORY_TOPICS.get(normalized_topic)
    if spec:
        for candidate_code, label, hint in spec["values"]:
            if candidate_code == normalized_code:
                return {
                    "code": candidate_code,
                    "statement": label,
                    "collaborator_hint": hint,
                    "lexicon_version": MEMORY_LEXICON_VERSION,
                }
    raise ValueError("memory value is not allowed for this topic")


def memory_value_is_current(topic: str, value: dict[str, Any]) -> bool:
    try:
        expected = memory_value(topic, str(value.get("code") or ""))
    except ValueError:
        return False
    return (
        value.get("statement") == expected["statement"]
        and value.get("lexicon_version") == MEMORY_LEXICON_VERSION
    )


def assert_lexicon_is_reversible() -> None:
    """Guard the constitutive rule: no value may read as worse than a sibling.

    This cannot be decided automatically, so the check enforces the structural
    preconditions that make a human review meaningful -- every topic offers a
    real alternative, and no value is a negation or a comparative of another.
    """

    banned = ("不", "少", "慢", "差", "弱", "缺", "懒", "拖")
    for topic, spec in MEMORY_TOPICS.items():
        values = spec["values"]
        if len(values) < 2:
            raise ValueError(f"{topic} must offer an alternative to be reversible")
        codes = [code for code, _, _ in values]
        if len(set(codes)) != len(codes):
            raise ValueError(f"{topic} has duplicate codes")
        for _, label, hint in values:
            if "我" not in label:
                raise ValueError(f"{topic} value must be first person: {label}")
            if any(token in label for token in banned):
                raise ValueError(
                    f"{topic} value reads as a deficit rather than a preference: {label}"
                )
            if not hint:
                raise ValueError(f"{topic} value must tell a colleague what to do")
