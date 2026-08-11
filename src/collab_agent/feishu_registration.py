"""Let somebody bind themselves by messaging the bot, instead of by CLI.

Before this, joining meant telling a coordinator your open_id and having them
run `feishu-bind` from a terminal. Every unbound person stalls a task -- a
dispatch cannot leave PENDING_ASSIGNMENT until each assignee answers, and a
card cannot be sent to somebody the system cannot address -- so the slowest
step in bringing a team on was a person copying identifiers into a shell.

Now they message the bot with the meeting and their name and are bound on the
spot.

## What this deliberately does not decide

Binding is first-come-first-served: the first person to claim a name gets it,
and the coordinator is told afterwards with a button to undo it. That was a
product decision made with the trade-off stated -- until the coordinator
revokes it, whoever claimed a name can see that person's tasks. The revocation
is therefore a card the coordinator already has in hand rather than a command
they would have to go and look up, because a remedy nobody can reach is not a
remedy.

Two things it will not do, both because the roster is the authorization
boundary:

- It never adds a participant. A name that is not already on the meeting's
  confirmed roster is refused, not created. Self-service registration that
  could enrol people would make the boundary self-service too.
- It never moves somebody between meetings implicitly. A binding is per
  organisation-person, which is what makes registering once enough for every
  later meeting -- so claiming a name has to be a claim on a real roster entry
  in a real meeting, and the meeting has to be named.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


#: What the bot is called when someone @s it. Feishu strips the mention out of
#: the text and leaves an `@_user_1` placeholder, so both shapes are cleaned.
MENTION_PATTERN = re.compile(r"@_user_\d+|@[A-Za-z0-9_一-鿿]+")

#: Words people put in the message that are not part of a name or a meeting.
NOISE = ("注册", "报名", "绑定", "我是", "加入", "签到")


@dataclass(frozen=True)
class RegistrationRequest:
    """What somebody's message asked for, before anything is checked."""

    meeting_hint: str
    name_hint: str

    @property
    def complete(self) -> bool:
        return bool(self.meeting_hint and self.name_hint)


def parse_registration(text: str) -> RegistrationRequest:
    """Read "@Agent 快速会议 黄Z恒" as a meeting and a name.

    Split on whitespace and common Chinese separators rather than on a fixed
    grammar: people will type a comma, a full-width space, or nothing in
    particular, and refusing all but one shape would send them back to the CLI
    this exists to replace.

    The name is taken as the *last* token because meeting names are the ones
    with spaces in them ("媒体运营中心第一次例会"), while a person's name is
    one word. Getting this backwards is recoverable -- the name is checked
    against the roster, so a wrong split is refused rather than acted on.
    """

    cleaned = MENTION_PATTERN.sub(" ", text or "")
    for word in NOISE:
        cleaned = cleaned.replace(word, " ")
    parts = [
        part
        for part in re.split(r"[\s,，、;；:：]+", cleaned)
        if part and not part.startswith("http")
    ]
    if len(parts) < 2:
        return RegistrationRequest(meeting_hint="", name_hint="")
    return RegistrationRequest(
        meeting_hint=" ".join(parts[:-1]).strip(), name_hint=parts[-1].strip()
    )


def match_meeting(sources: list[Any], hint: str) -> Any | None:
    """Find the meeting somebody meant, or nothing.

    Nothing rather than a best guess: binding into the wrong meeting hands out
    the wrong tasks, and an ambiguous hint is exactly when a guess is most
    likely to be wrong. An exact slug wins; otherwise the hint has to appear in
    one title and only one.
    """

    hint = (hint or "").strip()
    if not hint:
        return None
    for source in sources:
        if source.slug == hint:
            return source
    folded = hint.casefold()
    matches = [
        source
        for source in sources
        if folded in (source.title or "").casefold()
        or folded in source.slug.casefold()
    ]
    return matches[0] if len(matches) == 1 else None


def roster_actor(database: Any, episode_id: str, name: str) -> dict[str, Any] | None:
    """The roster entry with this display name, or nothing.

    Reads `episode_participants` rather than `actors`: the meeting's roster is
    the authorization boundary, and the actors table holds everybody the
    organisation has ever had. Someone whose name exists but who was not in
    this meeting must not be bindable through it.
    """

    row = database.one(
        "SELECT a.actor_id, a.display_name FROM actors a "
        "JOIN episode_participants ep ON ep.actor_id = a.actor_id "
        "WHERE ep.episode_id = ? AND a.display_name = ?",
        (episode_id, (name or "").strip()),
    )
    return dict(row) if row else None


def existing_binding(database: Any, actor_id: str) -> dict[str, Any] | None:
    row = database.one(
        "SELECT actor_id, open_id, display_name FROM feishu_identity_bindings "
        "WHERE actor_id = ?",
        (actor_id,),
    )
    return dict(row) if row else None


def roster_names(database: Any, episode_id: str) -> list[str]:
    """Who a person could claim to be, for the message that says they missed."""

    return [
        row["display_name"]
        for row in database.all(
            "SELECT a.display_name FROM actors a "
            "JOIN episode_participants ep ON ep.actor_id = a.actor_id "
            "WHERE ep.episode_id = ? ORDER BY a.display_name",
            (episode_id,),
        )
    ]


#: What the coordinator's undo button carries. Named here so the card that
#: draws it and the handler that answers it cannot drift apart.
REGISTRATION_REVOKE_ACTION = "REGISTRATION_REVOKE"


def build_registration_card(
    *, title: str, body: str, template: str = "green"
) -> dict[str, Any]:
    from .feishu_cards import build_ack_card  # noqa: PLC0415 - avoid a cycle

    return build_ack_card(title=title, body=body, template=template)


def build_revoke_card(
    *, display_name: str, meeting_title: str, actor_id: str, open_id: str
) -> dict[str, Any]:
    """Tell the coordinator who just claimed a name, with the undo attached.

    The undo lives on this card rather than in a command somewhere, because the
    whole safety argument for first-come-first-served is that a wrong claim can
    be taken back quickly. A remedy that requires finding documentation is a
    remedy that will not be used inside the window where it still matters.
    """

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "有人自助注册了"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{display_name}** 认领了「{meeting_title}」里的这个名字，"
                        "现在开始能收到这个人的任务卡片。\n"
                        f"飞书 open_id：`{open_id}`\n\n"
                        "如果这不是本人，点下面撤销。撤销后这个名字回到未绑定，"
                        "谁都可以重新认领。"
                    ),
                },
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "撤销这次注册"},
                        "type": "danger",
                        "value": {
                            "action": REGISTRATION_REVOKE_ACTION,
                            "actor_id": actor_id,
                        },
                    }
                ],
            },
        ],
    }


class Registrar:
    """Turn a chat message into a binding, and tell the coordinator.

    Holds no domain service: binding is an identity fact, not a coordination
    command, and routing it through the service that enforces meeting rules
    would suggest a meeting had changed when none has.
    """

    def __init__(self, database: Any, im: Any, *, base_url: str = "") -> None:
        self.database = database
        self.im = im
        self.base_url = base_url.rstrip("/")

    # ---- reading -------------------------------------------------------

    def _sources(self) -> list[Any]:
        from . import episode_registry  # noqa: PLC0415 - avoid a cycle

        return episode_registry.list_sources(self.database)

    def _coordinator_open_id(self, source: Any) -> tuple[str, str] | None:
        row = self.database.one(
            "SELECT a.actor_id, b.open_id FROM actors a "
            "JOIN episode_participants ep ON ep.actor_id = a.actor_id "
            "JOIN feishu_identity_bindings b ON b.actor_id = a.actor_id "
            "WHERE ep.episode_id = ? AND ep.role IN ('COORDINATOR','AGGREGATOR') "
            "LIMIT 1",
            (source.episode_id,),
        )
        return (row["actor_id"], row["open_id"]) if row else None

    def _url(self, slug: str, page: str) -> str:
        return f"{self.base_url}/{slug}/{page}" if self.base_url else f"/{slug}/{page}"

    # ---- the flow ------------------------------------------------------

    def handle_message(
        self, *, open_id: str, text: str, sim_time: str
    ) -> dict[str, Any]:
        """Answer one inbound message; return the reply and anything to notify.

        Every refusal says what was missing and shows what a working message
        looks like. A bot that answers "格式不对" to somebody who cannot see the
        format is a bot they message exactly once.
        """

        sources = self._sources()
        already = self.database.one(
            "SELECT actor_id, display_name FROM feishu_identity_bindings "
            "WHERE open_id = ?",
            (open_id,),
        )
        request = parse_registration(text)
        example = self._example(sources)

        if not request.complete:
            if already:
                return self._reply(
                    "你已经注册过了",
                    f"你绑定的是 **{already['display_name']}**。"
                    "任务卡片会直接推到这里。\n\n"
                    f"要注册到别的名字，发：{example}",
                    template="blue",
                )
            return self._reply(
                "需要会议和名字",
                "把会议和你的名字一起发给我就能注册。\n\n"
                f"例如：{example}\n\n" + self._meeting_list(sources),
                template="orange",
            )

        source = match_meeting(sources, request.meeting_hint)
        if source is None:
            return self._reply(
                "没找到这场会",
                f"「{request.meeting_hint}」对不上任何一场会，或者对上了不止一场。\n\n"
                + self._meeting_list(sources),
                template="orange",
            )

        actor = roster_actor(self.database, source.episode_id, request.name_hint)
        if actor is None:
            names = roster_names(self.database, source.episode_id)
            return self._reply(
                "名单里没有这个名字",
                f"「{source.title or source.slug}」的参会名单里没有"
                f"「{request.name_hint}」。\n\n"
                "参会名单是权限边界，我不会往里加人——"
                "如果你确实参加了这场会，请让会议负责人把你加进名单。\n\n"
                "名单上有：" + "、".join(names),
                template="orange",
            )

        prior = existing_binding(self.database, actor["actor_id"])
        if prior and prior["open_id"] == open_id:
            return self._reply(
                "你已经在这场会里了",
                f"**{actor['display_name']}** 已经绑定到你了。\n\n"
                f"任务在这里看：{self._url(source.slug, 'tasks')}",
                template="blue",
            )
        if prior and prior["open_id"] != open_id:
            return self._reply(
                "这个名字已经有人了",
                f"「{actor['display_name']}」已经被别人认领了。"
                "如果那是搞错了，请会议负责人先撤销。",
                template="orange",
            )

        self.im.bind_actor(
            actor["actor_id"],
            open_id,
            display_name=actor["display_name"],
            sim_time=sim_time,
        )
        notify: list[tuple[str, dict[str, Any], str]] = []
        coordinator = self._coordinator_open_id(source)
        if coordinator and coordinator[1] != open_id:
            notify.append(
                (
                    coordinator[1],
                    build_revoke_card(
                        display_name=actor["display_name"],
                        meeting_title=source.title or source.slug,
                        actor_id=actor["actor_id"],
                        open_id=open_id,
                    ),
                    f"register:{actor['actor_id']}:{open_id}",
                )
            )
        return {
            "bound": True,
            "actor_id": actor["actor_id"],
            "slug": source.slug,
            "card": build_registration_card(
                title="注册好了",
                body=(
                    f"你是「{source.title or source.slug}」里的 "
                    f"**{actor['display_name']}**。\n"
                    "被派到你的任务会直接推到这里，接受和退回都能在卡片上点。\n\n"
                    f"任务清单：{self._url(source.slug, 'tasks')}"
                ),
            ),
            "notify": notify,
        }

    def revoke(self, *, actor_id: str, by_open_id: str) -> dict[str, Any]:
        """Undo a binding, from the coordinator's card.

        Checks that the caller coordinates a meeting this person is actually
        in. Without it the undo button would be a way for anyone holding a
        forwarded card to unbind anybody.
        """

        allowed = self.database.one(
            "SELECT 1 AS ok FROM episode_participants target "
            "JOIN episode_participants lead "
            "  ON lead.episode_id = target.episode_id "
            "JOIN feishu_identity_bindings b ON b.actor_id = lead.actor_id "
            "WHERE target.actor_id = ? AND b.open_id = ? "
            "  AND lead.role IN ('COORDINATOR','AGGREGATOR')",
            (actor_id, by_open_id),
        )
        if not allowed:
            raise PermissionError(
                "only a coordinator of this person's meeting may revoke it"
            )
        removed = self.im.unbind_actor(actor_id)
        return {"revoked": bool(removed), "actor_id": actor_id}

    # ---- wording -------------------------------------------------------

    def _reply(self, title: str, body: str, *, template: str) -> dict[str, Any]:
        return {
            "bound": False,
            "card": build_registration_card(
                title=title, body=body, template=template
            ),
            "notify": [],
        }

    def _example(self, sources: list[Any]) -> str:
        name = sources[0].title.split(" · ")[0] if sources else "会议名"
        return f"`@机器人 {name} 你的名字`"

    def _meeting_list(self, sources: list[Any]) -> str:
        if not sources:
            return "现在还没有会议。"
        rows = "\n".join(f"· {source.title or source.slug}" for source in sources[:8])
        return f"现在有这些会：\n{rows}"
