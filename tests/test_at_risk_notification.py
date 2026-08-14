from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from collab_agent.meeting import load_meeting_service
from collab_agent.models import parse_time
from collab_agent.service import NOTIFY_TASK_AT_RISK
from collab_agent.store import Database


EXTRACTION = {
    "provider": "fixture",
    "model": "deterministic",
    "input_sha256": "d" * 64,
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


class AtRiskNotificationTests(unittest.TestCase):
    """A warning nobody receives is a label, not a warning.

    `AT_RISK` existed as a signal type from the start: it was recorded, it
    appeared on the task card, and it told nobody. Which meant only somebody
    already looking at that task ever learned the task was in trouble -- while
    the person who can act on it, the coordinator, works on a different page.
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
            organization_name="预警测试团队",
            coordinator_name="会议负责人",
            participant_names=["同事甲", "同事乙"],
        )
        self.coordinator = self.service.aggregator_actor_id
        self.actors = {
            row["display_name"]: row["actor_id"]
            for row in self.db.all(
                "SELECT a.actor_id, a.display_name FROM actors a "
                "JOIN episode_participants ep ON ep.actor_id = a.actor_id "
                "WHERE ep.episode_id = ? AND ep.role = 'PARTICIPANT'",
                (self.service.episode_id,),
            )
        }
        self.action_id = next(iter(self.service.action_items()))["action_item_id"]
        later = (parse_time(self.service.now()) + timedelta(days=3)).isoformat()
        self.service.revise_action_proposal(
            self.action_id,
            actor_id=self.coordinator,
            title="整理会议纪要",
            deliverable="会议纪要",
            acceptance_criteria="结论清晰",
            priority="P1",
            team_required_by_sim_time=later,
            message_id="prepare",
        )
        self.service.dispatch_action(
            self.action_id,
            actor_id=self.coordinator,
            owner_actor_id=self.actors["同事甲"],
            collaborator_actor_ids=[self.actors["同事乙"]],
            assignment_message="",
            message_id="dispatch",
        )
        for name in ("同事甲", "同事乙"):
            self.service.respond_to_assignment(
                self.action_id,
                actor_id=self.actors[name],
                decision="ACCEPT",
                response_message="",
                message_id=f"accept-{name}",
            )

    def signal(self, kind: str, *, note: str = "", tag: str = "s1", by: str = "同事甲"):
        return self.service.record_progress_signal(
            self.action_id,
            actor_id=self.actors[by],
            signal_type=kind,
            valid_until=None,
            note=note,
            message_id=f"signal-{tag}",
        )

    def notices(self) -> list[dict]:
        return [
            json.loads(row["payload"])
            for row in self.db.all(
                "SELECT payload FROM outbox_entries WHERE effect_type = ? "
                "ORDER BY created_sim_time",
                (NOTIFY_TASK_AT_RISK,),
            )
        ]

    def recipients(self) -> set[str]:
        found: set[str] = set()
        for payload in self.notices():
            found.update(payload.get("recipient_actor_ids") or [])
        return found

    # ---- it reaches somebody -------------------------------------------

    def test_at_risk_reaches_the_coordinator(self) -> None:
        self.signal("AT_RISK", note="等设计稿,可能拖到下周")

        self.assertEqual(self.recipients(), {self.coordinator})

    def test_blocked_reaches_the_coordinator(self) -> None:
        self.signal("BLOCKED", note="拿不到权限")

        self.assertEqual(self.recipients(), {self.coordinator})

    def test_the_note_travels_with_it(self) -> None:
        """Without it the coordinator learns there is a problem and nothing
        about what it is, which is a notification that creates work."""

        self.signal("AT_RISK", note="等设计稿,可能拖到下周")

        self.assertIn(
            "等设计稿", json.dumps(self.notices(), ensure_ascii=False)
        )

    # ---- it stays quiet where it should --------------------------------

    def test_on_track_tells_nobody(self) -> None:
        """The expected path is not news. A notification per heartbeat is how
        a bell becomes background noise."""

        self.signal("ON_TRACK")

        self.assertEqual(self.notices(), [])

    def test_ready_to_submit_tells_nobody(self) -> None:
        self.signal("READY_TO_SUBMIT")

        self.assertEqual(self.notices(), [])

    def test_the_reporter_is_not_told_about_their_own_signal(self) -> None:
        """The coordinator flagging their own task should not ring their own
        bell."""

        self.service.record_progress_signal(
            self.action_id,
            actor_id=self.actors["同事甲"],
            signal_type="AT_RISK",
            valid_until=None,
            note="我自己报的",
            message_id="self-signal",
        )

        self.assertNotIn(self.actors["同事甲"], self.recipients())

    # ---- repeats ---------------------------------------------------------

    def test_pressing_the_same_button_twice_sends_one_notice(self) -> None:
        self.signal("AT_RISK", note="等设计稿", tag="a")
        self.signal("AT_RISK", note="等设计稿", tag="b")

        self.assertEqual(len(self.notices()), 1)

    def test_flagging_again_with_new_information_gets_through(self) -> None:
        """Somebody who comes back with "now it is worse" is saying something
        new, and a notice keyed only on the signal type would swallow it."""

        self.signal("AT_RISK", note="等设计稿", tag="a")
        self.signal("AT_RISK", note="设计稿黄了,要重做", tag="b")

        self.assertEqual(len(self.notices()), 2)

    def test_a_collaborator_may_raise_it_too(self) -> None:
        """Trouble is not the owner's exclusive knowledge."""

        self.signal("BLOCKED", note="我这边也卡了", by="同事乙", tag="c")

        self.assertEqual(self.recipients(), {self.coordinator})


if __name__ == "__main__":
    unittest.main()
