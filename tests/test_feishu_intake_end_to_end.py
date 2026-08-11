from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path

from collab_agent import episode_registry
from collab_agent.feishu_app import FeishuApp
from collab_agent.feishu_config import FeishuConfig
from collab_agent.feishu_im import FeishuIM
from collab_agent.feishu_intake_flow import MeetingIntake, ROSTER_CONFIRM
from collab_agent.feishu_registration import Registrar
from collab_agent.intake_cache import IntakeCache
from collab_agent.models import stable_hash
from collab_agent.store import Database


TRANSCRIPT = (
    "王昱翔(00:01:00): 这周要把周报整理出来\n"
    "黄Z恒(00:02:00): 我来做数据那块\n"
)


def event(**fields):
    """Build the nested object lark hands a callback, with only what is read."""

    return types.SimpleNamespace(**fields)


class FakeMinutes:
    def get_transcript(self, minute_token: str, *, file_format: str = "srt") -> str:
        return TRANSCRIPT

    def get_chat_members(self, chat_id: str) -> list[dict]:
        return [
            {"name": "王昱翔", "open_id": "ou_lead"},
            {"name": "黄Z恒", "open_id": "ou_heng"},
        ]


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_message(self, **kwargs) -> tuple[str, dict]:
        self.sent.append(kwargs)
        return f"om_{len(self.sent)}", {}

    def update_card(self, **kwargs) -> None:
        self.sent.append({"update": kwargs})

    def cards(self) -> str:
        return json.dumps(self.sent, ensure_ascii=False)


class IntakeThroughTheAppTests(unittest.TestCase):
    """The chat path end to end, driven through the app's own queue.

    Not through the flow object directly: the callbacks park work and the
    worker performs it, and every defect this project has actually shipped
    lived in the seam between what one side puts on a queue and what the other
    side reads off it.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.initialize()

        cache = IntakeCache(
            extractions_dir=root / "extractions", transcripts_dir=root / "transcripts"
        )
        (root / "extractions").mkdir()
        # Prepared rather than extracted: this asserts the cached mode, which
        # is the one the demo runs and the one that must never call a model.
        (root / "extractions" / "prepared.json").write_text(
            json.dumps(
                {
                    "provider": "fixture",
                    "model": "deterministic",
                    "input_sha256": stable_hash(TRANSCRIPT),
                    "source": {
                        "filename": "20260309214014-王昱翔的快速会议-逐字稿文本-1.txt"
                    },
                    "action_items": [
                        {
                            "title": "整理周报",
                            "deliverable": "周报",
                            "owner_name": None,
                            "deadline_text": None,
                            "deadline_iso": None,
                            "source_timestamp": "00:01:00",
                            "source_quote": "这周要把周报整理出来",
                            "confidence": 0.9,
                            "needs_confirmation": True,
                            "uncertainties": [],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        self.transport = RecordingTransport()
        self.im = FeishuIM(self.db, self.transport)
        self.im.bind_actor(
            "actor_lead",
            "ou_lead",
            display_name="王昱翔",
            sim_time="2026-08-12T09:00:00+10:00",
        )
        self.ready: list[dict] = []
        self.app = FeishuApp(
            FeishuConfig(app_id="dry", app_secret="dry"),
            self.im,
            registrar=Registrar(self.db, self.im, base_url="http://127.0.0.1:8766"),
            intake=MeetingIntake(
                self.db,
                transport=FakeMinutes(),
                organization_name="端到端测试团队",
                cache=cache,
                mode="cache",
                base_url="http://127.0.0.1:8766",
            ),
            on_meeting_ready=self.ready.append,
            log=lambda line: None,
        )

    def drain(self) -> None:
        while self.app.drain_once(timeout=0.01):
            pass

    def send(self, text: str, *, open_id: str = "ou_lead") -> None:
        self.app.handle_message_receive(
            event(
                event=event(
                    sender=event(sender_id=event(open_id=open_id)),
                    message=event(
                        message_id=f"om_in_{len(self.transport.sent)}",
                        content=json.dumps({"text": text}),
                        chat_id="oc_group",
                        chat_type="group",
                    ),
                )
            )
        )
        self.drain()

    def click(self, value: dict, *, open_id: str = "ou_lead") -> None:
        self.app.handle_card_action(
            event(
                header=event(event_id=f"ev_{len(self.transport.sent)}"),
                event=event(
                    operator=event(open_id=open_id),
                    action=event(value=value, option=None),
                ),
            )
        )
        self.drain()

    # ---- the chain -----------------------------------------------------

    def test_a_link_then_one_tap_makes_a_meeting(self) -> None:
        self.send("@_user_1 https://x.feishu.cn/minutes/obcnabc123?from=copylink")

        self.assertIn("确认参会名单", self.transport.cards())
        intake_id = self.db.one(
            "SELECT intake_id FROM meeting_intakes", ()
        )["intake_id"]

        self.click(
            {
                "action": ROSTER_CONFIRM,
                "intake_id": intake_id,
                "choice": "spoke",
            }
        )

        self.assertEqual(len(self.ready), 1)
        outcome = self.ready[0]
        self.assertIn("会议已就绪", self.transport.cards())
        self.assertIn(outcome["url"], self.transport.cards())
        self.assertIsNotNone(
            episode_registry.source_for(self.db, outcome["episode_id"])
        )

    def test_the_slug_is_the_coordinators_name(self) -> None:
        """What the URL is for: something a person can read out in a message."""

        self.send("@_user_1 https://x.feishu.cn/minutes/obcnabc123")
        intake_id = self.db.one("SELECT intake_id FROM meeting_intakes", ())["intake_id"]
        self.click(
            {"action": ROSTER_CONFIRM, "intake_id": intake_id, "choice": "spoke"}
        )

        self.assertTrue(self.ready[0]["url"].endswith("/manage"))
        self.assertRegex(self.ready[0]["slug"], r"^[a-z][a-z0-9-]*$")

    def test_somebody_else_tapping_confirm_does_not_make_the_meeting(self) -> None:
        """There is no episode yet, so there is no roster to check them
        against; the only defensible authority is having started it."""

        self.send("@_user_1 https://x.feishu.cn/minutes/obcnabc123")
        intake_id = self.db.one("SELECT intake_id FROM meeting_intakes", ())["intake_id"]

        self.click(
            {"action": ROSTER_CONFIRM, "intake_id": intake_id, "choice": "spoke"},
            open_id="ou_heng",
        )

        self.assertEqual(self.ready, [])

    def test_a_message_with_no_link_still_registers(self) -> None:
        """Both paths share one inbox, and the link check must not eat an
        ordinary registration."""

        self.send("@_user_1 你好", open_id="ou_someone")

        self.assertNotIn("确认参会名单", self.transport.cards())
        self.assertIn("需要会议和名字", self.transport.cards())

    def test_the_same_link_twice_returns_the_meeting_rather_than_a_second_one(
        self,
    ) -> None:
        self.send("@_user_1 https://x.feishu.cn/minutes/obcnabc123")
        intake_id = self.db.one("SELECT intake_id FROM meeting_intakes", ())["intake_id"]
        self.click(
            {"action": ROSTER_CONFIRM, "intake_id": intake_id, "choice": "spoke"}
        )

        self.send("@_user_1 https://x.feishu.cn/minutes/obcnabc123")

        self.assertIn("这场会已经建好了", self.transport.cards())
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM episodes", ())["n"], 1)

    def test_the_new_meeting_can_then_be_joined_by_messaging_the_bot(self) -> None:
        """The two halves have to meet: a meeting nobody can register into is
        a meeting with one person in it."""

        self.send("@_user_1 https://x.feishu.cn/minutes/obcnabc123")
        intake_id = self.db.one("SELECT intake_id FROM meeting_intakes", ())["intake_id"]
        self.click(
            {"action": ROSTER_CONFIRM, "intake_id": intake_id, "choice": "spoke"}
        )

        self.send("@_user_1 快速会议 黄Z恒", open_id="ou_heng")

        self.assertIsNotNone(self.im.actor_for_open_id("ou_heng"))

    def test_an_unregistered_person_pasting_a_link_is_told_what_to_do(self) -> None:
        self.send("@_user_1 https://x.feishu.cn/minutes/obcnabc123", open_id="ou_new")

        self.assertIn("这份妙记读不了", self.transport.cards())
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM episodes", ())["n"], 0)


if __name__ == "__main__":
    unittest.main()
