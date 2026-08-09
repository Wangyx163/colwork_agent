"""Turn a domain refusal into something the person who hit it can act on.

The domain raises in English, and it should: those strings are read in logs,
in tests, and by whoever is debugging, and they name the rule precisely. What
they are not is an answer to the person who just clicked a button. "contribution
action must be INCLUDE, REQUEST_REVISION, or PROMOTE" tells a reader the shape
of an enum they have never seen and nothing about what went wrong or what to
do instead.

So the translation lives here, at the boundary where a refusal becomes a
response, and it is a translation of *intent* rather than of words: each entry
says what the system will not do and what the reader can do next.

Anything unmapped falls through with its English intact. A message nobody has
written a Chinese version of is a message that has not been thought about, and
hiding it behind "操作失败" would lose the only clue the reader had.
"""

from __future__ import annotations


#: Keyed on the English the domain raises. Kept as exact strings rather than
#: patterns: a fuzzy match that catches the wrong refusal is worse than no
#: translation, because it tells somebody confidently about a rule they did
#: not break.
USER_MESSAGES: dict[str, str] = {
    # ---- 派发与复核 ----
    "only a draft or returned task definition may be edited": (
        "这个任务已经派出去了，定义不能再改。"
        "要改名称或说明，让任务负责人在自己的卡片上改；要换人或改期限，只能先退回。"
    ),
    "only a draft or returned task may be dispatched": (
        "这个任务已经派发过了。它现在等的是被派到的人接受，不是再派一次。"
    ),
    "team required time must be later than the current time": (
        "团队要求交付的时间要晚于现在。"
    ),
    "only unpublished, unclaimed proposals may be merged": (
        "只有还没派发、也没人认领的候选才能合并。"
    ),
    "title, deliverable, and work requirements are required": (
        "任务名称和交付物都要填。"
    ),
    "priority must be P0, P1, or P2": "优先级只能是 P0、P1 或 P2。",
    # ---- 改任务说明 ----
    "only the task owner may amend its description": (
        "只有这个任务的负责人能改它的说明。"
        "会议负责人的权限在派发时就用完了。"
    ),
    "only a task being worked on may have its description amended": (
        "任务还没被人接下来，或者已经结束了，这时候改说明没有意义。"
    ),
    "title and description are required": "名称和说明都要填。",
    # ---- 求助 ----
    "resolution summary is required": (
        "要写一句怎么解决的，发起人会看到这句。"
        "如果成果已经提交，可以直接点标记解决，系统会把成果转过去。"
    ),
    "only the requested attendee may acknowledge help": (
        "这条求助不是发给你的，只有被求助的人能接手。"
    ),
    "only the requester may cancel help": "只有发起求助的人能撤销它。",
    "only the requester or helper may resolve help": (
        "只有发起人或接手的人能标记解决。"
    ),
    "assistance request is already closed": "这条求助已经关掉了。",
    "assistance request is already acknowledged": "已经有人接手了。",
    # ---- 协作者贡献 ----
    "contribution action must be INCLUDE, REQUEST_REVISION, or PROMOTE": (
        "只能选：采纳进我的版本、请对方再改、或直接作为最终候选。"
    ),
    "revision feedback is required": "请对方再改时，要写清楚改什么。",
    "the selected version is not a collaborator contribution": (
        "这个版本是任务负责人自己交的，不走协作者材料这条路。"
    ),
    # ---- 提交与验收 ----
    "an artifact version is already pending acceptance": (
        "上一版还等着验收，先等负责人处理完再交新的。"
    ),
    "task result processing must finish before human acceptance": (
        "系统还在读这一版的正文和附件，读完才能验收。稍等一下再试。"
    ),
    # ---- 收集 → 投票 → 定稿 ----
    "at least one collection action item is required": "至少要选一个上游收集任务。",
    "decision action item cannot depend on itself": "汇总任务不能把自己当上游。",
    "at least one voter is required": "至少要指定一个投票人。",
    "selection_count must be between 1 and 8": "最终保留的条数要在 1 到 8 之间。",
    "meeting source span is required": (
        "复合结构要指明会议上是在哪说的，不能凭空建。"
    ),
    "a revocation reason is required": "撤销要写原因。",
    "only the final task owner may open the ballot": (
        "只有定稿负责人能开启投票。"
    ),
    "all collection tasks must be accepted before opening the ballot": (
        "上游的收集任务要全部验收完，才能开始打分。"
    ),
    "the opened ballot is locked": "投票已经开了，候选不能再改。",
    "ballot must contain between 2 and 100 options": (
        "候选至少要有 2 条。"
    ),
    # ---- 协作说明书 ----
    "this topic is proposed from observed facts and must be confirmed, not declared": (
        "这一题是系统从你的协作记录里观察出来的，等它提出来你再确认，不用自己填。"
    ),
    "memory value is not allowed for this topic": (
        "这个选项不在这一题的选项表里。"
    ),
    "private collaboration memory belongs to another actor": (
        "这是别人的协作说明书，只有本人能改。"
    ),
    "only a draft memory may be rejected": (
        "只有系统提出、你还没确认的那种才能拒绝；已经确认的用「撤下」。"
    ),
    "only a confirmed memory may be withdrawn": (
        "这条还没确认，或者已经撤下过了。"
    ),
    "unsupported memory action": "不支持的操作。",
    # ---- 通用 ----
    "message_id is required": (
        "这次请求缺少幂等标识，页面没有正确发出。刷新一下再试。"
    ),
    "only the meeting coordinator may manage action items": (
        "这一步只有会议负责人能做。"
    ),
    "only a meeting participant may perform this action": (
        "你不在这场会议的参会名单里。"
    ),
}


def user_message(error: BaseException) -> str:
    """The sentence to show, falling back to whatever the domain said."""

    raw = str(error)
    return USER_MESSAGES.get(raw, raw)
