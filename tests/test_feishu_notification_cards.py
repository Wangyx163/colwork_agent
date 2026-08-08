from __future__ import annotations

import json
import unittest

from collab_agent.feishu_cards import (
    RETURN_REASONS,
    build_notification_card,
    render_command,
)
from collab_agent.feishu_commands import ASSISTANCE_DECISIONS, AssignmentBridge


def _command(**notification_overrides) -> dict:
    notification = {
        "notification_contract_version": "notification.v1",
        "kind": "ASSISTANCE_REQUESTED",
        "action_item_id": "ai_1",
        "subject_id": "asr_9",
        "title": "王昱翔 在任务上向你求助",
        "summary": "抖音指数工具装不上",
        "fields": [{"label": "任务", "value": "调研抖音指数"}],
        "decisions": [
            {
                "name": "ASSISTANCE_ACKNOWLEDGE",
                "label": "我来接手",
                "requires_reason": False,
            }
        ],
        "deep_link_path": "/tasks",
    }
    notification.update(notification_overrides)
    return {
        "effect_id": "eff_1",
        "effect_type": notification["kind"],
        "notification": notification,
    }


def _actions(card: dict) -> list[dict]:
    return [
        action
        for element in card["elements"]
        if element["tag"] == "action"
        for action in element["actions"]
    ]


class ContractDrivenRenderingTests(unittest.TestCase):
    """The domain states what a notification offers; the card renders that.

    Switching on effect_type here would mean every new notification type
    silently arrives as a read-only card until this module learns about it.
    """

    def test_a_decision_becomes_a_button(self) -> None:
        actions = _actions(build_notification_card(_command()))

        self.assertEqual([a["text"]["content"] for a in actions], ["我来接手"])
        self.assertEqual(actions[0]["value"]["action"], "ASSISTANCE_ACKNOWLEDGE")

    def test_the_button_carries_the_subject_the_decision_acts_on(self) -> None:
        actions = _actions(build_notification_card(_command()))

        self.assertEqual(actions[0]["value"]["subject_id"], "asr_9")

    def test_fields_are_rendered_not_dropped(self) -> None:
        card = build_notification_card(_command())

        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("调研抖音指数", rendered)

    def test_a_notification_with_no_decisions_is_read_only(self) -> None:
        card = build_notification_card(_command(kind="REVIEW_DECIDED", decisions=[]))

        self.assertEqual(_actions(card), [])

    def test_a_scored_decision_is_sent_to_the_web_not_faked_on_a_card(self) -> None:
        """Scoring several options is a form, not a tap."""

        card = build_notification_card(
            _command(
                kind="VOTE_REQUIRED",
                decisions=[
                    {
                        "name": "SUBMIT_VOTE",
                        "label": "提交评分",
                        "requires_reason": False,
                        "score_options": [{"option_id": "o1"}, {"option_id": "o2"}],
                    }
                ],
            )
        )

        self.assertEqual(_actions(card), [])
        self.assertIn("需要在网页工作台完成", json.dumps(card, ensure_ascii=False))

    def test_a_reason_requiring_decision_with_presets_becomes_a_picker(self) -> None:
        card = build_notification_card(
            _command(
                kind="ASSIGNMENT_RESPONSE_REQUIRED",
                decisions=[
                    {
                        "name": "ASSIGNMENT_RETURN",
                        "label": "退回重改",
                        "requires_reason": True,
                    }
                ],
            )
        )

        picker = _actions(card)[0]
        self.assertEqual(picker["tag"], "select_static")
        self.assertEqual(
            [option["value"] for option in picker["options"]], list(RETURN_REASONS)
        )

    def test_a_reason_requiring_decision_without_presets_goes_to_the_web(self) -> None:
        """A card cannot collect free text, and the domain refuses an empty
        reason, so offering the button would only produce a failure."""

        card = build_notification_card(
            _command(
                decisions=[
                    {"name": "SOME_NEW_ACTION", "label": "补充说明", "requires_reason": True}
                ]
            )
        )

        self.assertEqual(_actions(card), [])
        self.assertIn("补充说明", json.dumps(card, ensure_ascii=False))

    def test_render_command_prefers_the_contract_when_present(self) -> None:
        _, content = render_command(_command())

        card = json.loads(content)
        self.assertEqual(card["header"]["title"]["content"], "王昱翔 在任务上向你求助")

    def test_render_command_keeps_the_projected_path_for_commands_without_one(self) -> None:
        _, content = render_command(
            {
                "effect_id": "eff_2",
                "effect_type": "ASSIGNMENT_REQUEST",
                "content": "请确认",
            }
        )

        card = json.loads(content)
        self.assertEqual(card["header"]["title"]["content"], "任务派发")


class FakeService:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.db = None

    def update_assistance(self, request_id, **kwargs):
        self.calls.append({"request_id": request_id, **kwargs})
        return {"assistance_request_id": request_id, "status": "ACKNOWLEDGED"}


class AssistanceRoutingTests(unittest.TestCase):
    def _record(self, **overrides) -> dict:
        record = {
            "action_name": "ASSISTANCE_ACKNOWLEDGE",
            "actor_id": "actor_1",
            "operator_open_id": "ou_aaa",
            "effect_id": "eff_1",
            "event_key": "evt_1",
            "raw_value": json.dumps(
                {"action": "ASSISTANCE_ACKNOWLEDGE", "subject_id": "asr_9"}
            ),
        }
        record.update(overrides)
        return record

    def test_acknowledging_reaches_the_domain_with_the_request_id(self) -> None:
        service = FakeService()

        result = AssignmentBridge(service).handle(self._record())

        self.assertEqual(result["status"], "ACKNOWLEDGED")
        self.assertEqual(service.calls[0]["request_id"], "asr_9")
        self.assertEqual(service.calls[0]["action"], "ACKNOWLEDGE")

    def test_the_feishu_event_id_becomes_the_idempotency_key(self) -> None:
        service = FakeService()

        AssignmentBridge(service).handle(self._record())

        self.assertEqual(service.calls[0]["message_id"], "evt_1")

    def test_a_click_without_a_subject_is_refused(self) -> None:
        service = FakeService()
        record = self._record(raw_value=json.dumps({"action": "ASSISTANCE_ACKNOWLEDGE"}))

        with self.assertRaises(LookupError):
            AssignmentBridge(service).handle(record)
        self.assertEqual(service.calls, [])

    def test_an_unbound_clicker_is_never_a_decision(self) -> None:
        service = FakeService()

        with self.assertRaises(PermissionError):
            AssignmentBridge(service).handle(self._record(actor_id=None))
        self.assertEqual(service.calls, [])

    def test_resolving_is_not_offered_on_a_card(self) -> None:
        """Resolving needs a summary of what was done; a tap cannot supply it."""

        self.assertNotIn("ASSISTANCE_RESOLVE", ASSISTANCE_DECISIONS)


if __name__ == "__main__":
    unittest.main()
