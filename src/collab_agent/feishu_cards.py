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


def render_command(command: dict[str, Any]) -> tuple[str, str]:
    """Default renderer: every outbox effect becomes an interactive card."""

    return "interactive", canonical_json(build_effect_card(command))
