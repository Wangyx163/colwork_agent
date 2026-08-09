from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from collab_agent.meeting import load_meeting_service
from collab_agent.models import parse_time
from collab_agent.service import (
    NOTIFY_ASSISTANCE_RESOLVED,
    NOTIFY_TASK_AMENDED,
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


class TaskAmendmentTests(unittest.TestCase):
    """Amending a task's wording is a different act from revising its terms.

    `revise_action_proposal` refuses a task that already has an owner, and
    that refusal protects a real thing: what was dispatched and accepted is a
    commitment between people. These tests pin down that the narrower
    operation stays narrow -- it may fix what the task *says*, and may not
    touch who owes what by when.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        extraction = root / "extraction.json"
        transcript = root / "transcript.txt"
        extraction.write_text(
            json.dumps(extraction_payload(), ensure_ascii=False), encoding="utf-8"
        )
        transcript.write_text("主持人(00:01:00): 请整理会议纪要\n", encoding="utf-8")
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.initialize()
        self.service = load_meeting_service(
            self.db,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="改说明测试团队",
            coordinator_name="会议负责人",
            participant_names=["同事甲", "同事乙", "同事丙"],
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
        self.action_id = self.action["action_item_id"]
        self.deadline = (
            parse_time(self.service.now()) + timedelta(days=2)
        ).isoformat()
        self.service.revise_action_proposal(
            self.action_id,
            actor_id=self.coordinator,
            title=self.action["title"],
            deliverable="会议纪要",
            acceptance_criteria="结论清晰",
            priority="P1",
            team_required_by_sim_time=self.deadline,
            message_id="prepare",
        )

    # ---- helpers -------------------------------------------------------

    def dispatch_and_accept(self) -> None:
        self.service.dispatch_action(
            self.action_id,
            actor_id=self.coordinator,
            owner_actor_id=self.actors["同事甲"],
            collaborator_actor_ids=[self.actors["同事乙"]],
            assignment_message="请在周五前完成",
            message_id="dispatch",
        )
        # Both of them: a task moves to TRACKING only once every assignment is
        # accepted, so accepting as the owner alone leaves it pending and the
        # amendment guard would reject for the wrong reason.
        for index, name in enumerate(("同事甲", "同事乙")):
            self.service.respond_to_assignment(
                self.action_id,
                actor_id=self.actors[name],
                decision="ACCEPT",
                response_message="收到",
                message_id=f"accept-{index}",
            )

    def task(self) -> dict:
        return dict(
            self.db.one(
                "SELECT * FROM action_items WHERE action_item_id = ?",
                (self.action_id,),
            )
        )

    def amendments(self) -> list[dict]:
        return [
            dict(row)
            for row in self.db.all(
                "SELECT * FROM outbox_entries WHERE episode_id = ? "
                "AND effect_type = ? ORDER BY outbox_id",
                (self.service.episode_id, NOTIFY_TASK_AMENDED),
            )
        ]

    def amend(self, actor: str, *, title: str, deliverable: str, message_id: str):
        return self.service.amend_task_description(
            self.action_id,
            actor_id=self.actors.get(actor, actor),
            title=title,
            deliverable=deliverable,
            message_id=message_id,
        )

    # ---- who may -------------------------------------------------------

    def test_the_owner_may_fix_the_wording(self) -> None:
        self.dispatch_and_accept()

        self.amend(
            "同事甲",
            title="整理会议纪要并附行动项",
            deliverable="会议纪要 + 行动项清单",
            message_id="amend-1",
        )

        task = self.task()
        self.assertEqual(task["title"], "整理会议纪要并附行动项")
        self.assertEqual(
            json.loads(task["proposal_metadata"])["deliverable"],
            "会议纪要 + 行动项清单",
        )

    def test_a_collaborator_may_not(self) -> None:
        self.dispatch_and_accept()

        with self.assertRaises(PermissionError):
            self.amend(
                "同事乙", title="改个名", deliverable="改个说明", message_id="amend-2"
            )

    def test_the_coordinator_may_not_either(self) -> None:
        """Running the meeting is not the same as doing the task.

        The coordinator's authority over a task's terms is spent at dispatch;
        after someone accepts it, the wording belongs to the person doing it.
        """

        self.dispatch_and_accept()

        with self.assertRaises(PermissionError):
            self.service.amend_task_description(
                self.action_id,
                actor_id=self.coordinator,
                title="改个名",
                deliverable="改个说明",
                message_id="amend-3",
            )

    def test_a_task_nobody_has_taken_yet_cannot_be_amended(self) -> None:
        """Before dispatch there is no owner, so there is nobody with standing."""

        with self.assertRaises(PermissionError):
            self.amend(
                "同事甲", title="改个名", deliverable="改个说明", message_id="amend-4"
            )

    # ---- what may move -------------------------------------------------

    def test_the_commitment_is_untouched(self) -> None:
        self.dispatch_and_accept()
        before = self.task()

        self.amend(
            "同事甲",
            title="新标题",
            deliverable="新说明",
            message_id="amend-5",
        )

        after = self.task()
        for field in (
            "owner_actor_id",
            "team_required_by_sim_time",
            "deadline_sim_time",
            "active_commitment_revision_id",
            "definition_version",
            "status",
        ):
            self.assertEqual(before[field], after[field], field)

    def test_the_collaborator_list_is_untouched(self) -> None:
        self.dispatch_and_accept()

        self.amend(
            "同事甲", title="新标题", deliverable="新说明", message_id="amend-6"
        )

        metadata = json.loads(self.task()["proposal_metadata"])
        self.assertEqual(
            metadata["collaborator_actor_ids"], [self.actors["同事乙"]]
        )

    # ---- who hears about it --------------------------------------------

    def test_everyone_else_on_the_task_is_told(self) -> None:
        self.dispatch_and_accept()

        result = self.amend(
            "同事甲", title="新标题", deliverable="新说明", message_id="amend-7"
        )

        self.assertIn(self.actors["同事乙"], result["notified_actor_ids"])
        self.assertIn(self.coordinator, result["notified_actor_ids"])
        self.assertNotIn(
            self.actors["同事甲"],
            result["notified_actor_ids"],
            "the person who made the change does not need telling",
        )
        self.assertNotIn(
            self.actors["同事丙"],
            result["notified_actor_ids"],
            "somebody with no part in this task is not an audience",
        )
        self.assertEqual(len(self.amendments()), 1)

    def test_the_notification_asks_for_nothing(self) -> None:
        """A button that only dismisses teaches people to dismiss."""

        self.dispatch_and_accept()
        self.amend(
            "同事甲", title="新标题", deliverable="新说明", message_id="amend-8"
        )

        payload = json.loads(self.amendments()[0]["payload"])

        self.assertEqual(payload["notification"]["decisions"], [])

    def test_saving_the_same_wording_tells_nobody(self) -> None:
        self.dispatch_and_accept()

        result = self.amend(
            "同事甲",
            title=self.action["title"],
            deliverable="会议纪要",
            message_id="amend-9",
        )

        self.assertEqual(result["changed_fields"], {})
        self.assertEqual(self.amendments(), [])

    def test_a_replayed_call_does_not_notify_twice(self) -> None:
        self.dispatch_and_accept()

        for attempt in range(2):
            self.amend(
                "同事甲",
                title="新标题",
                deliverable="新说明",
                message_id="amend-replay",
            )
            self.assertEqual(len(self.amendments()), 1, f"attempt {attempt}")

    def test_the_real_payload_renders_as_a_read_only_feishu_card(self) -> None:
        """End to end, because the card is generic and the payload is not.

        `build_notification_card` renders whatever contract the domain emits,
        so nothing about this new kind was written on the Feishu side. That is
        only safe if what the domain actually produces satisfies the contract
        -- which is this test, not an assumption.
        """

        from collab_agent.feishu_cards import build_notification_card

        self.dispatch_and_accept()
        self.amend(
            "同事甲", title="新标题", deliverable="新说明", message_id="amend-card"
        )
        entry = self.amendments()[0]

        card = build_notification_card(
            {
                "effect_id": entry["effect_id"],
                "effect_type": entry["effect_type"],
                "notification": json.loads(entry["payload"])["notification"],
            }
        )
        rendered = json.dumps(card, ensure_ascii=False)

        self.assertEqual(
            [element for element in card["elements"] if element["tag"] == "action"],
            [],
            "an amendment tells people something; it asks nothing",
        )
        self.assertIn("新标题", rendered)
        self.assertIn("新说明", rendered)

    def test_the_notice_reaches_the_recipient_workbench_state(self) -> None:
        """The bell reads `notices`; if the projection misses, nobody sees it.

        Feishu and the web must not be two channels that can disagree, so both
        read the same Outbox row. This checks the web half arrives, and that it
        arrives only for the people it was addressed to.
        """

        from collab_agent.auth import Principal, PrincipalRole
        from collab_agent.web import workbench_state

        self.dispatch_and_accept()
        self.amend(
            "同事甲", title="新标题", deliverable="新说明", message_id="amend-bell"
        )

        def notices_for(actor_id: str) -> list[dict]:
            state = workbench_state(
                self.service,
                result_processing_mode="disabled",
                principal=Principal(
                    actor_id=actor_id,
                    episode_id=self.service.episode_id,
                    roles=frozenset({PrincipalRole.PARTICIPANT}),
                    auth_source="test",
                    session_id="s",
                ),
            )
            return [
                notice
                for notice in state["notices"]
                if notice["kind"] == NOTIFY_TASK_AMENDED
            ]

        collaborator = notices_for(self.actors["同事乙"])
        self.assertEqual(len(collaborator), 1)
        self.assertIn("新标题", collaborator[0]["summary"])
        self.assertFalse(
            collaborator[0]["decides"],
            "an amendment asks nothing, so the bell must not treat it as an ask",
        )

        self.assertEqual(
            notices_for(self.actors["同事甲"]),
            [],
            "the person who made the change is not notified of it",
        )
        self.assertEqual(
            notices_for(self.actors["同事丙"]),
            [],
            "someone with no part in the task is not an audience",
        )

    def test_the_notice_names_the_fields_in_words_not_columns(self) -> None:
        self.dispatch_and_accept()
        self.amend(
            "同事甲", title="新标题", deliverable="新说明", message_id="amend-words"
        )

        fields = json.loads(self.amendments()[0]["payload"])["notification"][
            "fields"
        ]
        rendered = json.dumps(fields, ensure_ascii=False)

        self.assertIn("任务名称", rendered)
        self.assertIn("任务说明", rendered)
        self.assertNotIn("deliverable", rendered)

    def test_the_audit_records_what_changed(self) -> None:
        self.dispatch_and_accept()
        self.amend(
            "同事甲",
            title="新标题",
            deliverable="新说明",
            message_id="amend-10",
        )

        row = self.db.one(
            "SELECT payload FROM audit_events WHERE event_type = ? "
            "AND aggregate_id = ?",
            ("ActionItemDescriptionAmended", self.action_id),
        )
        payload = json.loads(dict(row)["payload"])

        self.assertEqual(payload["amended_by"], self.actors["同事甲"])
        self.assertEqual(payload["changed_field_count"], 2)
        self.assertEqual(
            payload["changed_fields"]["title"]["before"], self.action["title"]
        )


if __name__ == "__main__":
    unittest.main()


class AssistanceResolutionTests(unittest.TestCase):
    """Closing a help request has to reach the person who was blocked.

    Marking it resolved used to be silent: the helper finished, and whoever
    had been waiting found out by opening the page and noticing. The
    resolution travels with the message, because "it is handled" without
    saying how is not an answer.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        extraction = root / "extraction.json"
        transcript = root / "transcript.txt"
        extraction.write_text(
            json.dumps(extraction_payload(), ensure_ascii=False), encoding="utf-8"
        )
        transcript.write_text("主持人(00:01:00): 请整理会议纪要\n", encoding="utf-8")
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.initialize()
        self.service = load_meeting_service(
            self.db,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="求助测试团队",
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
        action = next(iter(self.service.action_items()))
        self.action_id = action["action_item_id"]
        self.service.revise_action_proposal(
            self.action_id,
            actor_id=self.coordinator,
            title=action["title"],
            deliverable="会议纪要",
            acceptance_criteria="结论清晰",
            priority="P1",
            team_required_by_sim_time=(
                parse_time(self.service.now()) + timedelta(days=2)
            ).isoformat(),
            message_id="help-prepare",
        )
        self.service.dispatch_action(
            self.action_id,
            actor_id=self.coordinator,
            owner_actor_id=self.actors["同事甲"],
            collaborator_actor_ids=[],
            assignment_message="",
            message_id="help-dispatch",
        )
        self.service.respond_to_assignment(
            self.action_id,
            actor_id=self.actors["同事甲"],
            decision="ACCEPT",
            response_message="",
            message_id="help-accept",
        )
        self.request_id = self.service.request_assistance(
            self.action_id,
            actor_id=self.actors["同事甲"],
            target_actor_id=self.actors["同事乙"],
            category="EXPERTISE",
            summary="这块要你看一下",
            message_id="help-ask",
        )["assistance_request_id"]
        self.service.update_assistance(
            self.request_id,
            actor_id=self.actors["同事乙"],
            action="acknowledge",
            resolution_summary="",
            message_id="help-ack",
        )

    def resolved(self) -> list[dict]:
        return [
            dict(row)
            for row in self.db.all(
                "SELECT * FROM outbox_entries WHERE episode_id = ? "
                "AND effect_type = ?",
                (self.service.episode_id, NOTIFY_ASSISTANCE_RESOLVED),
            )
        ]

    def test_resolving_tells_whoever_was_blocked(self) -> None:
        self.service.update_assistance(
            self.request_id,
            actor_id=self.actors["同事乙"],
            action="resolve",
            resolution_summary="一起看过了，用第二个方案",
            message_id="help-resolve",
        )

        entries = self.resolved()
        self.assertEqual(len(entries), 1)
        payload = json.loads(entries[0]["payload"])
        self.assertIn(self.actors["同事甲"], payload["recipient_actor_ids"])
        self.assertNotIn(
            self.actors["同事乙"],
            payload["recipient_actor_ids"],
            "the helper does not need telling what they just did",
        )

    def test_the_message_carries_the_resolution_not_just_the_fact(self) -> None:
        self.service.update_assistance(
            self.request_id,
            actor_id=self.actors["同事乙"],
            action="resolve",
            resolution_summary="一起看过了，用第二个方案",
            message_id="help-resolve-2",
        )

        notification = json.loads(self.resolved()[0]["payload"])["notification"]

        self.assertEqual(notification["summary"], "一起看过了，用第二个方案")
        self.assertEqual(
            notification["decisions"], [], "nothing is being asked of the reader"
        )

    def test_a_resolution_still_has_to_say_something(self) -> None:
        """The page fills this in from the submission when there is one, so an
        empty resolution means nobody said anything anywhere."""

        with self.assertRaises(ValueError):
            self.service.update_assistance(
                self.request_id,
                actor_id=self.actors["同事乙"],
                action="resolve",
                resolution_summary="",
                message_id="help-empty",
            )
