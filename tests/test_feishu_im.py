from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from collab_agent.feishu_cards import build_effect_card, render_command
from collab_agent.feishu_config import FeishuConfig, load_feishu_config
from collab_agent.feishu_im import (
    FeishuIdentityUnbound,
    FeishuIM,
    RecordingTransport,
)
from collab_agent.mock_im import MockIM
from collab_agent.store import Database


SIM_TIME = "2026-03-02T09:00:00+08:00"


def _command(effect_id: str = "eff_test_0001", **overrides: object) -> dict:
    command = {
        "effect_id": effect_id,
        "effect_type": "CONFIRMATION_REQUEST",
        "conversation_id": "conv_main",
        "sender_actor_id": "agent",
        "recipient_actor_ids": ["参会者甲"],
        "content": "请确认任务“准备演示”",
    }
    command.update(overrides)
    return command


class FeishuIMTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Database(Path(self.directory.name) / "feishu.sqlite3")
        self.addCleanup(self.database.close)
        self.database.initialize()
        self.transport = RecordingTransport()
        self.im = FeishuIM(self.database, self.transport)
        self.im.bind_actor("参会者甲", "ou_aaa", display_name="甲", sim_time=SIM_TIME)

    def test_send_records_message_and_returns_mock_im_receipt_shape(self) -> None:
        receipt = self.im.send(_command(), accepted_sim_time=SIM_TIME)

        self.assertEqual(
            set(receipt),
            {"external_message_id", "deduplicated", "accepted_sim_time"},
            "receipt keys must match MockIM so dispatch_all needs no change",
        )
        self.assertFalse(receipt["deduplicated"])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0]["receive_id"], "ou_aaa")
        self.assertEqual(self.transport.calls[0]["receive_id_type"], "open_id")

        stored = self.im.messages()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["effect_id"], "eff_test_0001")

    def test_effect_id_is_sent_as_the_feishu_idempotency_uuid(self) -> None:
        self.im.send(_command(), accepted_sim_time=SIM_TIME)

        self.assertEqual(self.transport.calls[0]["uuid"], "eff_test_0001")

    def test_second_send_of_same_effect_does_not_reach_feishu_again(self) -> None:
        first = self.im.send(_command(), accepted_sim_time=SIM_TIME)
        second = self.im.send(_command(), accepted_sim_time="2026-03-02T10:00:00+08:00")

        self.assertTrue(second["deduplicated"])
        self.assertEqual(
            first["external_message_id"], second["external_message_id"]
        )
        self.assertEqual(
            second["accepted_sim_time"],
            SIM_TIME,
            "the receipt must report when the effect was first accepted",
        )
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(len(self.im.messages()), 1)

    def test_crash_after_feishu_accepted_replays_onto_the_same_message(self) -> None:
        """The gap between the network call and the local row must be safe.

        A process that dies after Feishu accepted has no local row, so the
        retry sends again -- and must land on the original message rather than
        deliver a second copy to the recipient.
        """

        command = _command()
        _, content = render_command(command)
        crashed_message_id, deduplicated = self.transport.send_message(
            receive_id="ou_aaa",
            receive_id_type="open_id",
            msg_type="interactive",
            content=content,
            uuid=command["effect_id"],
        )
        self.assertFalse(deduplicated)
        self.assertEqual(self.im.messages(), [], "the crash left no local row")

        receipt = self.im.send(command, accepted_sim_time=SIM_TIME)

        self.assertEqual(receipt["external_message_id"], crashed_message_id)
        self.assertTrue(
            receipt["deduplicated"],
            "the provider, not the local table, caught this duplicate",
        )
        self.assertEqual(len(self.im.messages()), 1)

    def test_unbound_recipient_is_refused_rather_than_guessed(self) -> None:
        command = _command(recipient_actor_ids=["参会者乙"])

        with self.assertRaises(FeishuIdentityUnbound):
            self.im.send(command, accepted_sim_time=SIM_TIME)

        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.im.messages(), [])

    def test_rebinding_an_actor_replaces_the_open_id(self) -> None:
        self.im.bind_actor("参会者甲", "ou_new", sim_time=SIM_TIME)

        self.assertEqual(self.im.open_id_for("参会者甲"), "ou_new")
        self.assertEqual(self.im.actor_for_open_id("ou_new"), "参会者甲")
        self.assertEqual(len(self.im.bindings()), 1)

    def test_multi_recipient_gets_one_uuid_per_person(self) -> None:
        self.im.bind_actor("参会者乙", "ou_bbb", sim_time=SIM_TIME)
        command = _command(recipient_actor_ids=["参会者甲", "参会者乙"])

        self.im.send(command, accepted_sim_time=SIM_TIME)

        uuids = [call["uuid"] for call in self.transport.calls]
        self.assertEqual(uuids, ["eff_test_0001_0", "eff_test_0001_1"])
        self.assertEqual(len(set(uuids)), 2, "recipients must not share a uuid")


class FeishuInboundActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Database(Path(self.directory.name) / "feishu.sqlite3")
        self.addCleanup(self.database.close)
        self.database.initialize()
        self.im = FeishuIM(self.database, RecordingTransport())
        self.im.bind_actor("参会者甲", "ou_aaa", sim_time=SIM_TIME)

    def _record(self, event_key: str = "evt_1") -> dict:
        return self.im.record_inbound_action(
            event_key=event_key,
            operator_open_id="ou_aaa",
            action_name="accept",
            effect_id="eff_test_0001",
            raw_value={"action": "accept", "effect_id": "eff_test_0001"},
            sim_time=SIM_TIME,
        )

    def test_click_resolves_back_to_the_bound_actor(self) -> None:
        outcome = self._record()

        self.assertFalse(outcome["deduplicated"])
        pending = self.im.pending_inbound_actions()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["actor_id"], "参会者甲")
        self.assertEqual(pending[0]["action_name"], "accept")

    def test_redelivered_click_is_not_a_second_decision(self) -> None:
        first = self._record()
        second = self._record()

        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["action_id"], second["action_id"])
        self.assertEqual(len(self.im.pending_inbound_actions()), 1)

    def test_click_from_an_unbound_operator_is_still_recorded(self) -> None:
        outcome = self.im.record_inbound_action(
            event_key="evt_2",
            operator_open_id="ou_stranger",
            action_name="accept",
            effect_id="eff_test_0001",
            raw_value={},
            sim_time=SIM_TIME,
        )

        self.assertFalse(outcome["deduplicated"])
        pending = self.im.pending_inbound_actions()
        self.assertIsNone(
            pending[0]["actor_id"],
            "an unknown clicker must be recorded without being guessed at",
        )


class FeishuCardTests(unittest.TestCase):
    def test_decision_effect_gets_buttons_carrying_the_effect_id(self) -> None:
        card = build_effect_card(_command())

        actions = [e for e in card["elements"] if e["tag"] == "action"]
        self.assertEqual(len(actions), 1)
        values = [button["value"] for button in actions[0]["actions"]]
        self.assertEqual([v["action"] for v in values], ["accept", "return"])
        for value in values:
            self.assertEqual(value["effect_id"], "eff_test_0001")

    def test_notice_effect_has_no_buttons(self) -> None:
        card = build_effect_card(_command(effect_type="ACCEPTANCE_NOTICE"))

        self.assertEqual([e for e in card["elements"] if e["tag"] == "action"], [])

    def test_render_command_emits_interactive_json(self) -> None:
        msg_type, content = render_command(_command())

        self.assertEqual(msg_type, "interactive")
        self.assertEqual(json.loads(content)["header"]["template"], "blue")


class FeishuConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env_path = Path(self.directory.name) / ".env.local"
        for name in (
            "FEISHU_APP_ID",
            "FEISHU_APP_SECRET",
            "FEISHU_ENCRYPT_KEY",
            "FEISHU_VERIFICATION_TOKEN",
        ):
            original = os.environ.pop(name, None)
            if original is not None:
                self.addCleanup(os.environ.__setitem__, name, original)

    def test_reads_credentials_from_local_env_file(self) -> None:
        self.env_path.write_text(
            "# comment\nFEISHU_APP_ID=cli_abc\nFEISHU_APP_SECRET=secret_xyz\n",
            encoding="utf-8",
        )

        config = load_feishu_config(self.env_path)

        self.assertEqual(config.app_id, "cli_abc")
        self.assertEqual(config.app_secret, "secret_xyz")
        self.assertEqual(config.encrypt_key, "")

    def test_process_environment_overrides_the_file(self) -> None:
        self.env_path.write_text(
            "FEISHU_APP_ID=from_file\nFEISHU_APP_SECRET=from_file\n", encoding="utf-8"
        )
        os.environ["FEISHU_APP_ID"] = "from_env"
        self.addCleanup(os.environ.pop, "FEISHU_APP_ID", None)

        config = load_feishu_config(self.env_path)

        self.assertEqual(config.app_id, "from_env")

    def test_missing_credentials_name_what_is_missing(self) -> None:
        self.env_path.write_text("FEISHU_APP_ID=cli_abc\n", encoding="utf-8")

        with self.assertRaises(ValueError) as caught:
            load_feishu_config(self.env_path)

        self.assertIn("FEISHU_APP_SECRET", str(caught.exception))

    def test_console_labels_are_diagnosed_without_leaking_the_secret(self) -> None:
        """Pasting the console's own labels is the likely first mistake."""

        self.env_path.write_text(
            "App ID=cli_abc\nApp Secret=super_secret_value\n", encoding="utf-8"
        )

        with self.assertRaises(ValueError) as caught:
            load_feishu_config(self.env_path)

        message = str(caught.exception)
        self.assertIn("App ID", message, "the error must show what was found")
        self.assertIn("FEISHU_APP_ID", message, "and what was expected")
        self.assertNotIn("super_secret_value", message, "values must never surface")

    def test_redacted_never_exposes_the_secret(self) -> None:
        config = FeishuConfig(app_id="cli_abc", app_secret="super_secret_value")

        redacted = json.dumps(config.redacted())

        self.assertNotIn("super_secret", redacted)
        self.assertIn("cli_abc", redacted)


class AdapterContractTests(unittest.TestCase):
    """FeishuIM must be substitutable for MockIM at the dispatcher seam."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Database(Path(self.directory.name) / "contract.sqlite3")
        self.addCleanup(self.database.close)
        self.database.initialize()

    def test_both_adapters_return_the_same_receipt_keys(self) -> None:
        mock = MockIM(self.database)
        feishu = FeishuIM(self.database, RecordingTransport())
        feishu.bind_actor("参会者甲", "ou_aaa", sim_time=SIM_TIME)

        mock_receipt = mock.send(_command("eff_mock"), accepted_sim_time=SIM_TIME)
        feishu_receipt = feishu.send(_command("eff_feishu"), accepted_sim_time=SIM_TIME)

        self.assertEqual(set(mock_receipt), set(feishu_receipt))

    def test_both_adapters_deduplicate_on_effect_id(self) -> None:
        mock = MockIM(self.database)
        feishu = FeishuIM(self.database, RecordingTransport())
        feishu.bind_actor("参会者甲", "ou_aaa", sim_time=SIM_TIME)

        for adapter, effect in ((mock, "eff_mock"), (feishu, "eff_feishu")):
            adapter.send(_command(effect), accepted_sim_time=SIM_TIME)
            repeat = adapter.send(_command(effect), accepted_sim_time=SIM_TIME)
            self.assertTrue(
                repeat["deduplicated"], f"{type(adapter).__name__} lost idempotency"
            )


if __name__ == "__main__":
    unittest.main()
