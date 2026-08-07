from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from collab_agent.feishu_cards import build_effect_card
from collab_agent.feishu_commands import (
    AssignmentBridge,
    UnknownCardAction,
    UnresolvableEffect,
    resolve_action_item,
)
from collab_agent.feishu_im import FeishuIM, RecordingTransport
from collab_agent.feishu_notifier import AssignmentNotifier
from collab_agent.meeting import load_meeting_service
from collab_agent.models import parse_time
from collab_agent.store import Database


EXTRACTION = {
    "provider": "test",
    "model": "test",
    "input_sha256": "e" * 64,
    "action_items": [
        {
            "title": "准备演示",
            "deliverable": "面试演示",
            "owner_name": None,
            "deadline_text": None,
            "deadline_iso": None,
            "source_timestamp": "00:01:00",
            "source_quote": "请准备面试演示",
            "confidence": 0.95,
            "needs_confirmation": True,
            "uncertainties": ["负责人未明确"],
        }
    ],
}

TRANSCRIPT = "会议负责人 (00:01:00): 请准备面试演示\n"


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        extraction_path = root / "extraction.json"
        transcript_path = root / "transcript.txt"
        extraction_path.write_text(
            json.dumps(EXTRACTION, ensure_ascii=False), encoding="utf-8"
        )
        transcript_path.write_text(TRANSCRIPT, encoding="utf-8")

        self.database = Database(root / "meeting.sqlite3")
        self.addCleanup(self.database.close)
        self.database.initialize()
        self.transport = RecordingTransport()
        self.im = FeishuIM(self.database, self.transport)
        self.service = load_meeting_service(
            self.database,
            extraction_path=extraction_path,
            transcript_path=transcript_path,
            organization_name="测试团队",
            coordinator_name="会议负责人",
            participant_names=["会议负责人", "参会者甲"],
            im=self.im,
        )
        self.coordinator_id = self.service.aggregator_actor_id
        self.owner_id = self._actor_id("参会者甲")
        # The binding keys on the internal actor id, which is what every
        # coordination command authorises against.
        self.im.bind_actor(self.owner_id, "ou_aaa", sim_time=self.service.now())

        action = dict(
            self.database.one(
                "SELECT * FROM action_items WHERE episode_id = ?",
                (self.service.episode_id,),
            )
        )
        self.action_item_id = action["action_item_id"]
        metadata = self.service.proposal_metadata(action)
        deadline = (parse_time(self.service.now()) + timedelta(days=3)).isoformat()
        self.service.revise_action_proposal(
            self.action_item_id,
            actor_id=self.coordinator_id,
            title=action["title"],
            deliverable=metadata["deliverable"],
            acceptance_criteria=metadata.get("acceptance_criteria", ""),
            work_requirements="提交可执行的演示脚本",
            management_review_policy="包含时间和责任人",
            priority="P0",
            team_required_by_sim_time=deadline,
            message_id="msg_revise",
        )
        self.service.dispatch_action(
            self.action_item_id,
            actor_id=self.coordinator_id,
            owner_actor_id=self.owner_id,
            collaborator_actor_ids=[],
            assignment_message="请准备面试演示",
            message_id="msg_dispatch",
        )
        # Dispatch is pull-based and enqueues no Outbox effect, so the notifier
        # is what turns a pending assignment into a card.
        self.notifier = AssignmentNotifier(self.service, self.im)
        self.notifier.notify_once()
        self.effect_id = self.im.messages()[0]["effect_id"]
        self.bridge = AssignmentBridge(self.service)

    def _actor_id(self, display_name: str) -> str:
        row = self.database.one(
            "SELECT actor_id FROM actors WHERE display_name = ?", (display_name,)
        )
        assert row, f"no actor named {display_name}"
        return dict(row)["actor_id"]

    def _record(self, action_name: str, *, event_key: str, reason: str = "") -> dict:
        raw: dict[str, object] = {
            "action": action_name,
            "effect_id": self.effect_id,
            "action_item_id": self.action_item_id,
        }
        if reason:
            raw["reason"] = reason
        outcome = self.im.record_inbound_action(
            event_key=event_key,
            operator_open_id="ou_aaa",
            action_name=action_name,
            effect_id=self.effect_id,
            raw_value=raw,
            sim_time=self.service.now(),
        )
        return dict(
            self.database.one(
                "SELECT * FROM feishu_inbound_actions WHERE action_id = ?",
                (outcome["action_id"],),
            )
        )

    def test_the_dispatch_actually_left_through_feishu(self) -> None:
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0]["receive_id"], "ou_aaa")
        self.assertEqual(self.transport.calls[0]["uuid"], self.effect_id)

    def test_polling_again_does_not_resend_the_same_assignment(self) -> None:
        outcome = self.notifier.notify_once()

        self.assertEqual(outcome["sent"], [], "a second poll must send nothing")
        self.assertEqual(len(self.transport.calls), 1)

    def test_card_carries_the_action_item_so_a_click_can_resolve(self) -> None:
        card = json.loads(self.transport.calls[0]["content"])
        button = [e for e in card["elements"] if e["tag"] == "action"][0]["actions"][0]

        self.assertEqual(button["value"]["action_item_id"], self.action_item_id)

    def test_card_id_wins_over_the_outbox_lookup(self) -> None:
        self.assertEqual(
            resolve_action_item(
                self.database, "eff_not_in_outbox", from_card=self.action_item_id
            ),
            self.action_item_id,
        )

    def test_unknown_effect_with_no_card_id_is_refused(self) -> None:
        with self.assertRaises(UnresolvableEffect):
            resolve_action_item(self.database, "eff_never_sent")

    def test_unbound_assignee_is_skipped_not_fatal(self) -> None:
        # Return the assignment to the not-yet-sent state, then drop the
        # binding: this is what dispatching to someone who never bound looks
        # like. The poll must skip them and keep going.
        with self.database.transaction() as cursor:
            cursor.execute("DELETE FROM feishu_im_messages")
        self.im.unbind_actor(self.owner_id)

        outcome = self.notifier.notify_once()

        self.assertEqual(outcome["sent"], [])
        self.assertEqual(len(outcome["skipped"]), 1)
        self.assertEqual(outcome["skipped"][0]["actor_id"], self.owner_id)

    def test_unbinding_after_delivery_does_not_resend(self) -> None:
        """A card already in someone's chat is delivered; unbinding is not undo."""

        self.im.unbind_actor(self.owner_id)

        outcome = self.notifier.notify_once()

        self.assertEqual(outcome["sent"], [])
        self.assertEqual(outcome["skipped"], [])
        self.assertEqual(len(self.transport.calls), 1)

    def test_accept_click_moves_the_real_domain_state(self) -> None:
        record = self._record("accept", event_key="evt_accept")

        result = self.bridge.handle(record)

        self.assertEqual(result["action_item_id"], self.action_item_id)
        action = self.database.one(
            "SELECT status, owner_actor_id FROM action_items WHERE action_item_id = ?",
            (self.action_item_id,),
        )
        self.assertEqual(dict(action)["status"], "TRACKING")
        self.assertEqual(dict(action)["owner_actor_id"], self.owner_id)

    def test_redelivered_click_replays_instead_of_deciding_twice(self) -> None:
        record = self._record("accept", event_key="evt_same")

        first = self.bridge.handle(record)
        second = self.bridge.handle(record)

        self.assertEqual(first, second, "the inbound receipt must replay the result")

    def test_return_needs_a_reason(self) -> None:
        record = self._record("return", event_key="evt_return_bare")

        with self.assertRaises(ValueError) as caught:
            self.bridge.handle(record)

        self.assertIn("reason", str(caught.exception))

    def test_return_with_a_picked_reason_sends_the_task_back(self) -> None:
        record = self._record(
            "return", event_key="evt_return", reason="时间安排不可行，需要调整工期"
        )

        self.bridge.handle(record)

        action = self.database.one(
            "SELECT status FROM action_items WHERE action_item_id = ?",
            (self.action_item_id,),
        )
        self.assertEqual(dict(action)["status"], "NEEDS_REVISION")

    def test_click_from_an_unbound_person_is_never_a_decision(self) -> None:
        record = self._record("accept", event_key="evt_stranger")
        record["actor_id"] = None

        with self.assertRaises(PermissionError):
            self.bridge.handle(record)

    def test_unsupported_card_action_is_refused(self) -> None:
        record = self._record("accept", event_key="evt_weird")
        record["action_name"] = "delete_everything"

        with self.assertRaises(UnknownCardAction):
            self.bridge.handle(record)


class CardReturnPickerTests(unittest.TestCase):
    def test_return_is_a_picker_so_a_reason_always_exists(self) -> None:
        card = build_effect_card(
            {
                "effect_id": "eff_1",
                "effect_type": "ASSIGNMENT_REQUEST",
                "content": "请确认",
            }
        )

        actions = [e for e in card["elements"] if e["tag"] == "action"][0]["actions"]
        tags = [a["tag"] for a in actions]
        self.assertEqual(tags, ["button", "select_static"])
        picker = actions[1]
        self.assertEqual(picker["value"]["action"], "return")
        self.assertTrue(picker["options"], "a return must offer at least one reason")


if __name__ == "__main__":
    unittest.main()
