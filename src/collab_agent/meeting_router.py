"""Send a card click to the meeting it belongs to.

One Feishu app has one long connection, so every click for every meeting
arrives through the same callback. The bridge that turns a click into a domain
command holds exactly one service, and it must: the rules about who may
respond are answered against one episode's roster, and a bridge that guessed
would be answering the wrong room's door.

So the routing happens above the bridge rather than inside it. A click already
carries enough to say which meeting it is for -- the effect resolves to a task,
and a task belongs to exactly one episode -- which means the answer is looked
up, never inferred.

A click that resolves to no meeting is refused. The alternative, falling back
to "the first meeting", would take a stale card from a deleted meeting and
apply it to a live one.
"""

from __future__ import annotations

from typing import Any

from .feishu_commands import UnresolvableEffect, resolve_action_item


class UnknownMeeting(LookupError):
    """The click resolved to an episode this process is not serving."""


class MeetingRouter:
    """Dispatch card clicks across several meetings sharing one connection."""

    def __init__(self, bridges: dict[str, Any], database: Any) -> None:
        self.bridges = dict(bridges)
        self.database = database

    def episode_for(self, record: dict[str, Any]) -> str | None:
        """Which meeting a click is about, from the task it resolves to.

        Assignment cards carry the task id; Outbox-born effects carry only an
        EffectId and resolve through the Outbox. Both end at a task, and a task
        names its episode, so neither path has to trust the card for the answer
        that decides which roster applies.
        """

        payload = record.get("payload")
        if isinstance(payload, dict):
            carried = payload.get("action_item_id")
        else:
            carried = None
        effect_id = record.get("effect_id")
        action_item_id = None
        if effect_id:
            try:
                action_item_id = resolve_action_item(
                    self.database, str(effect_id), from_card=carried
                )
            except (UnresolvableEffect, LookupError):
                action_item_id = carried
        else:
            action_item_id = carried
        if not action_item_id:
            return None
        row = self.database.one(
            "SELECT episode_id FROM action_items WHERE action_item_id = ?",
            (action_item_id,),
        )
        return row["episode_id"] if row else None

    def handle(self, record: dict[str, Any]) -> Any:
        episode_id = self.episode_for(record)
        if episode_id is None and len(self.bridges) == 1:
            # One meeting is the whole world here, so a click that could not be
            # resolved is still unambiguous. With two it would be a guess, and
            # the branch below refuses instead.
            return next(iter(self.bridges.values())).handle(record)
        bridge = self.bridges.get(episode_id or "")
        if bridge is None:
            raise UnknownMeeting(
                f"card click resolved to episode {episode_id!r}, which this "
                "process is not serving"
            )
        return bridge.handle(record)
