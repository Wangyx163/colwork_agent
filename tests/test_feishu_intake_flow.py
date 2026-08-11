from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from collab_agent import episode_registry
from collab_agent.feishu_im import FeishuIM
from collab_agent.feishu_intake_flow import (
    IntakeError,
    MeetingIntake,
    build_roster_card,
    intake_id_for,
)
from collab_agent.intake_cache import IntakeCache
from collab_agent.models import stable_hash
from collab_agent.store import Database


TRANSCRIPT = (
    "王昱翔(00:01:00): 这周要把周报整理出来\n"
    "黄Z恒(00:02:00): 我来做数据那块\n"
)


class FakeMinutes:
    """Stands in for the tenant. Returns what the real transport returns."""

    def __init__(self, transcript: str, members: list[dict]) -> None:
        self.transcript = transcript
        self.members = members
        self.calls = 0

    def get_transcript(self, minute_token: str, *, file_format: str = "srt") -> str:
        self.calls += 1
        return self.transcript

    def get_chat_members(self, chat_id: str) -> list[dict]:
        return list(self.members)


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_message(self, **kwargs) -> tuple[str, dict]:
        self.sent.append(kwargs)
        return f"om_{len(self.sent)}", {}

    def update_card(self, **kwargs) -> None:
        self.sent.append({"update": kwargs})


class RosterCardTests(unittest.TestCase):
    """All three buckets are shown because they mean different things and only
    a person knows which applies."""

    def test_every_bucket_reaches_the_reader(self) -> None:
        card = build_roster_card(
            intake_id="intake_x",
            title="两行",
            roster={
                "spoke_and_in_chat": [{"name": "王昱翔", "open_id": "ou_a"}],
                "spoke_but_not_in_chat": ["外部嘉宾"],
                "in_chat_but_silent": ["绒"],
            },
        )
        body = json.dumps(card, ensure_ascii=False)

        self.assertIn("王昱翔", body)
        self.assertIn("外部嘉宾", body)
        self.assertIn("绒", body)

    def test_it_says_who_decides(self) -> None:
        """The roster is the authorization boundary, and a card that just
        listed names would read as the system having already decided."""

        card = build_roster_card(
            intake_id="intake_x",
            title="两行",
            roster={"spoke_and_in_chat": [{"name": "王昱翔", "open_id": "ou_a"}]},
        )

        self.assertIn("不从逐字稿猜", json.dumps(card, ensure_ascii=False))

    def test_with_nobody_silent_there_is_only_one_button(self) -> None:
        card = build_roster_card(
            intake_id="intake_x",
            title="两行",
            roster={"spoke_and_in_chat": [{"name": "王昱翔", "open_id": "ou_a"}]},
        )
        actions = card["elements"][1]["actions"]

        self.assertEqual(len(actions), 1)


class MeetingIntakeTests(unittest.TestCase):
    """A 妙记 link to a served meeting, without a person in the middle.

    The two slow steps -- a model call and a human reading a card -- are slow
    for unrelated reasons and neither needs the other's output, so they run at
    once and whichever finishes second imports. These pin that the race settles
    to exactly one import, and that the roster is still nobody's inference.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.initialize()

        self.cache = IntakeCache(
            extractions_dir=self.root / "extractions",
            transcripts_dir=self.root / "transcripts",
        )
        (self.root / "extractions").mkdir()
        self.minutes = FakeMinutes(
            TRANSCRIPT,
            [
                {"name": "王昱翔", "open_id": "ou_lead"},
                {"name": "黄Z恒", "open_id": "ou_heng"},
                {"name": "绒", "open_id": "ou_rong"},
            ],
        )
        self.im = FeishuIM(self.db, RecordingTransport())
        self.im.bind_actor(
            "actor_lead", "ou_lead", display_name="王昱翔", sim_time="2026-08-12T09:00:00+10:00"
        )
        self.extracted: list[Path] = []
        self.intake = MeetingIntake(
            self.db,
            transport=self.minutes,
            organization_name="导入测试团队",
            cache=self.cache,
            extract=self.fake_extract,
            mode="live",
            base_url="http://127.0.0.1:8766",
        )

    def fake_extract(self, transcript_path: Path, destination: Path) -> None:
        self.extracted.append(destination)
        destination.write_text(
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

    def start(self, token: str = "obcnabc123") -> dict:
        return self.intake.start(
            minute_token=token,
            chat_id="oc_group",
            open_id="ou_lead",
            sim_time="2026-08-12T09:00:00+10:00",
        )

    # ---- the whole chain -----------------------------------------------

    def test_a_link_becomes_a_served_meeting(self) -> None:
        started = self.start()
        self.intake.run_extraction(started["intake_id"], started["transcript"])
        self.intake.confirm_roster(
            intake_id=started["intake_id"], choice="spoke", open_id="ou_lead"
        )

        outcome = self.intake.finish_if_ready(
            started["intake_id"], sim_time="2026-08-12T09:05:00+10:00"
        )

        self.assertEqual(outcome["status"], "READY")
        self.assertTrue(outcome["url"].endswith("/manage"))
        self.assertIsNotNone(
            episode_registry.source_for(self.db, outcome["episode_id"])
        )

    def test_the_roster_confirmation_may_arrive_before_the_extraction(self) -> None:
        """The whole point of running them at once: either order works."""

        started = self.start()
        self.intake.confirm_roster(
            intake_id=started["intake_id"], choice="spoke", open_id="ou_lead"
        )
        self.assertEqual(
            self.intake.finish_if_ready(
                started["intake_id"], sim_time="2026-08-12T09:05:00+10:00"
            )["status"],
            "WAITING",
        )

        self.intake.run_extraction(started["intake_id"], started["transcript"])
        outcome = self.intake.finish_if_ready(
            started["intake_id"], sim_time="2026-08-12T09:05:00+10:00"
        )

        self.assertEqual(outcome["status"], "READY")

    def test_both_halves_calling_finish_import_once(self) -> None:
        """Claimed with a conditional UPDATE for the same reason the Outbox is:
        two threads both arrive, and exactly one may do the import."""

        started = self.start()
        self.intake.run_extraction(started["intake_id"], started["transcript"])
        self.intake.confirm_roster(
            intake_id=started["intake_id"], choice="spoke", open_id="ou_lead"
        )

        first = self.intake.finish_if_ready(
            started["intake_id"], sim_time="2026-08-12T09:05:00+10:00"
        )
        second = self.intake.finish_if_ready(
            started["intake_id"], sim_time="2026-08-12T09:05:00+10:00"
        )

        self.assertEqual(first["status"], "READY")
        self.assertFalse(first.get("already"))
        self.assertTrue(second.get("already"))
        self.assertEqual(
            self.db.one(
                "SELECT COUNT(*) AS n FROM episodes WHERE episode_id = ?",
                (first["episode_id"],),
            )["n"],
            1,
        )

    def test_the_same_link_twice_is_one_meeting(self) -> None:
        started = self.start()
        self.intake.run_extraction(started["intake_id"], started["transcript"])
        self.intake.confirm_roster(
            intake_id=started["intake_id"], choice="spoke", open_id="ou_lead"
        )
        self.intake.finish_if_ready(
            started["intake_id"], sim_time="2026-08-12T09:05:00+10:00"
        )

        again = self.start()

        self.assertEqual(again["status"], "READY")
        self.assertEqual(again["intake_id"], intake_id_for("obcnabc123"))

    # ---- what it will not decide ---------------------------------------

    def test_it_never_promotes_speakers_into_a_roster_by_itself(self) -> None:
        """Nothing is imported until a person picks, which is what makes the
        roster an authorization boundary rather than a transcript artefact."""

        started = self.start()
        self.intake.run_extraction(started["intake_id"], started["transcript"])

        self.assertEqual(
            self.intake.finish_if_ready(
                started["intake_id"], sim_time="2026-08-12T09:05:00+10:00"
            )["status"],
            "WAITING",
        )
        self.assertEqual(
            self.db.one("SELECT COUNT(*) AS n FROM episodes", ())["n"], 0
        )

    def test_only_whoever_started_it_may_confirm_the_roster(self) -> None:
        """There is no episode yet, so there is no roster to check anybody
        against -- the only defensible authority is that they started it."""

        started = self.start()

        with self.assertRaises(PermissionError):
            self.intake.confirm_roster(
                intake_id=started["intake_id"], choice="spoke", open_id="ou_heng"
            )

    def test_the_silent_bucket_is_opt_in(self) -> None:
        started = self.start()

        chosen = self.intake.confirm_roster(
            intake_id=started["intake_id"], choice="all", open_id="ou_lead"
        )

        self.assertIn("绒", chosen["names"])

    def test_the_coordinator_is_always_in_their_own_meeting(self) -> None:
        """Leaving them out produced a meeting whose coordinator could not open
        their own console."""

        self.minutes.transcript = "黄Z恒(00:02:00): 我来做数据那块\n"
        started = self.intake.start(
            minute_token="obcnother",
            chat_id="oc_group",
            open_id="ou_lead",
            sim_time="2026-08-12T09:00:00+10:00",
        )

        chosen = self.intake.confirm_roster(
            intake_id=started["intake_id"], choice="spoke", open_id="ou_lead"
        )

        self.assertIn("王昱翔", chosen["names"])

    def test_an_unregistered_person_is_told_to_register_first(self) -> None:
        """Their display name becomes the coordinator's name, so there is no
        meeting to make without knowing who they are."""

        with self.assertRaises(IntakeError):
            self.intake.start(
                minute_token="obcnabc123",
                chat_id="oc_group",
                open_id="ou_stranger",
                sim_time="2026-08-12T09:00:00+10:00",
            )

    def test_an_empty_transcript_is_refused_with_the_reason(self) -> None:
        self.minutes.transcript = "   "

        with self.assertRaises(IntakeError):
            self.start(token="obcnempty")

    # ---- the cache -----------------------------------------------------

    def test_cache_mode_never_calls_the_extractor(self) -> None:
        """The demo runs on this, and a demo that sometimes calls a model is
        not a deterministic demo."""

        self.intake.mode = "cache"
        started = self.start()

        outcome = self.intake.run_extraction(
            started["intake_id"], started["transcript"]
        )

        self.assertEqual(outcome["status"], "FAILED")
        self.assertEqual(self.extracted, [])

    def test_a_cached_extraction_is_reused_in_cache_mode(self) -> None:
        (self.cache.extractions_dir / "prepared.json").write_text(
            json.dumps(
                {"input_sha256": stable_hash(TRANSCRIPT), "action_items": []},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.intake.mode = "cache"
        started = self.start()

        outcome = self.intake.run_extraction(
            started["intake_id"], started["transcript"]
        )

        self.assertEqual(outcome["status"], "EXTRACTED")
        self.assertEqual(self.extracted, [])

    def test_a_failed_extraction_is_recorded_rather_than_lost(self) -> None:
        """It runs on a worker thread, and a traceback in a log the coordinator
        cannot see is the same as silence."""

        self.intake.mode = "cache"
        started = self.start()
        self.intake.run_extraction(started["intake_id"], started["transcript"])

        row = self.intake.row(started["intake_id"])
        assert row is not None
        self.assertEqual(row["status"], "FAILED")
        self.assertTrue(row["error"])


if __name__ == "__main__":
    unittest.main()
