from __future__ import annotations

import unittest

from collab_agent.compound_store import (
    create_compound_task,
    load,
    project,
    skip_member,
    submit_input,
)
from collab_agent.compound_tasks import Stage
from collab_agent.store import Database


MEMBERS = ["a1", "a2", "a3", "a4", "a5"]
OWNER = "a3"
SPAN = "会议 12:04 我们各自出题，老三汇总"
WHEN = "2026-08-11T10:00:00+00:00"


class CompoundSkipTests(unittest.TestCase):
    """Moving a stage on without somebody who never delivered.

    Waiting for everybody is the rule that keeps the round traceable: a
    shortlist assembled from four of five people's questions is missing one,
    and nothing downstream can tell. So the way past it is a decision with an
    author and a reason -- not a timeout, which would move the stage on with
    nobody accountable for the gap.
    """

    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.counter = 0
        with self.db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO organizations VALUES (?, ?, ?, ?)",
                ("org1", "选题会团队", "ACTIVE", WHEN),
            )
            for actor_id in MEMBERS:
                cursor.execute(
                    "INSERT INTO actors VALUES (?, ?, ?, ?, ?)",
                    (actor_id, "org1", actor_id.upper(), "HUMAN", "ACTIVE"),
                )
            cursor.execute(
                "INSERT INTO episodes(episode_id, organization_id, run_id, "
                "content_pack_id, owner_actor_id, status, transcript, "
                "current_sim_time, created_sim_time, evaluation_cutoff_sim_time) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("ep1", "org1", "run1", "pack1", OWNER, "ACTIVE", SPAN, WHEN, WHEN, WHEN),
            )
        self.task_id = create_compound_task(
            self.db,
            run_id="run1",
            episode_id="ep1",
            kind="VOTE",
            title="面试题清单",
            body="每人先写几道",
            owner_actor_id=OWNER,
            member_actor_ids=list(MEMBERS),
            source_span=SPAN,
            selection_count=3,
            sim_time=WHEN,
            message_id=self.message(),
        )["compound_task_id"]

    def message(self) -> str:
        self.counter += 1
        return f"msg_{self.counter}"

    def fill(self, actors: list[str]) -> None:
        for actor_id in actors:
            submit_input(
                self.db,
                self.task_id,
                run_id="run1",
                actor_id=actor_id,
                payload={"options": [f"{actor_id} 的题"]},
                sim_time=WHEN,
                message_id=self.message(),
            )

    def skip(self, target: str, *, by: str = OWNER, reason: str = "已经离职了"):
        return skip_member(
            self.db,
            self.task_id,
            run_id="run1",
            actor_id=by,
            target_actor_id=target,
            reason=reason,
            sim_time=WHEN,
            message_id=self.message(),
        )

    def stage(self) -> str:
        return load(self.db, self.task_id)["stage"]

    # ---- what it does --------------------------------------------------

    def test_the_stage_moves_on_once_the_rest_have_answered(self) -> None:
        self.fill([a for a in MEMBERS if a != "a5"])
        self.assertEqual(self.stage(), Stage.COLLECTING)

        result = self.skip("a5")

        self.assertTrue(result["stage_complete"])
        self.assertEqual(self.stage(), Stage.MERGING)

    def test_skipping_early_does_not_move_the_stage_by_itself(self) -> None:
        """It settles one person, not the round."""

        self.skip("a5")

        self.assertEqual(self.stage(), Stage.COLLECTING)

    def test_who_was_skipped_and_why_survives_into_the_record(self) -> None:
        """The whole difference between this and a timeout."""

        self.fill([a for a in MEMBERS if a != "a5"])
        self.skip("a5", reason="出差到下周")

        events = [
            row["event_type"]
            for row in self.db.all(
                "SELECT event_type FROM audit_events WHERE aggregate_id = ?",
                (self.task_id,),
            )
        ]
        self.assertIn("CompoundTaskMemberSkipped", events)
        projected = next(
            task for task in project(self.db, "ep1", actor_id=OWNER)
            if task["compound_task_id"] == self.task_id
        )
        self.assertEqual(
            [(s["actor_id"], s["reason"]) for s in projected["skipped"]],
            [("a5", "出差到下周")],
        )

    def test_answering_late_undoes_the_skip(self) -> None:
        """Somebody who delivers has delivered, and leaving the skip standing
        would keep a record saying they did not."""

        self.skip("a5")
        self.fill(["a5"])

        projected = next(
            task for task in project(self.db, "ep1", actor_id=OWNER)
            if task["compound_task_id"] == self.task_id
        )
        self.assertEqual(projected["skipped"], [])

    # ---- who may, and when ---------------------------------------------

    def test_only_the_owner_may_skip(self) -> None:
        """Who is missing from a round is a judgement by the person assembling
        it, not by anybody who happens to be on the roster."""

        with self.assertRaises(PermissionError):
            self.skip("a5", by="a1")

    def test_a_skip_needs_a_reason(self) -> None:
        with self.assertRaises(ValueError):
            self.skip("a5", reason="   ")

    def test_somebody_who_already_answered_cannot_be_skipped(self) -> None:
        self.fill(["a5"])

        with self.assertRaises(ValueError):
            self.skip("a5")

    def test_the_owner_cannot_skip_themselves(self) -> None:
        with self.assertRaises(ValueError):
            self.skip(OWNER)

    def test_somebody_outside_the_roster_cannot_be_skipped(self) -> None:
        with self.assertRaises(ValueError):
            self.skip("a9")

    def test_an_owner_stage_is_not_waiting_on_anybody(self) -> None:
        self.fill(MEMBERS)
        self.assertEqual(self.stage(), Stage.MERGING)

        with self.assertRaises(ValueError):
            self.skip("a5")

    def test_a_retry_skips_once(self) -> None:
        message = self.message()
        first = skip_member(
            self.db,
            self.task_id,
            run_id="run1",
            actor_id=OWNER,
            target_actor_id="a5",
            reason="出差",
            sim_time=WHEN,
            message_id=message,
        )
        second = skip_member(
            self.db,
            self.task_id,
            run_id="run1",
            actor_id=OWNER,
            target_actor_id="a5",
            reason="出差",
            sim_time=WHEN,
            message_id=message,
        )

        self.assertEqual(first, second)
        self.assertEqual(
            self.db.one(
                "SELECT COUNT(*) AS n FROM compound_task_skips "
                "WHERE compound_task_id = ?",
                (self.task_id,),
            )["n"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
