from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from collab_agent.feishu_im import FeishuIM
from collab_agent.meeting import load_meeting_service
from collab_agent.models import parse_time
from collab_agent.scheduler import Scheduler, wall_clock_iso
from collab_agent.store import Database


EXTRACTION = {
    "provider": "fixture",
    "model": "deterministic",
    "input_sha256": "b" * 63 + "2",
    "action_items": [
        {
            "title": "整理会议纪要",
            "deliverable": "会议纪要",
            "owner_name": None,
            "deadline_text": None,
            "deadline_iso": None,
            "source_timestamp": "00:01:00",
            "source_quote": "请整理会议纪要",
            "confidence": 0.9,
            "needs_confirmation": True,
            "uncertainties": [],
        }
    ],
}


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_message(self, **kwargs) -> tuple[str, dict]:
        self.sent.append(kwargs)
        return f"om_{len(self.sent)}", {}

    def update_card(self, **kwargs) -> None:
        self.sent.append({"update": kwargs})


class SchedulerTests(unittest.TestCase):
    """Time passing without anybody watching.

    Every part of a reminder already existed -- `evaluate_policy` decides what
    is overdue, `advance_time` moves the clock, the Outbox delivers. What was
    missing is the thing that runs them: `evaluate_policy` was called from the
    evaluation harness and from nowhere in the serving path, so a meeting
    noticed a deadline only if somebody ran the deterministic scenario.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        extraction = root / "extraction.json"
        transcript = root / "transcript.txt"
        extraction.write_text(
            json.dumps(EXTRACTION, ensure_ascii=False), encoding="utf-8"
        )
        transcript.write_text("主持人(00:01:00): 请整理会议纪要\n", encoding="utf-8")
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.initialize()
        self.service = load_meeting_service(
            self.db,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="定时器测试团队",
            coordinator_name="会议负责人",
            participant_names=["同事甲"],
        )

    # ---- the clock -------------------------------------------------------

    def test_a_tick_brings_the_meeting_to_wall_time(self) -> None:
        before = self.service.now()
        scheduler = Scheduler(self.service)

        result = scheduler.tick()

        self.assertTrue(result["advanced"])
        self.assertGreater(parse_time(self.service.now()), parse_time(before))

    def test_the_clock_never_goes_backwards(self) -> None:
        """A clock correction on the host must not rewind a meeting that is
        mid-flight; deadlines that un-pass are worse than deadlines that slip.
        """

        far_future = (
            parse_time(wall_clock_iso()) + timedelta(days=30)
        ).isoformat()
        self.service.advance_time(far_future)
        scheduler = Scheduler(self.service)

        result = scheduler.tick()

        self.assertFalse(result["advanced"])
        self.assertEqual(self.service.now(), far_future)

    def test_a_tick_is_safe_to_repeat(self) -> None:
        scheduler = Scheduler(self.service)
        scheduler.tick()

        second = scheduler.tick()

        self.assertIsInstance(second["decisions"], list)

    def test_it_reports_what_it_did_rather_than_only_logging(self) -> None:
        """So a caller can see a tick did nothing without reading a log."""

        result = Scheduler(self.service).tick()

        self.assertEqual(set(result) >= {"advanced", "from", "to", "decisions"}, True)

    def test_a_broken_policy_pass_does_not_stop_the_clock(self) -> None:
        """One meeting with bad data must not stop every meeting behind it."""

        lines: list[str] = []
        scheduler = Scheduler(self.service, log=lines.append)

        def explode() -> list[dict]:
            raise RuntimeError("policy is broken")

        self.service.evaluate_policy = explode  # type: ignore[method-assign]
        result = scheduler.tick()

        self.assertTrue(result["advanced"])
        self.assertTrue(any("policy evaluation failed" in line for line in lines))

    def test_the_deadline_timezone_is_the_teams(self) -> None:
        """A deadline of "Friday" means Friday where the team is; comparing in
        UTC fires reminders on Thursday evening and teaches people the dates
        are wrong."""

        sydney = wall_clock_iso("Australia/Sydney")
        utc = wall_clock_iso("UTC")

        self.assertNotEqual(sydney[-6:], utc[-6:])

    def test_an_unknown_timezone_still_produces_a_time(self) -> None:
        """A misconfigured zone must degrade to a working clock, not stop it."""

        self.assertTrue(wall_clock_iso("Not/AZone"))


class RedirectTests(unittest.TestCase):
    """Turning reminders on against a live team without spamming it.

    The redirect lives at the delivery boundary, not in the domain: the Outbox
    row and the audit trail keep saying who each message was *for*, and only
    the envelope changes. Rewriting recipients would leave a permanent lie in
    the trail for the sake of a temporary test.
    """

    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.initialize()
        with self.db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO organizations VALUES (?, ?, ?, ?)",
                ("org1", "团队", "ACTIVE", "2026-08-11T10:00:00+00:00"),
            )
            for actor_id, name in (("actor_a", "甲"), ("actor_b", "乙")):
                cursor.execute(
                    "INSERT INTO actors VALUES (?, ?, ?, ?, ?)",
                    (actor_id, "org1", name, "HUMAN", "ACTIVE"),
                )
        self.transport = RecordingTransport()

    def test_without_a_redirect_everybody_gets_their_own_card(self) -> None:
        im = FeishuIM(self.db, self.transport)
        im.bind_actor("actor_a", "ou_a", display_name="甲", sim_time="t")
        im.bind_actor("actor_b", "ou_b", display_name="乙", sim_time="t")

        self.assertEqual(im.open_id_for("actor_b"), "ou_b")

    def test_with_a_redirect_every_card_goes_to_one_person(self) -> None:
        im = FeishuIM(self.db, self.transport, redirect_all_to_open_id="ou_a")
        im.bind_actor("actor_b", "ou_b", display_name="乙", sim_time="t")

        self.assertEqual(im.open_id_for("actor_b"), "ou_a")

    def test_a_redirect_reaches_somebody_who_was_never_bound(self) -> None:
        """Which is the point: the unbound colleagues are exactly the ones
        whose reminders would otherwise vanish."""

        im = FeishuIM(self.db, self.transport, redirect_all_to_open_id="ou_a")

        self.assertEqual(im.open_id_for("actor_never_bound"), "ou_a")

    def test_it_says_so_every_time(self) -> None:
        """A silent redirect is one somebody forgets is on, and then wonders
        why their team never heard anything."""

        lines: list[str] = []
        im = FeishuIM(
            self.db,
            self.transport,
            redirect_all_to_open_id="ou_a",
            log=lines.append,
        )

        im.open_id_for("actor_b")

        self.assertTrue(any("重定向" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
