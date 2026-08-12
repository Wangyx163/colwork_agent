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
    'only a draft or returned task definition may be edited': (
        '只有草稿或已退回的任务定义可以被编辑'
    ),
    'only a draft or returned task may be dispatched': (
        '只有草稿或已退回的任务可以被派发'
    ),
    'team required time must be later than the current time': (
        '团队要求时间必须晚于当前时间'
    ),
    'only unpublished, unclaimed proposals may be merged': (
        '只有未发布、未认领的提案可以被合并'
    ),
    'title, deliverable, and work requirements are required': (
        '标题、交付物和工作要求为必填项'
    ),
    'priority must be P0, P1, or P2': (
        '优先级必须为 P0、P1 或 P2'
    ),
    'only the task owner may amend its description': (
        '只有任务负责人可以修改任务描述'
    ),
    'only a task being worked on may have its description amended': (
        '只有正在处理中的任务可以修改任务描述'
    ),
    'title and deliverable are required': (
        '标题和交付物为必填项'
    ),
    'say where in the meeting this came from': (
        '请写明这条任务在会上是从哪儿来的'
    ),
    'title and description are required': (
        '标题和描述为必填项'
    ),
    'resolution summary is required': (
        '解决摘要为必填项'
    ),
    'only the requested attendee may acknowledge help': (
        '只有被请求的参与者可以确认帮助'
    ),
    'only the requester may cancel help': (
        '只有请求方可以取消帮助'
    ),
    'only the requester or helper may resolve help': (
        '只有请求方或帮助方可以解决帮助请求'
    ),
    'assistance request is already closed': (
        '协助请求已关闭'
    ),
    'assistance request is already acknowledged': (
        '协助请求已确认'
    ),
    'contribution action must be INCLUDE, REQUEST_REVISION, or PROMOTE': (
        '贡献操作必须为 INCLUDE、REQUEST_REVISION 或 PROMOTE'
    ),
    'revision feedback is required': (
        '修订反馈为必填项'
    ),
    'the selected version is not a collaborator contribution': (
        '所选版本不是协作者贡献'
    ),
    'an artifact version is already pending acceptance': (
        '已有一个制品版本正在等待接受'
    ),
    'task result processing must finish before human acceptance': (
        '任务结果处理必须在人工作出接受之前完成'
    ),
    'at least one collection action item is required': (
        '至少需要一个收集类行动项'
    ),
    'decision action item cannot depend on itself': (
        '决策行动项不能依赖其自身'
    ),
    'at least one voter is required': (
        '至少需要一名投票者'
    ),
    'selection_count must be between 1 and 8': (
        'selection_count 必须介于 1 和 8 之间'
    ),
    'meeting source span is required': (
        '会议来源范围为必填项'
    ),
    'a revocation reason is required': (
        '撤销原因是必填项'
    ),
    'only the final task owner may open the ballot': (
        '只有最终任务负责人可以开启投票'
    ),
    'all collection tasks must be accepted before opening the ballot': (
        '开启投票前，所有收集任务都必须已被接受'
    ),
    'the opened ballot is locked': (
        '已开启的投票将被锁定'
    ),
    'ballot must contain between 2 and 100 options': (
        '投票必须包含 2 到 100 个选项'
    ),
    'this topic is proposed from observed facts and must be confirmed, not declared': (
        '该主题是基于已观察事实提出的，必须经过确认，不能直接声明'
    ),
    'memory value is not allowed for this topic': (
        '该主题不允许设置记忆值'
    ),
    'private collaboration memory belongs to another actor': (
        '私有协作记忆属于其他参与者'
    ),
    'only a draft memory may be rejected': (
        '只有草稿状态的记忆可以被拒绝'
    ),
    'only a confirmed memory may be withdrawn': (
        '只有已确认的记忆可以被撤回'
    ),
    'unsupported memory action': (
        '不支持的记忆操作'
    ),
    'only the compound task owner may skip somebody': (
        '只有复合任务的负责人可以跳过某个人'
    ),
    'this stage is not waiting on anybody': (
        '这个环节没有在等谁'
    ),
    'that person is not on this compound task': (
        '这个人不在这个复合任务里'
    ),
    'the owner cannot skip themselves': (
        '负责人不能跳过自己'
    ),
    'a skip needs a reason': (
        '跳过要写原因'
    ),
    'that person already answered': (
        '这个人已经交了'
    ),
    'message_id is required': (
        'message_id 为必填项'
    ),
    'only the meeting coordinator may manage action items': (
        '只有会议协调人可以管理行动项'
    ),
    'only the meeting coordinator may perform this action': (
        '只有会议负责人可以执行此操作'
    ),
    'only the task executor may perform this action': (
        '只有任务负责人可以执行此操作'
    ),
    'only the task executor or an active collaborator may perform this action': (
        '只有任务负责人或在场的协作者可以执行此操作'
    ),
    'only a meeting participant may perform this action': (
        '只有会议参与者可以执行此操作'
    ),
}

def user_message(error: BaseException) -> str:
    """The sentence to show, falling back to whatever the domain said."""

    raw = str(error)
    return USER_MESSAGES.get(raw, raw)
