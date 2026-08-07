from __future__ import annotations

import json
from typing import Any


# What a card button asks the domain to do. Anything outside this map is
# rejected rather than guessed at, so a stale card from an older deploy cannot
# drive a command that no longer means what it used to.
DECISIONS = {
    "accept": "ACCEPT",
    "return": "RETURN_FOR_REVISION",
}

DEFAULT_RETURN_REASONS = (
    "任务定义不清楚，需要补充验收标准",
    "时间安排不可行，需要调整工期",
    "不该由我负责，请重新指派",
)


class UnknownCardAction(ValueError):
    """The card asked for something the bridge does not implement."""


class UnresolvableEffect(LookupError):
    """A click arrived for an effect with no action item behind it."""


def resolve_action_item(
    database: Any, effect_id: str, *, from_card: str | None = None
) -> str:
    """Work out which task a card click is about.

    Assignment cards carry the task id directly, because this project's
    dispatch is pull-based and leaves no Outbox row to resolve through. Effects
    that *do* originate in the Outbox carry only an EffectId, and for those the
    Outbox row stays the authority.

    Trusting the card is safe: `respond_to_assignment` re-checks the task
    status and the caller's assignment, so a card carrying a stale or forged id
    is refused by the domain rather than acted on.
    """

    if from_card:
        return str(from_card)
    row = database.one(
        "SELECT action_item_id FROM outbox_entries WHERE effect_id = ?", (effect_id,)
    )
    if not row:
        raise UnresolvableEffect(f"no outbox entry for effect {effect_id}")
    action_item_id = dict(row)["action_item_id"]
    if not action_item_id:
        raise UnresolvableEffect(f"effect {effect_id} is not about a task")
    return str(action_item_id)


def _payload_of(record: dict[str, Any]) -> dict[str, Any]:
    raw = record.get("raw_value") or "{}"
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def return_reason_from(record: dict[str, Any]) -> str:
    """Pull the revision reason a returning member picked.

    The domain refuses a return without a reason, so a bare button click can
    never satisfy it; the card offers preset reasons and the chosen one rides
    back in the action payload.
    """

    return str(_payload_of(record).get("reason") or "").strip()


class AssignmentBridge:
    """Turns a recorded card click into a real coordination command.

    Deliberately thin: it resolves identity and target, then hands over to
    `CoordinationService`, which keeps every rule about who may respond and
    when. The Feishu layer never decides domain state.
    """

    def __init__(self, service: Any, *, log: Any = None) -> None:
        self.service = service
        self.log = log

    def handle(self, record: dict[str, Any]) -> dict[str, Any]:
        action_name = str(record.get("action_name", ""))
        if action_name not in DECISIONS:
            raise UnknownCardAction(f"card action {action_name!r} is not supported")
        actor_id = record.get("actor_id")
        if not actor_id:
            raise PermissionError(
                f"open_id {record.get('operator_open_id')} is not bound to a "
                "participant; a click from an unknown person is never a decision"
            )
        effect_id = record.get("effect_id")
        if not effect_id:
            raise UnresolvableEffect("card click carried no effect_id")

        payload = _payload_of(record)
        action_item_id = resolve_action_item(
            self.service.db,
            str(effect_id),
            from_card=payload.get("action_item_id"),
        )
        decision = DECISIONS[action_name]
        reason = return_reason_from(record)
        if decision == "RETURN_FOR_REVISION" and not reason:
            raise ValueError("a revision reason is required to return a dispatch")

        result = self.service.respond_to_assignment(
            action_item_id,
            actor_id=str(actor_id),
            decision=decision,
            response_message=reason,
            # Feishu's own event id becomes the inbound receipt key, so a
            # redelivered click replays the stored result instead of deciding
            # a second time.
            message_id=str(record["event_key"]),
        )
        if self.log is not None:
            self.log(
                f"[feishu] {actor_id} {decision} {action_item_id} "
                f"-> {result.get('status')}"
            )
        return result
