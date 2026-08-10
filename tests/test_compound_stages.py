from __future__ import annotations

import unittest

from collab_agent.compound_tasks import (
    CompoundKind,
    CompoundTaskError,
    Stage,
    is_complete,
    may_act,
    next_stage,
    role_at,
    stages_for,
)


MEMBERS = ["a1", "a2", "a3", "a4", "a5"]
OWNER = "a3"


class StageOrderTests(unittest.TestCase):
    def test_a_vote_has_a_round_where_everybody_reacts(self) -> None:
        """That round is the whole difference between the two kinds."""

        order = stages_for(CompoundKind.VOTE)

        self.assertEqual(
            order,
            (
                Stage.COLLECTING,
                Stage.MERGING,
                Stage.VOTING,
                Stage.FINALIZING,
                Stage.DONE,
            ),
        )

    def test_a_submission_has_no_voting_round(self) -> None:
        self.assertNotIn(Stage.VOTING, stages_for(CompoundKind.SUBMIT))

    def test_the_turn_alternates(self) -> None:
        """Everybody, then one person, then everybody again.

        The alternation is the structure -- it is what makes the headcount
        drop that identifies the owner, and it is what a participant reads to
        know whether the ball is theirs.
        """

        turns = [
            role_at(stage)
            for stage in stages_for(CompoundKind.VOTE)
            if role_at(stage) != "NOBODY"
        ]

        self.assertEqual(turns, ["EVERYONE", "OWNER", "EVERYONE", "OWNER"])

    def test_advancing_walks_the_order(self) -> None:
        self.assertEqual(next_stage(CompoundKind.VOTE, Stage.COLLECTING), Stage.MERGING)
        self.assertEqual(next_stage(CompoundKind.VOTE, Stage.MERGING), Stage.VOTING)
        self.assertEqual(next_stage(CompoundKind.SUBMIT, Stage.MERGING), Stage.DONE)

    def test_a_finished_task_cannot_advance(self) -> None:
        for stage in (Stage.DONE, Stage.REVOKED):
            with self.assertRaises(CompoundTaskError, msg=stage):
                next_stage(CompoundKind.VOTE, stage)

    def test_a_stage_from_the_other_kind_is_refused(self) -> None:
        """A submit-type task in VOTING is a corrupted row, not a state."""

        with self.assertRaises(CompoundTaskError):
            next_stage(CompoundKind.SUBMIT, Stage.VOTING)

    def test_an_unknown_kind_is_refused(self) -> None:
        with self.assertRaises(CompoundTaskError):
            stages_for("BRAINSTORM")


class WhoMayActTests(unittest.TestCase):
    def test_an_owner_stage_belongs_to_the_owner_alone(self) -> None:
        self.assertTrue(
            may_act(Stage.MERGING, actor_id=OWNER, owner_actor_id=OWNER, members=MEMBERS)
        )
        self.assertFalse(
            may_act(Stage.MERGING, actor_id="a1", owner_actor_id=OWNER, members=MEMBERS)
        )

    def test_an_everyone_stage_still_means_only_the_roster(self) -> None:
        """The roster is the authorisation boundary everywhere else here."""

        self.assertTrue(
            may_act(
                Stage.COLLECTING, actor_id="a1", owner_actor_id=OWNER, members=MEMBERS
            )
        )
        self.assertFalse(
            may_act(
                Stage.COLLECTING,
                actor_id="outsider",
                owner_actor_id=OWNER,
                members=MEMBERS,
            )
        )

    def test_nobody_acts_on_a_finished_task(self) -> None:
        for stage in (Stage.DONE, Stage.REVOKED):
            self.assertFalse(
                may_act(
                    stage, actor_id=OWNER, owner_actor_id=OWNER, members=MEMBERS
                ),
                stage,
            )


class CompletionTests(unittest.TestCase):
    def test_everybody_has_to_have_answered(self) -> None:
        """Four of five people's questions is a shortlist missing one, and
        nothing downstream can tell that it is missing."""

        self.assertFalse(
            is_complete(
                Stage.COLLECTING,
                submitted_actor_ids={"a1", "a2", "a3", "a4"},
                members=MEMBERS,
            )
        )
        self.assertTrue(
            is_complete(
                Stage.COLLECTING, submitted_actor_ids=set(MEMBERS), members=MEMBERS
            )
        )

    def test_extra_answers_do_not_block_it(self) -> None:
        """Somebody who left the roster after answering should not hold it."""

        self.assertTrue(
            is_complete(
                Stage.VOTING,
                submitted_actor_ids=set(MEMBERS) | {"departed"},
                members=MEMBERS,
            )
        )

    def test_an_owner_stage_is_never_complete_by_headcount(self) -> None:
        """It finishes when the owner says so, not when a set fills up."""

        self.assertFalse(
            is_complete(
                Stage.MERGING, submitted_actor_ids=set(MEMBERS), members=MEMBERS
            )
        )


if __name__ == "__main__":
    unittest.main()
