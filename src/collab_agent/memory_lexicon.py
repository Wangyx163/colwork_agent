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

Group A -- 常用工作模式 -- is proposed by the system from audit facts.  Group B
-- 我的手动设置 -- describes what someone wants from others, which the system
cannot observe without guessing at intent, so it is self-declared only.  Group
B also gives a newcomer a manual on day one, before any history exists.

Every value names a mode before describing it ("草稿模式：...").  The name is
what a colleague remembers and repeats; the sentence after the colon is what
makes it checkable against the reversibility rule.
"""

from __future__ import annotations

from typing import Any


MEMORY_LEXICON_VERSION = "collaboration-memory-lexicon.v3"

SYSTEM_OBSERVED = "SYSTEM_OBSERVED"
SELF_DECLARED = "SELF_DECLARED"

#: What each group is called on screen, and why it is a group. Two different
#: things are being asked for, and mixing them would either invite the system
#: to guess at intent or hide that it has been watching.
GROUP_TITLES = {
    SYSTEM_OBSERVED: "常用工作模式",
    SELF_DECLARED: "我的手动设置",
}
GROUP_BLURBS = {
    SYSTEM_OBSERVED: "这些习惯通常会在真实协作里慢慢显现。系统可以帮你发现，但最终解释权归你。",
    SELF_DECLARED: "这些更像真正的个人设置。系统不会替你猜，因为不同选择之间没有优劣，只有哪一种更适合你。",
}

MEMORY_TOPICS: dict[str, dict[str, Any]] = {
    # ---- Group A -- 常用工作模式: the system proposes these from audit facts.
    #
    # Each value names a mode before describing it. The mode name is what
    # somebody remembers and repeats; the sentence after the colon is what
    # makes it checkable. The reversibility rule still binds: swap any two
    # modes inside a topic and neither may read as the worse one.
    "DELIVERY_RHYTHM": {
        "origin": SYSTEM_OBSERVED,
        "title": "交付模式",
        "prompt": "东西做到什么程度，你会先拿出来？",
        "values": (
            (
                "DRAFT_FIRST",
                "草稿模式：有一个能讨论的版本，我就愿意先拿出来",
                "不用等我做到很完整，粗版就可以一起看",
            ),
            (
                "ITERATIVE_REVIEW",
                "分段模式：我喜欢一段一段推进，边做边校准",
                "阶段版本就可以找我对，不必等到最后",
            ),
            (
                "COMPLETE_FIRST",
                "成品模式：我更习惯想完整后，再一次性交付",
                "给我一段连续工作时间，中途频繁检查反而容易打断",
            ),
        ),
    },
    "PROGRESS_SIGNAL": {
        "origin": SYSTEM_OBSERVED,
        "title": "进度播报模式",
        "prompt": "做的过程中，你一般怎么同步进度？",
        "values": (
            (
                "QUICK_SIGNAL",
                "常亮模式：有进展我通常会顺手同步一点",
                "看我的日常消息，基本就能知道做到哪了",
            ),
            (
                "MILESTONE_ONLY",
                "节点模式：我主要在关键节点同步",
                "平时安静通常代表正常推进，到节点我会主动出现",
            ),
            (
                "RISK_FIRST",
                "异常优先模式：风险、阻塞和变化我会优先说",
                "普通进展未必都会报，但有问题我不会藏到最后",
            ),
        ),
    },
    "HELP_SEEKING": {
        "origin": SYSTEM_OBSERVED,
        "title": "卡住后的处理模式",
        "prompt": "卡住的时候，你通常怎么处理？",
        "values": (
            (
                "ASK_WHEN_BLOCKED",
                "求援模式：确认卡住后，我会尽早找人一起解",
                "我来找你的时候，通常是真的需要一起处理",
            ),
            (
                "TRY_FIRST",
                "排障模式：我习惯先自己拆一轮、试几种办法",
                "我暂时没出声，可能只是在自己定位问题",
            ),
            (
                "OPTIONS_INCLUDED",
                "带方案模式：求助前，我通常会先整理几个候选解法",
                "不一定需要陪我从零想，帮我判断几个方案就很有价值",
            ),
        ),
    },
    "SCHEDULE_HABIT": {
        "origin": SYSTEM_OBSERVED,
        "title": "截止时间模式",
        "prompt": "给自己定时间时，你更接近哪一种？",
        "values": (
            (
                "COMMIT_EARLY",
                "提前量模式：我习惯把自己的目标时间放在正式截止之前",
                "我的东西通常会比约定时间更早回来",
            ),
            (
                "COMMIT_TO_ASK",
                "准点模式：我习惯直接按约定时间推进",
                "不用额外替我预留提前量，按说好的时间配合即可",
            ),
            (
                "RENEGOTIATE_EARLY",
                "预警模式：一旦判断时间可能有变化，我会尽早重新对齐",
                "我主动改时间，通常意味着已经识别到了实际风险",
            ),
        ),
    },
    # ---- Group B -- 我的手动设置: self-declared only.
    #
    # Nothing in an audit trail reveals which of these somebody wants, so the
    # system never proposes one; guessing here would be inventing intent.
    "BRIEF_DETAIL": {
        "origin": SELF_DECLARED,
        "title": "信息输入方式",
        "prompt": "派活给你时，你希望先拿到什么？",
        "values": (
            (
                "CONCLUSION_AND_BOUNDS",
                "结论输入：先告诉我目标、结论和边界就够了",
                "把“要什么、不要什么”讲清楚，中间过程我可以自己补",
            ),
            (
                "CHECKLIST",
                "清单输入：步骤、条目和待办最容易让我进入状态",
                "能拆成清单的信息，我通常接得最快",
            ),
            (
                "CONTEXT_FIRST",
                "全景输入：我更希望先理解背景和来龙去脉",
                "先让我知道为什么做，后面的判断会更准确",
            ),
        ),
    },
    "FEEDBACK_STYLE": {
        "origin": SELF_DECLARED,
        "title": "反馈接收方式",
        "prompt": "对你的成果提意见时，你希望怎么说？",
        "values": (
            (
                "DIRECT",
                "直球反馈：问题在哪里，可以直接告诉我",
                "不需要太多铺垫，明确指出问题就好",
            ),
            (
                # "我希望" added: a value must be first person, and the guard
                # that enforces it is the same one that keeps these readable
                # as self-description rather than as instructions about a person.
                "GOAL_FIRST",
                "目标反馈：我希望先把最终想达到的效果讲清楚",
                "先告诉我终点，我更容易判断现在差在哪里",
            ),
            (
                "CONCRETE_SUGGESTION",
                "示例反馈：具体告诉我希望它变成什么样最有效",
                "与其只说“不太对”，不如给一个修改方向或参考",
            ),
        ),
    },
    "SYNC_PREFERENCE": {
        "origin": SELF_DECLARED,
        "title": "沟通通道",
        "prompt": "需要跟你确认事情时，走哪种方式更好？",
        "values": (
            (
                "ASYNC_TEXT",
                "留言模式：我更习惯集中处理异步文字消息",
                "直接留言即可，不需要等我即时回复",
            ),
            (
                # "我" added, same reason as GOAL_FIRST above.
                "SYNC_AT_MILESTONE",
                "节点通话模式：平时异步，关键时刻我更喜欢快速聊一下",
                "真正重要的问题，十分钟同步往往比来回打字更快",
            ),
            (
                "INTERRUPTIBLE",
                "随时呼叫模式：我对即时打断的接受度比较高",
                "有事情可以直接来找我，不用特意攒到一起",
            ),
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
        "groups": [
            {
                "origin": origin,
                "title": GROUP_TITLES[origin],
                "blurb": GROUP_BLURBS[origin],
            }
            for origin in (SYSTEM_OBSERVED, SELF_DECLARED)
        ],
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


def projected_value(topic: str, value: dict[str, Any]) -> dict[str, str] | None:
    """The words to show for a stored entry, derived from its code.

    The code is the fact -- it is what the subject actually confirmed. The
    sentence beside it is presentation, so it is re-derived from the current
    lexicon rather than read back from the row: entries written before a
    wording change would otherwise be shown in words nobody uses any more, and
    entries written before the lexicon existed carry no colleague-facing hint
    at all.

    Returns None when there is nothing safe to show -- no code, or a code that
    is no longer in the lexicon. Those are not repaired by guessing which
    modern value an old sentence meant; putting words in somebody's mouth is
    the one failure this whole feature exists to avoid.
    """

    try:
        return memory_value(topic, str(value.get("code") or ""))
    except ValueError:
        return None


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
        # Upper bound as well as lower. A questionnaire people actually finish
        # is a handful of choices per question, and a topic that grew to six
        # would quietly turn one question into a memory test.
        if len(values) > 4:
            raise ValueError(f"{topic} offers too many modes to choose between")
        if "：" not in str(spec["title"]) and not all(
            "：" in label for _, label, _ in values
        ):
            raise ValueError(f"{topic} values must name a mode before describing it")
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
