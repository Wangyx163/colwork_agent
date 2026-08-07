from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from collab_agent.meeting import load_meeting_service
from collab_agent.models import parse_time
from collab_agent.store import Database


def extraction_payload() -> dict:
    def item(title: str, quote: str, timestamp: str) -> dict:
        return {
            "title": title,
            "deliverable": title,
            "owner_name": None,
            "deadline_text": None,
            "deadline_iso": None,
            "source_timestamp": timestamp,
            "source_quote": quote,
            "confidence": 0.94,
            "needs_confirmation": True,
            "uncertainties": ["负责人未明确", "截止时间未明确"],
        }

    return {
        "provider": "fixture",
        "model": "deterministic",
        "input_sha256": "d" * 64,
        "action_items": [
            item("甲准备采访问题", "甲准备七八个采访问题", "00:01:00"),
            item("乙准备采访问题", "乙也准备七八个采访问题", "00:01:10"),
            item(
                "汇总、投票并定稿采访问题",
                "子恒汇总后大家打分投票，最后保留七到八个问题",
                "00:01:20",
            ),
        ],
    }


class P1QuestionVoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        extraction = root / "extraction.json"
        transcript = root / "transcript.txt"
        extraction.write_text(
            json.dumps(extraction_payload(), ensure_ascii=False), encoding="utf-8"
        )
        transcript.write_text(
            "主持人(00:01:00): 甲准备七八个采访问题\n"
            "主持人(00:01:10): 乙也准备七八个采访问题\n"
            "主持人(00:01:20): 子恒汇总后大家打分投票，最后保留七到八个问题\n",
            encoding="utf-8",
        )
        self.db = Database(":memory:")
        self.db.initialize()
        self.service = load_meeting_service(
            self.db,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="P1 测试团队",
            coordinator_name="会议负责人",
            participant_names=["甲", "乙", "子恒"],
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
        self.actions = {
            row["title"]: dict(row) for row in self.service.action_items()
        }
        self.first = self.actions["甲准备采访问题"]
        self.second = self.actions["乙准备采访问题"]
        self.decision = self.actions["汇总、投票并定稿采访问题"]
        self.deadline = (
            parse_time(self.service.now()) + timedelta(days=3)
        ).isoformat()
        for index, action in enumerate(
            (self.first, self.second, self.decision), start=1
        ):
            metadata = self.service.proposal_metadata(action)
            self.service.revise_action_proposal(
                action["action_item_id"],
                actor_id=self.coordinator,
                title=action["title"],
                deliverable=metadata["deliverable"],
                acceptance_criteria="内容清晰且来源可追溯",
                priority="P1",
                team_required_by_sim_time=self.deadline,
                message_id=f"prepare-p1-{index}",
            )

    def tearDown(self) -> None:
        self.db.close()
        self.directory.cleanup()

    def accept(self, action: dict, actor_id: str, suffix: str, content: str) -> str:
        current = self.db.one(
            "SELECT owner_actor_id FROM action_items WHERE action_item_id = ?",
            (action["action_item_id"],),
        )
        self.assertEqual(current["owner_actor_id"], actor_id)
        submitted = self.service.submit_artifact(
            action["action_item_id"],
            actor_id=actor_id,
            message_id=f"submit-{suffix}",
            payload={"summary": content, "content": content},
        )
        self.service.process_task_result_once(processing_mode="local")
        reviewed = self.service.review_artifact(
            submitted["version_id"],
            actor_id=self.coordinator,
            approve=True,
            comment="验收通过",
            message_id=f"review-{suffix}",
        )
        self.assertEqual(reviewed["action_status"], "ACCEPTED")
        return submitted["version_id"]

    def test_question_collection_vote_and_upstream_invalidation(self) -> None:
        confirmed = self.service.confirm_question_vote_structure(
            collection_action_item_ids=[
                self.first["action_item_id"],
                self.second["action_item_id"],
            ],
            decision_action_item_id=self.decision["action_item_id"],
            final_owner_actor_id=self.actors["子恒"],
            voter_actor_ids=[self.actors["甲"], self.actors["乙"]],
            selection_count=2,
            source_span="子恒汇总后大家打分投票，最后保留七到八个问题",
            actor_id=self.coordinator,
            message_id="confirm-question-vote",
        )
        self.assertEqual(confirmed["status"], "CONFIRMED")
        self.assertEqual(len(confirmed["dependency_ids"]), 2)

        for action, owner_actor_id, suffix in (
            (self.first, self.actors["甲"], "first"),
            (self.second, self.actors["乙"], "second"),
            (self.decision, self.actors["子恒"], "decision"),
        ):
            dispatched = self.service.dispatch_action(
                action["action_item_id"],
                actor_id=self.coordinator,
                owner_actor_id=owner_actor_id,
                collaborator_actor_ids=[],
                assignment_message="按会议原话执行",
                message_id=f"dispatch-{suffix}",
            )
            self.assertEqual(dispatched["status"], "PENDING_ASSIGNMENT")
            accepted = self.service.respond_to_assignment(
                action["action_item_id"],
                actor_id=owner_actor_id,
                decision="ACCEPT",
                response_message="接受任务",
                message_id=f"accept-assignment-{suffix}",
            )
            self.assertEqual(accepted["status"], "TRACKING")

        with self.assertRaisesRegex(ValueError, "all collection tasks"):
            self.service.prepare_question_ballot_draft(
                self.decision["action_item_id"],
                actor_id=self.actors["子恒"],
                processing_mode="local",
                message_id="prepare-ballot-draft-too-early",
            )

        first_version = self.accept(
            self.first, self.actors["甲"], "first-v1", "问题甲1、问题甲2"
        )
        self.accept(self.second, self.actors["乙"], "second-v1", "问题乙1、问题乙2")
        draft = self.service.prepare_question_ballot_draft(
            self.decision["action_item_id"],
            actor_id=self.actors["子恒"],
            processing_mode="local",
            message_id="prepare-ballot-draft",
        )
        self.assertEqual(draft["status"], "DRAFT_READY")
        self.assertEqual(draft["generation_mode"], "DETERMINISTIC_RULES")
        self.assertEqual(
            draft["invocation"]["purpose"], "QUESTION_BALLOT_DRAFT"
        )
        self.assertEqual(draft["invocation"]["capability_type"], "RULE")
        self.assertGreaterEqual(len(draft["options"]), 2)
        self.assertTrue(
            all(option["source_refs"] for option in draft["options"])
        )
        progress = self.service.collaboration_progress(
            self.decision["action_item_id"]
        )
        self.assertTrue(progress["dependencies_ready"])
        self.assertEqual(
            {item["bound_upstream_version_id"] for item in progress["dependencies"]},
            {
                first_version,
                self.db.one(
                    "SELECT current_valid_version_id FROM action_items "
                    "WHERE action_item_id = ?",
                    (self.second["action_item_id"],),
                )["current_valid_version_id"],
            },
        )

        self.service.open_question_ballot(
            self.decision["action_item_id"],
            actor_id=self.actors["子恒"],
            options=[
                {
                    "option_id": "q1",
                    "text": "问题甲1",
                    "source_action_item_id": self.first["action_item_id"],
                },
                {
                    "option_id": "q2",
                    "text": "问题甲2",
                    "source_action_item_id": self.first["action_item_id"],
                },
                {
                    "option_id": "q3",
                    "text": "问题乙1",
                    "source_action_item_id": self.second["action_item_id"],
                },
            ],
            message_id="open-ballot",
        )
        with self.assertRaisesRegex(ValueError, "opened ballot is locked"):
            self.service.open_question_ballot(
                self.decision["action_item_id"],
                actor_id=self.actors["子恒"],
                options=draft["options"],
                message_id="open-ballot-again",
            )
        with self.assertRaisesRegex(ValueError, "all required votes"):
            self.service.submit_artifact(
                self.decision["action_item_id"],
                actor_id=self.actors["子恒"],
                message_id="premature-final",
                payload={"summary": "提前定稿", "content": "提前定稿"},
            )

        self.service.submit_question_vote(
            self.decision["action_item_id"],
            actor_id=self.actors["甲"],
            scores={"q1": 5, "q2": 2, "q3": 4},
            message_id="vote-a",
        )
        with self.assertRaisesRegex(ValueError, "submitted vote is locked"):
            self.service.submit_question_vote(
                self.decision["action_item_id"],
                actor_id=self.actors["甲"],
                scores={"q1": 1, "q2": 1, "q3": 1},
                message_id="vote-a-change",
            )
        vote_result = self.service.submit_question_vote(
            self.decision["action_item_id"],
            actor_id=self.actors["乙"],
            scores={"q1": 4, "q2": 3, "q3": 5},
            message_id="vote-b",
        )
        self.assertTrue(vote_result["progress"]["final_submission_ready"])
        self.assertEqual(
            [
                item["option_id"]
                for item in vote_result["progress"]["vote_summary"][
                    "selected_options"
                ]
            ],
            ["q1", "q3"],
        )

        self.accept(
            self.decision,
            self.actors["子恒"],
            "decision-v1",
            "最终保留问题甲1和问题乙1",
        )
        first_update = self.service.submit_artifact(
            self.first["action_item_id"],
            actor_id=self.actors["甲"],
            message_id="submit-first-v2",
            payload={"summary": "补充问题", "content": "问题甲1、问题甲2、问题甲3"},
        )
        self.service.process_task_result_once(processing_mode="local")
        self.service.review_artifact(
            first_update["version_id"],
            actor_id=self.coordinator,
            approve=True,
            comment="新版通过",
            message_id="review-first-v2",
        )
        reopened = self.db.one(
            "SELECT status, current_valid_version_id FROM action_items "
            "WHERE action_item_id = ?",
            (self.decision["action_item_id"],),
        )
        self.assertEqual(reopened["status"], "TRACKING")
        self.assertIsNone(reopened["current_valid_version_id"])
        reset_progress = self.service.collaboration_progress(
            self.decision["action_item_id"]
        )
        self.assertFalse(reset_progress["ballot_open"])
        self.assertFalse(reset_progress["final_submission_ready"])

    def test_versioned_dispatch_enforces_the_confirmed_final_owner(self) -> None:
        self.service.confirm_question_vote_structure(
            collection_action_item_ids=[
                self.first["action_item_id"],
                self.second["action_item_id"],
            ],
            decision_action_item_id=self.decision["action_item_id"],
            final_owner_actor_id=self.actors["子恒"],
            voter_actor_ids=[self.actors["甲"], self.actors["乙"]],
            selection_count=2,
            source_span="子恒汇总后大家打分投票，最后保留七到八个问题",
            actor_id=self.coordinator,
            message_id="confirm-question-vote-dispatch",
        )
        with self.assertRaisesRegex(PermissionError, "confirmed final owner"):
            self.service.dispatch_action(
                self.decision["action_item_id"],
                actor_id=self.coordinator,
                owner_actor_id=self.actors["甲"],
                collaborator_actor_ids=[],
                assignment_message="负责汇总",
                message_id="wrong-final-owner-dispatch",
            )
        dispatched = self.service.dispatch_action(
            self.decision["action_item_id"],
            actor_id=self.coordinator,
            owner_actor_id=self.actors["子恒"],
            collaborator_actor_ids=[],
            assignment_message="负责汇总和定稿",
            message_id="correct-final-owner-dispatch",
        )
        self.assertEqual(dispatched["status"], "PENDING_ASSIGNMENT")

    def prepare_two_accepted_question_lists(self) -> None:
        self.service.confirm_question_vote_structure(
            collection_action_item_ids=[
                self.first["action_item_id"],
                self.second["action_item_id"],
            ],
            decision_action_item_id=self.decision["action_item_id"],
            final_owner_actor_id=self.actors["子恒"],
            voter_actor_ids=[self.actors["甲"], self.actors["乙"]],
            selection_count=2,
            source_span="子恒汇总后大家打分投票，最后保留七到八个问题",
            actor_id=self.coordinator,
            message_id="confirm-inputs",
        )
        for action, owner_actor_id, suffix in (
            (self.first, self.actors["甲"], "first"),
            (self.second, self.actors["乙"], "second"),
            (self.decision, self.actors["子恒"], "decision"),
        ):
            self.service.dispatch_action(
                action["action_item_id"],
                actor_id=self.coordinator,
                owner_actor_id=owner_actor_id,
                collaborator_actor_ids=[],
                assignment_message="按会议原话执行",
                message_id=f"inputs-dispatch-{suffix}",
            )
            self.service.respond_to_assignment(
                action["action_item_id"],
                actor_id=owner_actor_id,
                decision="ACCEPT",
                response_message="接受任务",
                message_id=f"inputs-accept-{suffix}",
            )
        self.accept(
            self.first,
            self.actors["甲"],
            "inputs-first",
            "你最近一次觉得特别开心是什么时候？\n你会怎么向外地朋友介绍这座城市？",
        )
        self.accept(
            self.second,
            self.actors["乙"],
            "inputs-second",
            "你上一次熬夜是因为什么？\n你觉得钱和时间哪个更值钱？",
        )

    def test_ballot_draft_reads_the_accepted_delivery_body(self) -> None:
        self.prepare_two_accepted_question_lists()
        draft = self.service.prepare_question_ballot_draft(
            self.decision["action_item_id"],
            actor_id=self.actors["子恒"],
            processing_mode="local",
            message_id="prepare-draft-from-body",
        )
        texts = {option["text"] for option in draft["options"]}
        # The questions themselves live in the accepted delivery body; a draft
        # built only from completion reports cannot contain them.
        self.assertIn("你最近一次觉得特别开心是什么时候？", texts)
        self.assertIn("你觉得钱和时间哪个更值钱？", texts)
        self.assertEqual(len(draft["options"]), 4)

    def test_upstream_context_detail_level_splits_body_from_direction(self) -> None:
        self.prepare_two_accepted_question_lists()
        full = self.service.collaboration_input_context(
            self.decision["action_item_id"]
        )["upstream_results"]
        self.assertTrue(full)
        for result in full:
            self.assertEqual(result["detail_level"], "FULL")
            self.assertIn("？", result["submitted_content"])
            self.assertIn("attachment_texts", result)

        summary = self.service.collaboration_input_context(
            self.decision["action_item_id"], detail_level="SUMMARY"
        )["upstream_results"]
        for result in summary:
            self.assertEqual(result["detail_level"], "SUMMARY")
            self.assertNotIn("submitted_content", result)
            self.assertNotIn("attachment_texts", result)
            self.assertNotIn("normalized_result", result)
            # Who delivered, and in what direction, still syncs to everyone.
            self.assertTrue(result["submitted_by_display_name"])
            self.assertTrue(result["responsibility"])
            self.assertTrue(result["submission_summary"])

        with self.assertRaisesRegex(ValueError, "detail level"):
            self.service.collaboration_input_context(
                self.decision["action_item_id"], detail_level="RAW"
            )

    def confirm_structure(self, message_id: str = "confirm-for-revoke") -> None:
        self.service.confirm_question_vote_structure(
            collection_action_item_ids=[
                self.first["action_item_id"],
                self.second["action_item_id"],
            ],
            decision_action_item_id=self.decision["action_item_id"],
            final_owner_actor_id=self.actors["子恒"],
            voter_actor_ids=[self.actors["甲"], self.actors["乙"]],
            selection_count=2,
            source_span="子恒汇总后大家打分投票",
            actor_id=self.coordinator,
            message_id=message_id,
        )

    def test_a_structure_confirmed_on_the_wrong_task_can_be_revoked(self) -> None:
        """Confirmation used to be one-way, so a misconfigured task was stranded
        behind dependencies it never needed, invisible to the page that made
        them."""

        self.confirm_structure()
        self.assertIsNotNone(
            self.service.collaboration_progress(self.decision["action_item_id"])
        )
        revoked = self.service.revoke_question_vote_structure(
            self.decision["action_item_id"],
            actor_id=self.coordinator,
            reason="选错了定稿任务",
            message_id="revoke-structure",
        )
        self.assertEqual(revoked["status"], "REVOKED")
        self.assertEqual(revoked["removed_dependency_count"], 2)
        self.assertEqual(revoked["removed_participation_input_count"], 3)
        self.assertIsNone(
            self.service.collaboration_progress(self.decision["action_item_id"])
        )
        action = self.service.action(self.decision["action_item_id"])
        self.assertNotIn(
            "collaboration_structure", self.service.proposal_metadata(action)
        )
        # The task is configurable again from scratch.
        self.confirm_structure("confirm-again")
        self.assertIsNotNone(
            self.service.collaboration_progress(self.decision["action_item_id"])
        )

    def test_revoking_after_dispatch_is_refused(self) -> None:
        self.confirm_structure()
        self.service.dispatch_action(
            self.decision["action_item_id"],
            actor_id=self.coordinator,
            owner_actor_id=self.actors["子恒"],
            collaborator_actor_ids=[],
            assignment_message="负责汇总",
            message_id="dispatch-before-revoke",
        )
        with self.assertRaisesRegex(ValueError, "already dispatched"):
            self.service.revoke_question_vote_structure(
                self.decision["action_item_id"],
                actor_id=self.coordinator,
                reason="想撤销",
                message_id="revoke-too-late",
            )

    def test_only_the_coordinator_may_revoke(self) -> None:
        self.confirm_structure()
        with self.assertRaises(PermissionError):
            self.service.revoke_question_vote_structure(
                self.decision["action_item_id"],
                actor_id=self.actors["子恒"],
                reason="我想撤销",
                message_id="revoke-wrong-actor",
            )

    def test_a_ballot_must_offer_more_candidates_than_it_keeps(self) -> None:
        """Keeping every candidate makes the vote decorative; the meeting asked
        for the top N out of more."""

        self.prepare_two_accepted_question_lists()
        context = self.service.collaboration_input_context(
            self.decision["action_item_id"]
        )
        versions = {
            item["action_item_id"]: item["accepted_version_id"]
            for item in context["upstream_results"]
        }
        first_id = self.first["action_item_id"]
        options = [
            {
                "option_id": f"d{index}",
                "text": text,
                "source_action_item_id": first_id,
                "source_refs": [
                    {"action_item_id": first_id, "version_id": versions[first_id]}
                ],
            }
            for index, text in enumerate(["候选甲", "候选乙"])
        ]
        # selection_count is 2 in this fixture, so exactly two candidates
        # would select both regardless of any score.
        with self.assertRaisesRegex(ValueError, "must offer"):
            self.service.open_question_ballot(
                self.decision["action_item_id"],
                actor_id=self.actors["子恒"],
                options=options,
                message_id="degenerate-ballot",
            )

    def test_a_voter_reaches_the_decision_task_and_its_ballot(self) -> None:
        """A confirmed voter owns nothing on the decision task, so the state a
        voter receives must still carry the task, their pending VOTE record and
        the published options -- otherwise the ballot is unreachable."""

        from collab_agent.auth import VirtualSessionPrincipalProvider
        from collab_agent.web import workbench_state

        self.prepare_two_accepted_question_lists()
        self.service.prepare_question_ballot_draft(
            self.decision["action_item_id"],
            actor_id=self.actors["子恒"],
            processing_mode="local",
            message_id="voter-visibility-draft",
        )
        context = self.service.collaboration_input_context(
            self.decision["action_item_id"]
        )
        versions = {
            item["action_item_id"]: item["accepted_version_id"]
            for item in context["upstream_results"]
        }
        options = [
            {
                "option_id": f"v{index}",
                "text": text,
                "source_action_item_id": action_id,
                "source_refs": [
                    {"action_item_id": action_id, "version_id": versions[action_id]}
                ],
            }
            for index, (text, action_id) in enumerate(
                [
                    ("你最近一次觉得特别开心是什么时候？", self.first["action_item_id"]),
                    ("你会怎么向外地朋友介绍这座城市？", self.first["action_item_id"]),
                    ("你上一次熬夜是因为什么？", self.second["action_item_id"]),
                ]
            )
        ]
        self.service.open_question_ballot(
            self.decision["action_item_id"],
            actor_id=self.actors["子恒"],
            options=options,
            message_id="voter-visibility-open",
        )

        provider = VirtualSessionPrincipalProvider(
            self.db, episode_id=self.service.episode_id, secret="0" * 32
        )
        voter = provider.resolve(provider.issue(self.actors["甲"])["token"])
        state = workbench_state(self.service, principal=voter)
        task = next(
            (
                item
                for item in state["tasks"]
                if item["action_item_id"] == self.decision["action_item_id"]
            ),
            None,
        )
        self.assertIsNotNone(task, "the voter cannot see the decision task at all")
        self.assertFalse(task["is_mine"])
        self.assertFalse(task["is_collaborator"])

        contributions = task["collaboration_progress"]["contributions"]
        mine = next(
            item
            for item in contributions
            if item["contribution_type"] == "VOTE"
            and item["actor_id"] == self.actors["甲"]
        )
        self.assertEqual(mine["status"], "PENDING")
        ballot = next(
            item
            for item in contributions
            if item["contribution_type"] == "BALLOT" and item["status"] == "SUBMITTED"
        )
        self.assertEqual(len(ballot["payload"]["options"]), 3)
        self.assertEqual(ballot["payload"]["selection_count"], 2)

        # Another voter's ballot is visible, but their scores are not.
        other = next(
            item
            for item in contributions
            if item["contribution_type"] == "VOTE"
            and item["actor_id"] != self.actors["甲"]
        )
        self.assertIsNone(other["payload"])


if __name__ == "__main__":
    unittest.main()
