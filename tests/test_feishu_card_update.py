from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from collab_agent.feishu_app import FeishuApp
from collab_agent.feishu_cards import build_decided_card, build_effect_card
from collab_agent.feishu_config import FeishuConfig
from collab_agent.feishu_im import FeishuIM, RecordingTransport
from collab_agent.store import Database


SIM_TIME = "2026-03-09T09:00:00+08:00"
EFFECT = "eff_card_update"


def _card_event(action: str = "accept", event_id: str = "evt_1", reason: str = ""):
    value = {"action": action, "effect_id": EFFECT, "action_item_id": "ai_1"}
    if reason:
        value["reason"] = reason
    return SimpleNamespace(
        header=SimpleNamespace(event_id=event_id),
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_aaa"),
            action=SimpleNamespace(value=value, option=reason or None),
        ),
    )


class DecidedCardTests(unittest.TestCase):
    def test_a_decided_card_offers_no_controls(self) -> None:
        """Disabled buttons still invite a click; removed ones cannot."""

        card = build_decided_card("原任务说明", decision="ACCEPT")

        self.assertEqual([e for e in card["elements"] if e["tag"] == "action"], [])
        self.assertIn("已接受", json.dumps(card, ensure_ascii=False))

    def test_the_original_text_survives_the_rewrite(self) -> None:
        card = build_decided_card("准备7-8个采访问题", decision="ACCEPT")

        self.assertIn("准备7-8个采访问题", json.dumps(card, ensure_ascii=False))

    def test_a_return_shows_the_reason(self) -> None:
        card = build_decided_card(
            "原任务", decision="RETURN_FOR_REVISION", reason="时间安排不可行"
        )

        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("已退回", rendered)
        self.assertIn("时间安排不可行", rendered)


class CardUpdateFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Database(Path(self.directory.name) / "card.sqlite3")
        self.addCleanup(self.database.close)
        self.database.initialize()
        self.transport = RecordingTransport()
        self.im = FeishuIM(self.database, self.transport)
        self.im.bind_actor("actor_1", "ou_aaa", sim_time=SIM_TIME)
        self.im.send(
            {
                "effect_id": EFFECT,
                "effect_type": "ASSIGNMENT_REQUEST",
                "conversation_id": "conv",
                "sender_actor_id": "agent",
                "recipient_actor_ids": ["actor_1"],
                "action_item_id": "ai_1",
                "content": "准备7-8个采访问题",
            },
            accepted_sim_time=SIM_TIME,
        )
        self.logs: list[str] = []
        self.app = FeishuApp(
            FeishuConfig(app_id="x", app_secret="y"), self.im, log=self.logs.append
        )

    def _drain(self) -> None:
        while self.app.drain_once(timeout=0.01):
            pass

    def test_the_card_still_has_buttons_before_the_decision(self) -> None:
        card = json.loads(self.transport.calls[0]["content"])

        self.assertTrue([e for e in card["elements"] if e["tag"] == "action"])

    def test_the_card_is_rewritten_after_the_worker_decides(self) -> None:
        self.app.on_action = lambda record: None

        self.app.handle_card_action(_card_event())
        self._drain()

        self.assertEqual(len(self.transport.updates), 1)
        card = json.loads(self.transport.updates[0]["content"])
        self.assertEqual([e for e in card["elements"] if e["tag"] == "action"], [])
        self.assertIn("已接受", json.dumps(card, ensure_ascii=False))

    def test_the_update_targets_the_message_the_send_produced(self) -> None:
        self.app.on_action = lambda record: None

        self.app.handle_card_action(_card_event())
        self._drain()

        self.assertEqual(
            self.transport.updates[0]["message_id"],
            self.im.messages()[0]["external_message_id"],
        )

    def test_a_refused_decision_does_not_claim_acceptance(self) -> None:
        """The domain can still say no after the callback has answered."""

        def refuse(_record):
            raise PermissionError("actor is not assigned to this task version")

        self.app.on_action = refuse
        self.app.handle_card_action(_card_event())
        self._drain()

        rendered = json.dumps(
            json.loads(self.transport.updates[0]["content"]), ensure_ascii=False
        )
        self.assertNotIn("已接受", rendered)
        self.assertIn("处理失败", rendered)

    def test_the_stored_content_follows_the_update(self) -> None:
        self.app.on_action = lambda record: None

        self.app.handle_card_action(_card_event())
        self._drain()

        stored = json.loads(self.im.messages()[0]["content"])
        self.assertEqual([e for e in stored["elements"] if e["tag"] == "action"], [])

    def test_a_failed_update_does_not_undo_the_decision(self) -> None:
        decided: list[str] = []
        self.app.on_action = lambda record: decided.append(record["action_name"])

        def explode(**_kwargs):
            raise RuntimeError("feishu unreachable")

        self.transport.update_message = explode
        self.app.handle_card_action(_card_event())
        self._drain()

        self.assertEqual(decided, ["accept"])
        row = self.database.one(
            "SELECT status FROM feishu_inbound_actions WHERE event_key = ?", ("evt_1",)
        )
        self.assertEqual(dict(row)["status"], "PROCESSED")
        self.assertTrue(any("card update failed" in line for line in self.logs))

    def test_an_effect_with_no_card_is_skipped_quietly(self) -> None:
        self.app.on_action = lambda record: None
        event = _card_event(event_id="evt_other")
        event.event.action.value["effect_id"] = "eff_never_sent"

        self.app.handle_card_action(event)
        self._drain()

        self.assertEqual(self.transport.updates, [])


if __name__ == "__main__":
    unittest.main()
