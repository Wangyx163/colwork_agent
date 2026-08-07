from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from collab_agent.meeting import load_meeting_service
from collab_agent.models import parse_time
from collab_agent.service import (
    NOTIFICATION_EFFECT_TYPES,
    NOTIFY_ASSIGNMENT_RESPONSE_REQUIRED,
    NOTIFY_ASSISTANCE_REQUESTED,
    NOTIFY_REVIEW_DECIDED,
)
from collab_agent.store import Database


def extraction_payload() -> dict:
    return {
        "provider": "fixture",
        "model": "deterministic",
        "input_sha256": "f" * 64,
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


class NotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        extraction = root / "extraction.json"
        transcript = root / "transcript.txt"
        extraction.write_text(
            json.dumps(extraction_payload(), ensure_ascii=False), encoding="utf-8"
        )
        transcript.write_text("主持人(00:01:00): 请整理会议纪要\n", encoding="utf-8")
        self.db = Database(":memory:")
        self.db.initialize()
        self.service = load_meeting_service(
            self.db,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="通知测试团队",
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
        self.action = next(iter(self.service.action_items()))
        deadline = (parse_time(self.service.now()) + timedelta(days=2)).isoformat()
        self.service.revise_action_proposal(
            self.action["action_item_id"],
            actor_id=self.coordinator,
            title=self.action["title"],
            deliverable="会议纪要",
            acceptance_criteria="结论清晰",
            priority="P1",
            team_required_by_sim_time=deadline,
            message_id="prepare-notify",
        )

    def tearDown(self) -> None:
        self.db.close()
        self.directory.cleanup()

    def notifications(self, effect_type: str | None = None) -> list[dict]:
        rows = self.db.all(
            "SELECT * FROM outbox_entries WHERE episode_id = ? "
            "ORDER BY created_sim_time, outbox_id",
            (self.service.episode_id,),
        )
        result = []
        for row in rows:
            if row["effect_type"] not in NOTIFICATION_EFFECT_TYPES:
                continue
            if effect_type and row["effect_type"] != effect_type:
                continue
            item = dict(row)
            item["payload"] = json.loads(row["payload"])
            result.append(item)
        return result

    def dispatch(self, message_id: str = "dispatch-1") -> None:
        self.service.dispatch_action(
            self.action["action_item_id"],
            actor_id=self.coordinator,
            owner_actor_id=self.actors["同事甲"],
            collaborator_actor_ids=[self.actors["同事乙"]],
            assignment_message="请在周五前完成",
            message_id=message_id,
        )

    def test_dispatch_notifies_every_assignee_with_both_decisions(self) -> None:
        self.dispatch()
        entries = self.notifications(NOTIFY_ASSIGNMENT_RESPONSE_REQUIRED)
        self.assertEqual(len(entries), 1)
        payload = entries[0]["payload"]
        self.assertCountEqual(
            payload["recipient_actor_ids"],
            [self.actors["同事甲"], self.actors["同事乙"]],
        )
        notification = payload["notification"]
        self.assertEqual(notification["notification_contract_version"], "notification.v1")
        self.assertEqual(
            notification["action_item_id"], self.action["action_item_id"]
        )
        decisions = {item["name"]: item for item in notification["decisions"]}
        self.assertEqual(
            set(decisions), {"ASSIGNMENT_ACCEPT", "ASSIGNMENT_RETURN"}
        )
        # Returning terminates the round for everyone, so it cannot be a bare
        # button click.
        self.assertFalse(decisions["ASSIGNMENT_ACCEPT"]["requires_reason"])
        self.assertTrue(decisions["ASSIGNMENT_RETURN"]["requires_reason"])

    def test_a_text_only_transport_still_has_something_to_send(self) -> None:
        """MockIM and any other text adapter read `content`; the structured
        notification is additive so no existing transport breaks."""

        self.dispatch()
        payload = self.notifications(NOTIFY_ASSIGNMENT_RESPONSE_REQUIRED)[0]["payload"]
        self.assertIn("整理会议纪要", payload["content"])
        self.assertIn("请在周五前完成", payload["content"])

    def test_redispatching_the_same_version_does_not_notify_twice(self) -> None:
        self.dispatch()
        self.dispatch(message_id="dispatch-1")
        self.assertEqual(len(self.notifications(NOTIFY_ASSIGNMENT_RESPONSE_REQUIRED)), 1)

    def test_a_new_definition_version_notifies_again(self) -> None:
        self.dispatch()
        self.service.respond_to_assignment(
            self.action["action_item_id"],
            actor_id=self.actors["同事甲"],
            decision="RETURN_FOR_REVISION",
            response_message="范围不清楚",
            message_id="return-1",
        )
        deadline = (parse_time(self.service.now()) + timedelta(days=3)).isoformat()
        self.service.revise_action_proposal(
            self.action["action_item_id"],
            actor_id=self.coordinator,
            title=self.action["title"],
            deliverable="会议纪要（含决议清单）",
            acceptance_criteria="结论清晰",
            priority="P1",
            team_required_by_sim_time=deadline,
            message_id="revise-after-return",
        )
        self.dispatch(message_id="dispatch-2")
        entries = self.notifications(NOTIFY_ASSIGNMENT_RESPONSE_REQUIRED)
        self.assertEqual(len(entries), 2)
        versions = {
            field["value"]
            for entry in entries
            for field in entry["payload"]["notification"]["fields"]
            if field["label"] == "任务版本"
        }
        self.assertEqual(versions, {"v1", "v2"})

    def test_assistance_reaches_the_target_with_an_acknowledge_action(self) -> None:
        """A help request used to live only on a page the target might never
        open, which made asking for help unreliable by construction."""

        self.dispatch()
        for name in ("同事甲", "同事乙"):
            self.service.respond_to_assignment(
                self.action["action_item_id"],
                actor_id=self.actors[name],
                decision="ACCEPT",
                response_message="收到",
                message_id=f"accept-{name}",
            )
        self.service.request_assistance(
            self.action["action_item_id"],
            actor_id=self.actors["同事甲"],
            target_actor_id=self.actors["同事乙"],
            category="EXPERTISE",
            summary="需要帮忙核对决议清单",
            message_id="help-1",
        )
        entries = self.notifications(NOTIFY_ASSISTANCE_REQUESTED)
        self.assertEqual(len(entries), 1)
        payload = entries[0]["payload"]
        self.assertEqual(payload["recipient_actor_ids"], [self.actors["同事乙"]])
        self.assertEqual(payload["notification"]["summary"], "需要帮忙核对决议清单")
        self.assertEqual(
            [item["name"] for item in payload["notification"]["decisions"]],
            ["ASSISTANCE_ACKNOWLEDGE"],
        )

    def test_a_verdict_notifies_without_offering_inline_decisions(self) -> None:
        """A verdict is information; the rework it may trigger needs the
        workbench, so the notification only links back."""

        self.dispatch()
        for name in ("同事甲", "同事乙"):
            self.service.respond_to_assignment(
                self.action["action_item_id"],
                actor_id=self.actors[name],
                decision="ACCEPT",
                response_message="收到",
                message_id=f"accept-{name}",
            )
        submitted = self.service.submit_artifact(
            self.action["action_item_id"],
            actor_id=self.actors["同事甲"],
            message_id="submit-1",
            payload={"summary": "纪要已完成", "content": "决议一、决议二"},
        )
        self.service.process_task_result_once(processing_mode="local")
        self.service.review_artifact(
            submitted["version_id"],
            actor_id=self.coordinator,
            approve=False,
            comment="缺少负责人和时间",
            message_id="review-1",
        )
        entries = self.notifications(NOTIFY_REVIEW_DECIDED)
        self.assertEqual(len(entries), 1)
        notification = entries[0]["payload"]["notification"]
        self.assertEqual(notification["decisions"], [])
        self.assertEqual(notification["deep_link_path"], "/tasks")
        self.assertIn("缺少负责人和时间", notification["summary"])

    def test_notifications_do_not_consume_the_nudge_budget(self) -> None:
        """A dispatch someone must respond to is event-driven and always has to
        arrive; only nudges are subject to the daily touch budget."""

        self.dispatch()
        entries = self.notifications()
        self.assertTrue(entries)
        interventions = self.db.all("SELECT COUNT(*) AS count FROM interventions")
        self.assertEqual(int(interventions[0]["count"]), 0)

    def accept_all_and_track(self) -> None:
        self.dispatch()
        for name in ("同事甲", "同事乙"):
            self.service.respond_to_assignment(
                self.action["action_item_id"],
                actor_id=self.actors[name],
                decision="ACCEPT",
                response_message="收到",
                message_id=f"accept-{name}",
            )

    def test_an_owner_final_candidate_notifies_the_coordinator_once_processed(
        self,
    ) -> None:
        """The card fires when processing settles, not at submission: review
        is refused before then, so an earlier card could not be acted on."""

        from collab_agent.service import NOTIFY_RESULT_PENDING_REVIEW

        self.accept_all_and_track()
        self.service.submit_artifact(
            self.action["action_item_id"],
            actor_id=self.actors["同事甲"],
            message_id="submit-final",
            payload={"summary": "纪要含决议与负责人", "content": "决议一、决议二"},
        )
        self.assertEqual(self.notifications(NOTIFY_RESULT_PENDING_REVIEW), [])

        self.service.process_task_result_once(processing_mode="local")
        entries = self.notifications(NOTIFY_RESULT_PENDING_REVIEW)
        self.assertEqual(len(entries), 1)
        payload = entries[0]["payload"]
        self.assertEqual(payload["recipient_actor_ids"], [self.coordinator])
        notification = payload["notification"]
        labels = {field["label"]: field["value"] for field in notification["fields"]}
        self.assertEqual(labels["完成摘要"], "纪要含决议与负责人")
        self.assertIn("用时", labels)
        self.assertIn("AI", labels["AI 处理状态"])
        self.assertEqual(notification["deep_link_path"], "/manage")
        # Accepting requires reading the deliverable and the assist package,
        # which only the workbench shows.
        self.assertEqual(notification["decisions"], [])
        # The card summarises; it must not carry the delivery body.
        self.assertNotIn("决议一", json.dumps(notification, ensure_ascii=False))

    def test_a_collaborator_contribution_does_not_notify_the_coordinator(self) -> None:
        """A contribution is internal to the team: the task owner decides what
        to do with it, so broadcasting it would both spam the meeting and leak
        work-in-progress past the owner."""

        from collab_agent.service import NOTIFY_RESULT_PENDING_REVIEW

        self.accept_all_and_track()
        submitted = self.service.submit_artifact(
            self.action["action_item_id"],
            actor_id=self.actors["同事乙"],
            message_id="submit-contribution",
            payload={"summary": "协作者的阶段材料", "content": "草稿"},
        )
        self.assertEqual(submitted["submission_kind"], "CONTRIBUTION")
        self.service.process_task_result_once(
            processing_mode="local", allow_contribution_analysis=True
        )
        self.assertEqual(self.notifications(NOTIFY_RESULT_PENDING_REVIEW), [])

    def test_the_agent_job_queue_is_never_sent_to_people(self) -> None:
        """FINAL_ORGANIZATION is the worker's own queue; routing it to a chat
        transport would message colleagues with internal jobs."""

        self.assertNotIn("FINAL_ORGANIZATION", NOTIFICATION_EFFECT_TYPES)


if __name__ == "__main__":
    unittest.main()
