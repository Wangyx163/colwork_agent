from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from collab_agent.feishu_app import FeishuApp
from collab_agent.feishu_config import FeishuConfig
from collab_agent.feishu_im import FeishuIM, RecordingTransport
from collab_agent.store import Database


def _message_event(
    *, open_id: str = "ou_aaa", message_id: str = "om_1", text: str = "你好"
) -> SimpleNamespace:
    return SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=open_id)),
            message=SimpleNamespace(
                message_id=message_id,
                content=json.dumps({"text": text}),
                message_type="text",
            ),
        )
    )


def _card_event(
    *,
    open_id: str = "ou_aaa",
    event_id: str = "evt_1",
    action: str = "accept",
    effect_id: str = "eff_test_0001",
) -> SimpleNamespace:
    return SimpleNamespace(
        header=SimpleNamespace(event_id=event_id),
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id=open_id),
            action=SimpleNamespace(
                value={"action": action, "effect_id": effect_id}
            ),
        ),
    )


class FeishuMinimalLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Database(Path(self.directory.name) / "loop.sqlite3")
        self.addCleanup(self.database.close)
        self.database.initialize()
        self.transport = RecordingTransport()
        self.im = FeishuIM(self.database, self.transport)
        self.im.bind_actor(
            "参会者甲", "ou_aaa", display_name="甲", sim_time="2026-03-02T09:00:00+08:00"
        )
        self.logs: list[str] = []
        self.app = FeishuApp(
            FeishuConfig(app_id="cli_test", app_secret="secret"),
            self.im,
            log=self.logs.append,
        )

    def _drain(self) -> None:
        while self.app.drain_once(timeout=0.01):
            pass

    def test_chat_reply_is_read_only(self) -> None:
        """A chat reply must never carry decision buttons.

        Any card minted from a chat message would need an EffectId, and there
        is no task behind it -- so clicking it could only fail. Decision cards
        come from the notifier, which starts from a real pending assignment.
        """

        self.app.handle_message_receive(_message_event(text="开始"))
        self._drain()

        self.assertEqual(len(self.transport.calls), 1)
        call = self.transport.calls[0]
        self.assertEqual(call["receive_id"], "ou_aaa")
        self.assertEqual(call["msg_type"], "interactive")

        card = json.loads(call["content"])
        self.assertEqual(
            [e for e in card["elements"] if e["tag"] == "action"],
            [],
            "a chat reply must have no buttons",
        )
        self.assertEqual(
            self.im.messages(),
            [],
            "a chat reply is not an Outbox-tracked effect",
        )

    def test_callback_returns_before_any_business_work_happens(self) -> None:
        """The 3-second budget means the handler may only park the click."""

        response = self.app.handle_card_action(_card_event())

        self.assertIsNotNone(response)
        pending = self.im.pending_inbound_actions()
        self.assertEqual(len(pending), 1, "the click is durable before processing")
        self.assertEqual(
            pending[0]["status"],
            "PENDING",
            "nothing may be processed inside the callback",
        )

    def test_worker_processes_the_parked_click(self) -> None:
        seen: list[dict] = []
        self.app.on_action = seen.append

        self.app.handle_card_action(_card_event())
        self._drain()

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["action_name"], "accept")
        self.assertEqual(seen[0]["actor_id"], "参会者甲")
        self.assertEqual(self.im.pending_inbound_actions(), [])
        row = self.database.one(
            "SELECT status FROM feishu_inbound_actions WHERE event_key = ?", ("evt_1",)
        )
        self.assertEqual(dict(row)["status"], "PROCESSED")

    def test_redelivered_callback_does_not_queue_a_second_decision(self) -> None:
        seen: list[dict] = []
        self.app.on_action = seen.append

        self.app.handle_card_action(_card_event(event_id="evt_dup"))
        self.app.handle_card_action(_card_event(event_id="evt_dup"))
        self._drain()

        self.assertEqual(len(seen), 1, "Feishu redelivery must not double-decide")

    def test_failing_action_is_recorded_not_lost(self) -> None:
        def explode(_record: dict) -> None:
            raise RuntimeError("downstream refused")

        self.app.on_action = explode
        self.app.handle_card_action(_card_event(event_id="evt_fail"))
        self._drain()

        row = self.database.one(
            "SELECT status, process_error FROM feishu_inbound_actions "
            "WHERE event_key = ?",
            ("evt_fail",),
        )
        record = dict(row)
        self.assertEqual(record["status"], "FAILED")
        self.assertIn("downstream refused", record["process_error"])

    def test_unbound_sender_is_told_how_to_get_bound(self) -> None:
        self.app.handle_message_receive(_message_event(open_id="ou_stranger"))
        self._drain()

        self.assertEqual(len(self.transport.calls), 1)
        card = json.loads(self.transport.calls[0]["content"])
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("ou_stranger", rendered)
        self.assertIn("尚未绑定", rendered)
        self.assertEqual(
            self.im.messages(), [], "an unbound sender gets no Outbox-tracked effect"
        )

    def test_repeated_inbound_message_reuses_the_same_uuid(self) -> None:
        self.app.handle_message_receive(_message_event(message_id="om_same"))
        self.app.handle_message_receive(_message_event(message_id="om_same"))
        self._drain()

        uuids = {call["uuid"] for call in self.transport.calls}
        self.assertEqual(
            len(uuids), 1, "the same source message must reuse one idempotency key"
        )

    def test_a_bad_event_never_escapes_the_callback(self) -> None:
        broken = SimpleNamespace(event=None)

        self.app.handle_message_receive(broken)
        response = self.app.handle_card_action(broken)

        self.assertIsNotNone(response)
        self.assertTrue(any("error" in line for line in self.logs))


if __name__ == "__main__":
    unittest.main()
