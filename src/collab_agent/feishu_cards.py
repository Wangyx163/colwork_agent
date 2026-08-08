from __future__ import annotations

from typing import Any

from .models import ASSIGNMENT_RETURN_REASONS, canonical_json


HEADER_TEMPLATES = {
    "CONFIRMATION_REQUEST": "blue",
    "ASSIGNMENT_REQUEST": "blue",
    "ASSISTANCE_REQUEST": "orange",
    "REWORK_RESPONSE": "orange",
    "ACCEPTANCE_NOTICE": "green",
    "FINAL_RELEASE": "green",
}

HEADER_TITLES = {
    "CONFIRMATION_REQUEST": "任务待确认",
    "ASSIGNMENT_REQUEST": "任务派发",
    "ASSISTANCE_REQUEST": "求助请求",
    "REWORK_RESPONSE": "退回重做",
    "ACCEPTANCE_NOTICE": "验收通知",
    "FINAL_RELEASE": "终稿发布",
}

# Effect types whose card carries a decision the recipient must make. Anything
# else renders as a read-only notice, so a new effect type never grows buttons
# the service has no command for.
DECISION_EFFECT_TYPES = {
    "CONFIRMATION_REQUEST",
    "ASSIGNMENT_REQUEST",
}

# The domain refuses a return without a reason, and a bare button click cannot
# carry one. Presets keep the card a single tap while still satisfying the rule.
# Shared with the web workbench so both surfaces offer the identical list.
RETURN_REASONS = ASSIGNMENT_RETURN_REASONS


def build_effect_card(command: dict[str, Any]) -> dict[str, Any]:
    """Render one outbox effect as a Feishu interactive card.

    The button `value` carries `effect_id` rather than a row id: it is the same
    stable identifier the Outbox already uses, so a click that arrives twice is
    recognised as the same intent without a second lookup table.
    """

    effect_type = str(command.get("effect_type", ""))
    effect_id = str(command.get("effect_id", ""))
    content = str(command.get("content", ""))
    action_item_id = command.get("action_item_id")
    template = HEADER_TEMPLATES.get(effect_type, "grey")
    title = HEADER_TITLES.get(effect_type, effect_type or "通知")

    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": content}}
    ]

    if effect_type in DECISION_EFFECT_TYPES:
        base_value = {"effect_id": effect_id, "effect_type": effect_type}
        if action_item_id:
            base_value["action_item_id"] = str(action_item_id)
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "接受"},
                        "type": "primary",
                        "value": {**base_value, "action": "accept"},
                    },
                    # A picker rather than a button: choosing the reason and
                    # returning the dispatch must be one action, or a member
                    # can return without ever supplying a reason.
                    {
                        "tag": "select_static",
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "退回重改（选择原因）",
                        },
                        "value": {**base_value, "action": "return"},
                        "options": [
                            {
                                "text": {"tag": "plain_text", "content": reason},
                                "value": reason,
                            }
                            for reason in RETURN_REASONS
                        ],
                    },
                ],
            }
        )

    elements.append(
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": f"EffectId {effect_id}"}
            ],
        }
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


DECISION_OUTCOME_CARDS = {
    "ACCEPT": ("green", "已接受", "你已接受这项派发。"),
    "RETURN_FOR_REVISION": ("orange", "已退回", "你已退回这项派发，本轮其他人的回应同时失效。"),
}


def build_decided_card(
    original_content: str,
    *,
    decision: str,
    reason: str = "",
    footer: str = "",
) -> dict[str, Any]:
    """The same card with the controls replaced by what was decided.

    Buttons are removed rather than disabled: a disabled control still invites
    a click, and a card that looks actionable after the decision is the single
    most confusing state in this flow.
    """

    template, title, body = DECISION_OUTCOME_CARDS.get(
        decision, ("grey", "已处理", "这项派发已经处理。")
    )
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": original_content}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**{title}**：{body}"}},
    ]
    if reason:
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": f"原因：{reason}"}}
        )
    if footer:
        elements.append(
            {"tag": "note", "elements": [{"tag": "plain_text", "content": footer}]}
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def build_pending_card(original_content: str) -> dict[str, Any]:
    """Shown between the click and the worker finishing.

    Deliberately not "已接受": at this point the click is only recorded. The
    domain still has to check that this person is on the current dispatch and
    that the round has not been superseded, and either can refuse.
    """

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "处理中"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": original_content}},
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "已收到你的选择，正在处理…"},
            },
        ],
    }


def build_ack_card(*, title: str, body: str, template: str = "green") -> dict[str, Any]:
    """A minimal read-only card, used to acknowledge an inbound message."""

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
    }


NOTIFICATION_TEMPLATES = {
    "ASSIGNMENT_RESPONSE_REQUIRED": "blue",
    "ASSISTANCE_REQUESTED": "orange",
    "VOTE_REQUIRED": "purple",
    "RESULT_PENDING_REVIEW": "turquoise",
    "REVIEW_DECIDED": "green",
}

# Decisions whose reason can be picked from a fixed list. Anything else that
# needs typing has to happen on the web: a card picker cannot collect free text
# in the same tap, and a decision recorded without its required reason would be
# refused by the domain anyway.
DECISION_REASON_PRESETS = {
    "ASSIGNMENT_RETURN": RETURN_REASONS,
}


def build_notification_card(command: dict[str, Any]) -> dict[str, Any]:
    """Render a card from the `notification` contract the domain emits.

    The domain already states what a notification says and which decisions it
    offers -- `_notify` builds title, summary, fields and decisions in one
    place. Rendering from that rather than switching on effect_type means a new
    notification type reaches Feishu correctly without this module learning
    about it, and a decision can never appear on a card that the service has no
    command for.
    """

    notification = command.get("notification") or {}
    effect_id = str(command.get("effect_id", ""))
    kind = str(notification.get("kind") or command.get("effect_type") or "")
    title = str(notification.get("title") or "")
    summary = str(notification.get("summary") or "")

    elements: list[dict[str, Any]] = []
    if summary:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": summary}})

    fields = notification.get("fields") or []
    if fields:
        elements.append(
            {
                "tag": "div",
                "fields": [
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f'**{field.get("label")}**\n{field.get("value")}',
                        },
                    }
                    for field in fields
                ],
            }
        )

    base_value = {
        "effect_id": effect_id,
        "effect_type": kind,
        "action_item_id": str(notification.get("action_item_id") or ""),
        "subject_id": str(notification.get("subject_id") or ""),
    }

    actions: list[dict[str, Any]] = []
    web_only: list[str] = []
    for decision in notification.get("decisions") or []:
        name = str(decision.get("name") or "")
        label = str(decision.get("label") or name)
        if decision.get("score_options"):
            # Scoring several options is a form, not a tap.
            web_only.append(label)
            continue
        presets = DECISION_REASON_PRESETS.get(name)
        if decision.get("requires_reason") and not presets:
            web_only.append(label)
            continue
        if presets:
            actions.append(
                {
                    "tag": "select_static",
                    "placeholder": {"tag": "plain_text", "content": f"{label}（选择原因）"},
                    "value": {**base_value, "action": name},
                    "options": [
                        {"text": {"tag": "plain_text", "content": reason}, "value": reason}
                        for reason in presets
                    ],
                }
            )
        else:
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": label},
                    "type": "primary",
                    "value": {**base_value, "action": name},
                }
            )

    if actions:
        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": actions})

    footer = f"EffectId {effect_id}"
    if web_only:
        footer = f'{"、".join(web_only)} 需要在网页工作台完成 · {footer}'
    elements.append(
        {"tag": "note", "elements": [{"tag": "plain_text", "content": footer}]}
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": NOTIFICATION_TEMPLATES.get(kind, "blue"),
            "title": {"tag": "plain_text", "content": title or kind or "通知"},
        },
        "elements": elements,
    }


def render_command(command: dict[str, Any]) -> tuple[str, str]:
    """Every outbox effect becomes an interactive card.

    A command carrying the `notification` contract is rendered from it; the
    projected assignment cards, which the notifier builds directly, keep the
    older path.
    """

    if command.get("notification"):
        return "interactive", canonical_json(build_notification_card(command))
    return "interactive", canonical_json(build_effect_card(command))
