from __future__ import annotations

import unittest

from collab_agent.compound_store import (
    create_compound_task,
    finish_owner_stage,
    load,
    project,
    revoke,
    submit_input,
)
from collab_agent.compound_tasks import CompoundTaskError, Stage
from collab_agent.store import Database


MEMBERS = ["a1", "a2", "a3", "a4", "a5"]
OWNER = "a3"
SPAN = "会议 12:04 我们各自出题，老三汇总"


class CompoundStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.db.initialize()
        self.counter = 0
        when = "2026-08-11T10:00:00+00:00"
        with self.db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO organizations VALUES (?, ?, ?, ?)",
                ("org1", "选题会团队", "ACTIVE", when),
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
                ("ep1", "org1", "run1", "pack1", OWNER, "ACTIVE", SPAN, when, when, when),
            )
        self.addCleanup(self.db.close)

    def message(self) -> str:
        self.counter += 1
        return f"msg_{self.counter}"

    def create(self, **overrides: object) -> str:
        payload = {
            "kind": "VOTE",
            "title": "面试题清单",
            "body": "每人先写几道",
            "owner_actor_id": OWNER,
            "member_actor_ids": list(MEMBERS),
            "source_span": SPAN,
            "selection_count": 3,
        }
        payload.update(overrides)
        return create_compound_task(
            self.db,
            run_id="run1",
            episode_id="ep1",
            sim_time="2026-08-11T10:00:00+00:00",
            message_id=self.message(),
            **payload,  # type: ignore[arg-type]
        )["compound_task_id"]

    def fill(self, task_id: str, actors: list[str], **payload: object) -> dict:
        result: dict = {}
        for actor_id in actors:
            result = submit_input(
                self.db,
                task_id,
                run_id="run1",
                actor_id=actor_id,
                payload=dict(payload) or {"options": [f"{actor_id} 的题"]},
                sim_time="2026-08-11T11:00:00+00:00",
                message_id=self.message(),
            )
        return result

    def run_to_voting(self) -> str:
        task_id = self.create(selection_count=2)
        self.fill(task_id, MEMBERS)
        finish_owner_stage(
            self.db,
            task_id,
            run_id="run1",
            actor_id=OWNER,
            payload={"options": ["一", "二", "三", "四"]},
            sim_time="2026-08-11T12:00:00+00:00",
            message_id=self.message(),
        )
        return task_id


class DeclaringTests(CompoundStoreTestCase):
    def test_it_starts_with_everybody_owing_something(self) -> None:
        task = load(self.db, self.create())

        self.assertEqual(task["stage"], Stage.COLLECTING)
        self.assertEqual(task["member_actor_ids"], MEMBERS)

    def test_the_owner_has_to_be_one_of_the_members(self) -> None:
        """Merging without having contributed is how a summary drifts."""

        with self.assertRaises(CompoundTaskError):
            self.create(owner_actor_id="a9")

    def test_it_needs_a_meeting_source(self) -> None:
        """A shape is something the meeting decided, not a console invention."""

        with self.assertRaises(CompoundTaskError):
            self.create(source_span="   ")

    def test_declaring_twice_with_one_message_makes_one_task(self) -> None:
        message_id = self.message()
        arguments = dict(
            run_id="run1",
            episode_id="ep1",
            kind="SUBMIT",
            title="材料收集",
            body="",
            owner_actor_id=OWNER,
            member_actor_ids=list(MEMBERS),
            source_span=SPAN,
            selection_count=None,
            sim_time="2026-08-11T10:00:00+00:00",
            message_id=message_id,
        )

        first = create_compound_task(self.db, **arguments)
        second = create_compound_task(self.db, **arguments)

        self.assertEqual(first, second)
        rows = self.db.all("SELECT compound_task_id FROM compound_tasks")
        self.assertEqual(len(rows), 1)


class TurnTakingTests(CompoundStoreTestCase):
    def test_the_stage_moves_only_when_the_last_person_answers(self) -> None:
        task_id = self.create()

        four = self.fill(task_id, MEMBERS[:4])
        self.assertFalse(four["stage_complete"])
        self.assertEqual(load(self.db, task_id)["stage"], Stage.COLLECTING)

        five = self.fill(task_id, MEMBERS[4:])
        self.assertTrue(five["stage_complete"])
        self.assertEqual(load(self.db, task_id)["stage"], Stage.MERGING)

    def test_answering_again_replaces_rather_than_adds(self) -> None:
        """A second list from one person would double their say in the merge."""

        task_id = self.create()
        self.fill(task_id, ["a1"], options=["第一版"])
        self.fill(task_id, ["a1"], options=["改过的一版", "又想到一条"])

        rows = self.db.all(
            "SELECT payload FROM compound_task_inputs WHERE actor_id = ?", ("a1",)
        )
        self.assertEqual(len(rows), 1)
        self.assertIn("又想到一条", dict(rows[0])["payload"])

    def test_a_bystander_cannot_answer(self) -> None:
        task_id = self.create()

        with self.assertRaises(PermissionError):
            self.fill(task_id, ["outsider"])

    def test_nobody_but_the_owner_finishes_an_owner_stage(self) -> None:
        task_id = self.create()
        self.fill(task_id, MEMBERS)

        with self.assertRaises(PermissionError):
            finish_owner_stage(
                self.db,
                task_id,
                run_id="run1",
                actor_id="a1",
                payload={"options": ["一", "二", "三", "四"]},
                sim_time="2026-08-11T12:00:00+00:00",
                message_id=self.message(),
            )

    def test_a_member_cannot_answer_during_an_owner_stage(self) -> None:
        """Otherwise a late list lands after the merge that was meant to hold it."""

        task_id = self.create()
        self.fill(task_id, MEMBERS)

        with self.assertRaises((PermissionError, CompoundTaskError)):
            self.fill(task_id, ["a1"], options=["再补一条"])


class ValidationTests(CompoundStoreTestCase):
    def test_an_empty_list_of_options_is_refused(self) -> None:
        task_id = self.create()

        with self.assertRaises(CompoundTaskError):
            self.fill(task_id, ["a1"], options=["  ", ""])

    def test_a_shortlist_shorter_than_the_cut_is_refused(self) -> None:
        """Scoring a list you must take whole decides nothing."""

        task_id = self.create(selection_count=3)
        self.fill(task_id, MEMBERS)

        with self.assertRaises(CompoundTaskError):
            finish_owner_stage(
                self.db,
                task_id,
                run_id="run1",
                actor_id=OWNER,
                payload={"options": ["一", "二", "三"]},
                sim_time="2026-08-11T12:00:00+00:00",
                message_id=self.message(),
            )

    def test_a_score_outside_the_range_is_refused(self) -> None:
        task_id = self.run_to_voting()

        with self.assertRaises(CompoundTaskError):
            self.fill(task_id, ["a1"], scores={"0": 9})



class WholeRunTests(CompoundStoreTestCase):
    def test_a_vote_runs_all_four_stages_and_ends_done(self) -> None:
        task_id = self.run_to_voting()
        self.assertEqual(load(self.db, task_id)["stage"], Stage.VOTING)

        for index, actor_id in enumerate(MEMBERS):
            submit_input(
                self.db,
                task_id,
                run_id="run1",
                actor_id=actor_id,
                payload={"scores": {"0": 5, "1": 1, "2": 4, "3": 2}},
                sim_time="2026-08-11T13:00:00+00:00",
                message_id=self.message(),
            )
        self.assertEqual(load(self.db, task_id)["stage"], Stage.FINALIZING)

        finish_owner_stage(
            self.db,
            task_id,
            run_id="run1",
            actor_id=OWNER,
            payload={"remark": "按分数留前两条"},
            sim_time="2026-08-11T14:00:00+00:00",
            message_id=self.message(),
        )
        self.assertEqual(load(self.db, task_id)["stage"], Stage.DONE)

    def test_a_submission_skips_the_voting_round(self) -> None:
        task_id = self.create(kind="SUBMIT", selection_count=None)
        self.fill(task_id, MEMBERS, content="我这边的材料")

        self.assertEqual(load(self.db, task_id)["stage"], Stage.MERGING)
        finish_owner_stage(
            self.db,
            task_id,
            run_id="run1",
            actor_id=OWNER,
            payload={"content": "汇总稿"},
            sim_time="2026-08-11T12:00:00+00:00",
            message_id=self.message(),
        )
        self.assertEqual(load(self.db, task_id)["stage"], Stage.DONE)

    def test_every_move_leaves_an_audit_event(self) -> None:
        """The same expectation the ordinary domain is held to."""

        task_id = self.run_to_voting()

        events = [
            dict(row)["event_type"]
            for row in self.db.all(
                "SELECT event_type FROM audit_events WHERE aggregate_id = ? "
                "ORDER BY sequence_no",
                (task_id,),
            )
        ]
        self.assertEqual(events[0], "CompoundTaskDeclared")
        self.assertIn("CompoundTaskStageEntered", events)
        self.assertEqual(events.count("CompoundTaskInputRecorded"), len(MEMBERS))

    def test_revoking_needs_a_reason_and_stops_the_task(self) -> None:
        task_id = self.create()

        with self.assertRaises(CompoundTaskError):
            revoke(
                self.db,
                task_id,
                run_id="run1",
                actor_id=OWNER,
                reason="",
                sim_time="2026-08-11T12:00:00+00:00",
                message_id=self.message(),
            )
        revoke(
            self.db,
            task_id,
            run_id="run1",
            actor_id=OWNER,
            reason="人拉错了",
            sim_time="2026-08-11T12:00:00+00:00",
            message_id=self.message(),
        )

        self.assertEqual(load(self.db, task_id)["stage"], Stage.REVOKED)
        with self.assertRaises(PermissionError):
            self.fill(task_id, ["a1"])


class ProjectionTests(CompoundStoreTestCase):
    def test_it_says_whether_the_ball_is_yours(self) -> None:
        task_id = self.create()
        self.fill(task_id, ["a1"])

        seen = {
            actor_id: project(self.db, "ep1", actor_id=actor_id)[0]["my_turn"]
            for actor_id in ("a1", "a2")
        }

        self.assertFalse(seen["a1"], "a1 已经填过了")
        self.assertTrue(seen["a2"])
        self.assertEqual(str(task_id), task_id)

    def test_nobody_sees_what_others_wrote_before_writing_their_own(self) -> None:
        """Five lists become one list written five times otherwise."""

        task_id = self.create()
        self.fill(task_id, ["a1"], options=["a1 想到的那条"])

        mine = project(self.db, "ep1", actor_id="a2")[0]

        self.assertEqual(mine["collected"], [])
        self.assertNotIn("a1 想到的那条", str(mine))
        self.assertEqual(mine["answered_count"], 1)

    def test_the_owner_sees_everything_once_it_is_their_turn(self) -> None:
        task_id = self.create()
        self.fill(task_id, MEMBERS)

        theirs = project(self.db, "ep1", actor_id=OWNER)[0]

        self.assertEqual(len(theirs["collected"]), len(MEMBERS))
        self.assertTrue(theirs["my_turn"])

    def test_the_ranking_shows_what_lost_as_well_as_what_won(self) -> None:
        """Which options came close is how somebody judges the cut."""

        task_id = self.run_to_voting()
        for actor_id in MEMBERS:
            submit_input(
                self.db,
                task_id,
                run_id="run1",
                actor_id=actor_id,
                payload={"scores": {"0": 5, "1": 1, "2": 4, "3": 2}},
                sim_time="2026-08-11T13:00:00+00:00",
                message_id=self.message(),
            )

        result = project(self.db, "ep1", actor_id="a1")[0]["result"]

        self.assertEqual([item["text"] for item in result["selected"]], ["一", "三"])
        self.assertEqual(len(result["ranked"]), 4)
        self.assertEqual(result["ranked"][0]["score_total"], 25)
        self.assertTrue(result["complete"])


class NoticeTests(CompoundStoreTestCase):
    """The bell is the entry point, so a stage nobody is told about is a stage
    that quietly waits forever."""

    def notices(self) -> list[dict]:
        import json

        return [
            {**dict(row), "payload": json.loads(dict(row)["payload"])}
            for row in self.db.all(
                "SELECT effect_type, payload FROM outbox_entries "
                "WHERE effect_type = ? ORDER BY created_sim_time, outbox_id",
                ("COMPOUND_TURN",),
            )
        ]

    def test_declaring_tells_everybody_it_is_their_turn(self) -> None:
        self.create()

        sent = self.notices()

        self.assertEqual(len(sent), 1)
        self.assertEqual(
            sorted(sent[0]["payload"]["recipient_actor_ids"]), sorted(MEMBERS)
        )

    def test_an_owner_stage_tells_only_the_owner(self) -> None:
        """A notice everybody gets on every move is one people stop reading."""

        task_id = self.create()
        self.fill(task_id, MEMBERS)

        self.assertEqual(
            self.notices()[-1]["payload"]["recipient_actor_ids"], [OWNER]
        )

    def test_finishing_notifies_nobody(self) -> None:
        task_id = self.run_to_voting()
        before = len(self.notices())
        for actor_id in MEMBERS:
            submit_input(
                self.db,
                task_id,
                run_id="run1",
                actor_id=actor_id,
                payload={"scores": {"0": 5, "1": 1, "2": 4, "3": 2}},
                sim_time="2026-08-11T13:00:00+00:00",
                message_id=self.message(),
            )
        finish_owner_stage(
            self.db,
            task_id,
            run_id="run1",
            actor_id=OWNER,
            payload={"remark": "留前两条"},
            sim_time="2026-08-11T14:00:00+00:00",
            message_id=self.message(),
        )

        after = self.notices()

        self.assertEqual(after[-1]["payload"]["stage"], "FINALIZING")
        self.assertGreater(len(after), before)

    def test_the_notice_carries_the_meeting_source(self) -> None:
        self.create()

        fields = self.notices()[0]["payload"]["notification"]["fields"]

        self.assertIn(SPAN, [field["value"] for field in fields])

    def test_the_notice_carries_what_an_im_adapter_needs(self) -> None:
        """Reaching the bell is not the same as being deliverable.

        The first version of this notice had a title, a summary and fields, so
        it rendered -- and then the dispatcher died on a missing
        conversation_id, leaving a row the outbox could never drain. Only a
        run that actually delivered found it, so the shape is asserted here.
        """

        self.create()

        payload = self.notices()[0]["payload"]

        for field in ("conversation_id", "sender_actor_id", "content"):
            self.assertIn(field, payload, field)


if __name__ == "__main__":
    unittest.main()
