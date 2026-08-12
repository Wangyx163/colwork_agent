"""The stage machine for a compound task.

A compound task is a shape a meeting decides on -- "we each write some
questions, one of us merges them, everybody scores, and that person writes it
up" -- and it is its own kind of thing rather than a set of ordinary tasks
wired together with dependencies. The earlier attempt did the latter, and it
showed: there was no moment at which somebody was asked to *fill in their
options*, because filling in options is not what an ordinary task does. People
submitted free text and a model guessed the option boundaries out of it.

So the stages are explicit, and they alternate between everybody and one
person. The alternation is the whole structure:

    投票型  个人填项 -> 负责人汇总 -> 个人投票 -> 负责人定稿
    提交型  个人填信息 -> 负责人处理 -> (直接提交)

`ROLE_AT` says whose turn it is, which is the only question either surface
asks: a participant needs to know whether there is something for them to do,
and the owner needs to know whether the ball is theirs yet.

Kept free of storage and of the service on purpose -- it is a small enough
piece of logic to test on its own, and every rule below was easier to get right
without a database in the way.
"""

from __future__ import annotations

from enum import StrEnum


class CompoundKind(StrEnum):
    """What the meeting asked for.

    The distinction is not cosmetic: a vote needs a round in which everybody
    reacts to what was merged, and a submission does not. Getting it wrong
    means either asking people to score nothing or never asking them at all,
    which is why it is decided once, up front, and confirmed by a person.
    """

    VOTE = "VOTE"
    SUBMIT = "SUBMIT"


class Stage(StrEnum):
    COLLECTING = "COLLECTING"
    MERGING = "MERGING"
    VOTING = "VOTING"
    FINALIZING = "FINALIZING"
    DONE = "DONE"
    #: Withdrawn before anybody was asked to do anything. Separate from DONE so
    #: a report can tell "the group decided" from "the group never started".
    REVOKED = "REVOKED"


#: The order of play, per kind. A submit-type task has no voting round and no
#: separate finalising round: the owner edits what came in and hands it on.
STAGES: dict[CompoundKind, tuple[Stage, ...]] = {
    CompoundKind.VOTE: (
        Stage.COLLECTING,
        Stage.MERGING,
        Stage.VOTING,
        Stage.FINALIZING,
        Stage.DONE,
    ),
    CompoundKind.SUBMIT: (Stage.COLLECTING, Stage.MERGING, Stage.DONE),
}

#: Whose turn each stage belongs to. Every stage has exactly one answer; a
#: stage that both sides could act in would leave neither knowing whether to.
ROLE_AT: dict[Stage, str] = {
    Stage.COLLECTING: "EVERYONE",
    Stage.MERGING: "OWNER",
    Stage.VOTING: "EVERYONE",
    Stage.FINALIZING: "OWNER",
    Stage.DONE: "NOBODY",
    Stage.REVOKED: "NOBODY",
}

STAGE_TITLES: dict[Stage, str] = {
    Stage.COLLECTING: "填写",
    Stage.MERGING: "汇总",
    Stage.VOTING: "投票",
    Stage.FINALIZING: "定稿",
    Stage.DONE: "已完成",
    Stage.REVOKED: "已撤销",
}


class CompoundTaskError(ValueError):
    """A stage rule was broken."""


def stages_for(kind: str) -> tuple[Stage, ...]:
    try:
        return STAGES[CompoundKind(kind)]
    except ValueError as error:
        raise CompoundTaskError("unknown compound task kind") from error


def next_stage(kind: str, stage: str) -> Stage:
    """The stage that follows, refusing to run off either end.

    Advancing past DONE is a bug in the caller rather than a state to sit in,
    and saying so here keeps every caller from having to check.
    """

    order = stages_for(kind)
    current = Stage(stage)
    if current in (Stage.DONE, Stage.REVOKED):
        raise CompoundTaskError("this compound task is already finished")
    try:
        index = order.index(current)
    except ValueError as error:
        raise CompoundTaskError(
            "this stage does not belong to this kind of compound task"
        ) from error
    return order[index + 1]


def role_at(stage: str) -> str:
    return ROLE_AT[Stage(stage)]


def may_act(stage: str, *, actor_id: str, owner_actor_id: str, members: list[str]) -> bool:
    """Whether this person has something to do right now.

    Membership is checked even in an EVERYONE stage: the roster is the
    authorisation boundary everywhere else in this system, and a compound task
    is not the place to start letting bystanders in.
    """

    role = role_at(stage)
    if role == "OWNER":
        return actor_id == owner_actor_id
    if role == "EVERYONE":
        return actor_id in members
    return False


def is_complete(
    stage: str,
    *,
    submitted_actor_ids: set[str],
    members: list[str],
    skipped_actor_ids: set[str] | None = None,
) -> bool:
    """Whether an EVERYONE stage can move on.

    Everybody, not a quorum: a shortlist assembled from four of five people's
    questions is missing one, and nothing downstream can tell that it is
    missing. The wait is the feature.

    Which is also why the only way past it is an explicit act by the owner,
    named here as a skip. Waiting forever is not a policy -- somebody is on
    leave, somebody left the team -- but the difference between "everyone
    answered" and "four answered and one was passed over" has to survive into
    the record, or the shortlist quietly becomes untraceable again. A skip is
    therefore a decision with an author and a reason, not a timeout.
    """

    if role_at(stage) != "EVERYONE":
        return False
    settled = set(submitted_actor_ids) | set(skipped_actor_ids or ())
    return set(members).issubset(settled)
