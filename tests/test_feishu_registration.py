from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from collab_agent import episode_registry
from collab_agent.feishu_im import FeishuIM
from collab_agent.feishu_registration import (
    REGISTRATION_REVOKE_ACTION,
    Registrar,
    match_meeting,
    parse_registration,
)
from collab_agent.meeting import load_meeting_service
from collab_agent.store import Database


class RecordingTransport:
    """Enough of the Feishu transport to see what would have been sent."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_message(self, **kwargs) -> tuple[str, dict]:
        self.sent.append(kwargs)
        return f"om_{len(self.sent)}", {}

    def update_card(self, **kwargs) -> None:
        self.sent.append({"update": kwargs})


def extraction_for(quote: str, filename: str) -> dict:
    return {
        "provider": "fixture",
        "model": "deterministic",
        "input_sha256": hashlib.sha256(quote.encode()).hexdigest(),
        "source": {"filename": filename},
        "action_items": [
            {
                "title": f"任务：{quote}",
                "deliverable": "一页说明",
                "owner_name": None,
                "deadline_text": None,
                "deadline_iso": None,
                "source_timestamp": "00:01:00",
                "source_quote": quote,
                "confidence": 0.9,
                "needs_confirmation": True,
                "uncertainties": [],
            }
        ],
    }


class ParsingTests(unittest.TestCase):
    """People will not type one shape, so the parser must not require one."""

    def test_the_mention_is_stripped(self) -> None:
        request = parse_registration("@_user_1 快速会议 黄Z恒")

        self.assertEqual(request.meeting_hint, "快速会议")
        self.assertEqual(request.name_hint, "黄Z恒")

    def test_chinese_separators_work_like_spaces(self) -> None:
        request = parse_registration("@机器人，快速会议，黄Z恒")

        self.assertEqual(request.name_hint, "黄Z恒")

    def test_the_name_is_the_last_token_so_meeting_names_may_have_spaces(
        self,
    ) -> None:
        request = parse_registration("@机器人 ACISC 媒体运营中心第一次例会 绒")

        self.assertEqual(request.meeting_hint, "ACISC 媒体运营中心第一次例会")
        self.assertEqual(request.name_hint, "绒")

    def test_filler_words_do_not_become_the_name(self) -> None:
        request = parse_registration("@机器人 注册 快速会议 我是 黄Z恒")

        self.assertEqual(request.name_hint, "黄Z恒")

    def test_one_word_is_not_enough_to_act_on(self) -> None:
        self.assertFalse(parse_registration("@机器人 你好").complete)


class MatchingTests(unittest.TestCase):
    class Source:
        def __init__(self, slug: str, title: str) -> None:
            self.slug = slug
            self.title = title

    def setUp(self) -> None:
        self.sources = [
            self.Source("wangyuxiang01", "王昱翔的快速会议 · 2026-03-09"),
            self.Source("wangyuxiang02", "ACISC 媒体运营中心第一次例会 · 2026-03-02"),
        ]

    def test_an_exact_slug_wins(self) -> None:
        self.assertEqual(
            match_meeting(self.sources, "wangyuxiang02").slug, "wangyuxiang02"
        )

    def test_a_distinctive_fragment_of_the_title_matches(self) -> None:
        self.assertEqual(
            match_meeting(self.sources, "快速会议").slug, "wangyuxiang01"
        )

    def test_an_ambiguous_hint_matches_nothing(self) -> None:
        """Nothing rather than the first: binding into the wrong meeting hands
        out the wrong person's tasks, and an ambiguous hint is exactly when a
        guess is most likely to be wrong."""

        self.assertIsNone(match_meeting(self.sources, "2026"))


class RegistrarTests(unittest.TestCase):
    """Self-service binding, with the roster still the boundary.

    First-come-first-served was chosen with its cost stated: until the
    coordinator revokes, whoever claimed a name sees that person's tasks. These
    pin the two things that limit the blast radius -- a claim can only ever
    land on somebody already on the roster, and the undo is reachable.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.initialize()

        extraction = root / "meeting.json"
        transcript = root / "meeting.txt"
        extraction.write_text(
            json.dumps(
                extraction_for(
                    "请把周报整理出来",
                    "20260309214014-王昱翔的快速会议-逐字稿文本-1.txt",
                ),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        transcript.write_text("主持人(00:01:00): 请把周报整理出来\n", encoding="utf-8")
        self.service = load_meeting_service(
            self.db,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="注册测试团队",
            coordinator_name="王昱翔",
            participant_names=["王昱翔", "黄Z恒", "Jasmine"],
        )
        self.source = episode_registry.register(
            self.db,
            episode_id=self.service.episode_id,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="注册测试团队",
            coordinator_name="王昱翔",
            participant_names=["王昱翔", "黄Z恒", "Jasmine"],
            timezone="Australia/Sydney",
            sim_time=self.service.now(),
            title=episode_registry.title_from_extraction(extraction),
        )
        self.transport = RecordingTransport()
        self.im = FeishuIM(self.db, self.transport)
        self.registrar = Registrar(self.db, self.im, base_url="http://127.0.0.1:8766")
        self.coordinator_actor = self.service.aggregator_actor_id

    def bind_coordinator(self) -> None:
        self.im.bind_actor(
            self.coordinator_actor,
            "ou_lead",
            display_name="王昱翔",
            sim_time=self.service.now(),
        )

    def register(self, open_id: str, text: str) -> dict:
        return self.registrar.handle_message(
            open_id=open_id, text=text, sim_time=self.service.now()
        )

    # ---- the happy path ------------------------------------------------

    def test_naming_a_meeting_and_a_roster_name_binds(self) -> None:
        outcome = self.register("ou_heng", "@机器人 快速会议 黄Z恒")

        self.assertTrue(outcome["bound"])
        self.assertEqual(self.im.actor_for_open_id("ou_heng"), outcome["actor_id"])

    def test_the_reply_carries_the_link_to_their_own_tasks(self) -> None:
        """The point of registering is to be able to do something next."""

        outcome = self.register("ou_heng", "@机器人 快速会议 黄Z恒")
        body = json.dumps(outcome["card"], ensure_ascii=False)

        self.assertIn(f"http://127.0.0.1:8766/{self.source.slug}/tasks", body)

    def test_the_coordinator_is_told_with_the_undo_attached(self) -> None:
        self.bind_coordinator()

        outcome = self.register("ou_heng", "@机器人 快速会议 黄Z恒")

        self.assertEqual(len(outcome["notify"]), 1)
        open_id, card, _ = outcome["notify"][0]
        self.assertEqual(open_id, "ou_lead")
        self.assertIn(
            REGISTRATION_REVOKE_ACTION, json.dumps(card, ensure_ascii=False)
        )

    def test_a_coordinator_registering_themselves_is_not_told_about_it(self) -> None:
        outcome = self.register("ou_lead", "@机器人 快速会议 王昱翔")

        self.assertTrue(outcome["bound"])
        self.assertEqual(outcome["notify"], [])

    # ---- the boundary --------------------------------------------------

    def test_a_name_not_on_the_roster_is_refused_not_created(self) -> None:
        """The roster is the authorization boundary, so a self-service path
        that could enrol people would make the boundary self-service too."""

        before = self.db.one(
            "SELECT COUNT(*) AS n FROM episode_participants WHERE episode_id = ?",
            (self.service.episode_id,),
        )["n"]

        outcome = self.register("ou_stranger", "@机器人 快速会议 路人甲")

        self.assertFalse(outcome["bound"])
        self.assertIsNone(self.im.actor_for_open_id("ou_stranger"))
        self.assertEqual(
            self.db.one(
                "SELECT COUNT(*) AS n FROM episode_participants WHERE episode_id = ?",
                (self.service.episode_id,),
            )["n"],
            before,
        )

    def test_somebody_in_another_meeting_cannot_be_claimed_through_this_one(
        self,
    ) -> None:
        """`actors` holds everyone the organisation ever had; only this
        meeting's roster may be claimed against."""

        with self.db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO actors("
                "actor_id, organization_id, display_name, actor_type, status"
                ") SELECT 'actor_outsider', organization_id, '局外人', "
                "'HUMAN', 'ACTIVE' FROM episodes WHERE episode_id = ?",
                (self.service.episode_id,),
            )

        outcome = self.register("ou_out", "@机器人 快速会议 局外人")

        self.assertFalse(outcome["bound"])

    def test_a_name_already_claimed_is_not_handed_over(self) -> None:
        self.register("ou_heng", "@机器人 快速会议 黄Z恒")

        outcome = self.register("ou_impostor", "@机器人 快速会议 黄Z恒")

        self.assertFalse(outcome["bound"])
        self.assertEqual(
            self.im.open_id_for(self.im.actor_for_open_id("ou_heng")), "ou_heng"
        )

    def test_registering_twice_is_answered_not_refused(self) -> None:
        self.register("ou_heng", "@机器人 快速会议 黄Z恒")

        outcome = self.register("ou_heng", "@机器人 快速会议 黄Z恒")

        self.assertFalse(outcome["bound"])
        self.assertIn("已经", json.dumps(outcome["card"], ensure_ascii=False))

    def test_an_unusable_message_lists_the_meetings(self) -> None:
        """A refusal a person cannot act on is the dead end this replaced."""

        outcome = self.register("ou_new", "@机器人 你好")
        body = json.dumps(outcome["card"], ensure_ascii=False)

        self.assertFalse(outcome["bound"])
        self.assertIn("快速会议", body)

    # ---- the undo ------------------------------------------------------

    def test_the_coordinator_can_revoke(self) -> None:
        self.bind_coordinator()
        outcome = self.register("ou_heng", "@机器人 快速会议 黄Z恒")

        result = self.registrar.revoke(
            actor_id=outcome["actor_id"], by_open_id="ou_lead"
        )

        self.assertTrue(result["revoked"])
        self.assertIsNone(self.im.actor_for_open_id("ou_heng"))

    def test_a_revoked_name_can_be_claimed_again(self) -> None:
        """Otherwise revoking a mistaken claim would lock out the real person
        too, and the undo would cost more than the mistake."""

        self.bind_coordinator()
        first = self.register("ou_wrong", "@机器人 快速会议 黄Z恒")
        self.registrar.revoke(actor_id=first["actor_id"], by_open_id="ou_lead")

        second = self.register("ou_right", "@机器人 快速会议 黄Z恒")

        self.assertTrue(second["bound"])
        self.assertEqual(self.im.actor_for_open_id("ou_right"), first["actor_id"])

    def test_a_participant_cannot_revoke_somebody_elses_binding(self) -> None:
        """The undo travels on a card, and a card can be forwarded."""

        self.bind_coordinator()
        outcome = self.register("ou_heng", "@机器人 快速会议 黄Z恒")
        self.register("ou_jas", "@机器人 快速会议 Jasmine")

        with self.assertRaises(PermissionError):
            self.registrar.revoke(
                actor_id=outcome["actor_id"], by_open_id="ou_jas"
            )

    def test_a_stranger_cannot_revoke_anybody(self) -> None:
        outcome = self.register("ou_heng", "@机器人 快速会议 黄Z恒")

        with self.assertRaises(PermissionError):
            self.registrar.revoke(
                actor_id=outcome["actor_id"], by_open_id="ou_nobody"
            )


if __name__ == "__main__":
    unittest.main()
