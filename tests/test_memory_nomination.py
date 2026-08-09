from __future__ import annotations

import json
import unittest

from collab_agent.memory_lexicon import MEMORY_TOPICS, SYSTEM_OBSERVED
from collab_agent.memory_nomination import (
    MemoryNominator,
    NominationError,
    nomination_messages,
    observable_labels,
)


REPORT = {"action_item_id": "ai_1", "signals": [], "delivery_versions": []}
EVIDENCE = {"evt_1", "evt_2", "version:ver_1", "assistance:asr_1"}


def nominator(payload: object) -> MemoryNominator:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return MemoryNominator(lambda _messages: raw)


class LabelSpaceTests(unittest.TestCase):
    """The schema the model may fill is the lexicon, nothing wider."""

    def test_only_observable_topics_are_offered(self) -> None:
        offered = {row["topic"] for row in observable_labels()}
        expected = {
            topic
            for topic, spec in MEMORY_TOPICS.items()
            if spec["origin"] == SYSTEM_OBSERVED
        }

        self.assertEqual(offered, expected)

    def test_the_label_space_is_small_enough_to_be_read(self) -> None:
        """A dozen labels a person can hold in their head, not free text."""

        labels = observable_labels()

        self.assertGreaterEqual(len(labels), 9)
        self.assertLessEqual(len(labels), 20)

    def test_the_prompt_carries_the_labels_and_what_is_already_settled(
        self,
    ) -> None:
        messages = nomination_messages(
            REPORT, [{"topic": "HELP_SEEKING", "code": "TRY_FIRST"}]
        )
        sent = json.loads(messages[-1]["content"])

        self.assertEqual(sent["allowed_labels"], observable_labels())
        self.assertEqual(
            sent["already_settled"],
            [{"topic": "HELP_SEEKING", "code": "TRY_FIRST"}],
        )


class ValidationTests(unittest.TestCase):
    """Everything the model returns is re-checked before it becomes a draft."""

    def test_a_label_in_the_lexicon_with_a_real_citation_survives(self) -> None:
        result = nominator(
            {
                "labels": [
                    {
                        "topic": "SCHEDULE_HABIT",
                        "code": "COMMIT_EARLY",
                        "evidence_refs": ["evt_1"],
                    }
                ]
            }
        ).nominate(REPORT, evidence_refs=EVIDENCE)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].code, "COMMIT_EARLY")
        self.assertEqual(result[0].evidence_refs, ("evt_1",))

    def test_an_invented_label_is_dropped(self) -> None:
        result = nominator(
            {
                "labels": [
                    {
                        "topic": "SCHEDULE_HABIT",
                        "code": "WORKS_FAST",
                        "evidence_refs": ["evt_1"],
                    }
                ]
            }
        ).nominate(REPORT, evidence_refs=EVIDENCE)

        self.assertEqual(result, [])

    def test_a_self_declared_topic_is_refused(self) -> None:
        """The split is the rule that stops the model guessing intent.

        Both are checked: one the system could never see (how somebody wants
        feedback), and one it plausibly could (how finished they like a draft
        before showing it) but which is asked in the questionnaire instead.
        The second is the easier mistake to make.
        """

        for topic, code in (
            ("FEEDBACK_STYLE", "DIRECT"),
            ("DELIVERY_RHYTHM", "DRAFT_FIRST"),
        ):
            result = nominator(
                {
                    "labels": [
                        {
                            "topic": topic,
                            "code": code,
                            "evidence_refs": ["evt_1"],
                        }
                    ]
                }
            ).nominate(REPORT, evidence_refs=EVIDENCE)

            self.assertEqual(result, [], topic)

    def test_a_label_citing_nothing_real_is_dropped(self) -> None:
        """An unsupported label is a claim about a person with no basis."""

        result = nominator(
            {
                "labels": [
                    {
                        "topic": "HELP_SEEKING",
                        "code": "TRY_FIRST",
                        "evidence_refs": ["evt_does_not_exist"],
                    }
                ]
            }
        ).nominate(REPORT, evidence_refs=EVIDENCE)

        self.assertEqual(result, [])

    def test_citations_are_filtered_rather_than_the_whole_label_rejected(
        self,
    ) -> None:
        result = nominator(
            {
                "labels": [
                    {
                        "topic": "HELP_SEEKING",
                        "code": "TRY_FIRST",
                        "evidence_refs": ["evt_1", "made_up"],
                    }
                ]
            }
        ).nominate(REPORT, evidence_refs=EVIDENCE)

        self.assertEqual(result[0].evidence_refs, ("evt_1",))

    def test_two_readings_of_one_topic_cannot_both_survive(self) -> None:
        """They are mutually exclusive; keeping both asks for a contradiction."""

        result = nominator(
            {
                "labels": [
                    {
                        "topic": "SCHEDULE_HABIT",
                        "code": "COMMIT_EARLY",
                        "evidence_refs": ["evt_1"],
                    },
                    {
                        "topic": "SCHEDULE_HABIT",
                        "code": "RENEGOTIATE_EARLY",
                        "evidence_refs": ["evt_2"],
                    },
                ]
            }
        ).nominate(REPORT, evidence_refs=EVIDENCE)

        self.assertEqual(len(result), 1)

    def test_something_already_settled_is_not_asked_again(self) -> None:
        result = nominator(
            {
                "labels": [
                    {
                        "topic": "HELP_SEEKING",
                        "code": "TRY_FIRST",
                        "evidence_refs": ["evt_1"],
                    }
                ]
            }
        ).nominate(
            REPORT,
            evidence_refs=EVIDENCE,
            existing=[{"topic": "HELP_SEEKING", "code": "TRY_FIRST"}],
        )

        self.assertEqual(result, [])

    def test_nominating_nothing_is_a_normal_answer(self) -> None:
        result = nominator({"labels": []}).nominate(
            REPORT, evidence_refs=EVIDENCE
        )

        self.assertEqual(result, [])

    def test_non_json_is_an_error_rather_than_a_silent_empty_result(
        self,
    ) -> None:
        with self.assertRaises(NominationError):
            nominator("这次任务他表现不错").nominate(
                REPORT, evidence_refs=EVIDENCE
            )


class LandsAsADraftTests(unittest.TestCase):
    """A nomination is a candidate, exactly like a counted one.

    The whole feature rests on the model having nomination rights and nothing
    else, so the thing worth testing end to end is not that a label appears --
    it is that appearing changes nothing.
    """

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from collab_agent.meeting import load_meeting_service
        from collab_agent.store import Database

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        extraction = root / "extraction.json"
        transcript = root / "transcript.txt"
        extraction.write_text(
            json.dumps(
                {
                    "provider": "fixture",
                    "model": "deterministic",
                    "input_sha256": "e" * 64,
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
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        transcript.write_text("主持人(00:01:00): 请整理会议纪要\n", encoding="utf-8")
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.initialize()
        self.captured: list[list[dict[str, str]]] = []

        class Recording:
            def __init__(self, outer: "LandsAsADraftTests") -> None:
                self.outer = outer

            def nominate(self, report, *, evidence_refs, existing=None):
                self.outer.captured.append([report, sorted(evidence_refs)])
                return MemoryNominator(
                    lambda _m: json.dumps(
                        {
                            "labels": [
                                {
                                    "topic": "SCHEDULE_HABIT",
                                    "code": "COMMIT_TO_ASK",
                                    "evidence_refs": sorted(evidence_refs)[:1],
                                }
                            ]
                        }
                    )
                ).nominate(report, evidence_refs=evidence_refs, existing=existing)

        self.service = load_meeting_service(
            self.db,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="提名测试团队",
            coordinator_name="会议负责人",
            participant_names=["同事甲"],
        )
        self.service.memory_nominator = Recording(self)
        self.actor = dict(
            self.db.one(
                "SELECT a.actor_id FROM actors a JOIN episode_participants ep "
                "ON ep.actor_id = a.actor_id WHERE ep.episode_id = ? "
                "AND ep.role = 'PARTICIPANT'",
                (self.service.episode_id,),
            )
        )["actor_id"]

    def run_task_to_acceptance(self) -> None:
        from datetime import timedelta

        from collab_agent.models import parse_time

        coordinator = self.service.aggregator_actor_id
        action = next(iter(self.service.action_items()))
        action_id = action["action_item_id"]
        self.service.revise_action_proposal(
            action_id,
            actor_id=coordinator,
            title=action["title"],
            deliverable="会议纪要",
            acceptance_criteria="结论清晰",
            priority="P1",
            team_required_by_sim_time=(
                parse_time(self.service.now()) + timedelta(days=2)
            ).isoformat(),
            message_id="prepare",
        )
        self.service.dispatch_action(
            action_id,
            actor_id=coordinator,
            owner_actor_id=self.actor,
            collaborator_actor_ids=[],
            assignment_message="",
            message_id="dispatch",
        )
        self.service.respond_to_assignment(
            action_id,
            actor_id=self.actor,
            decision="ACCEPT",
            response_message="收到",
            message_id="accept",
        )
        submitted = self.service.submit_artifact(
            action_id,
            actor_id=self.actor,
            message_id="submit",
            payload={"summary": "纪要初稿", "content": "结论三条"},
        )
        # Acceptance is gated on result processing having finished, so the
        # worker runs here the way it does in a real deployment -- in "local"
        # mode, which organises deterministically and calls no provider.
        from collab_agent.agent_worker import AgentWorker

        AgentWorker(
            self.service, processing_mode="local", session_id="nomination-test"
        ).run_until_idle()
        self.service.review_artifact(
            submitted["version_id"],
            actor_id=coordinator,
            approve=True,
            comment="可以",
            message_id="review",
        )

    def drafts(self) -> list[dict]:
        return [
            dict(row)
            for row in self.db.all(
                "SELECT * FROM collaboration_memories WHERE actor_id = ?",
                (self.actor,),
            )
        ]

    def test_a_nominated_label_arrives_as_a_private_draft(self) -> None:
        self.run_task_to_acceptance()

        nominated = [
            row
            for row in self.drafts()
            if row["topic"] == "SCHEDULE_HABIT"
        ]

        self.assertEqual(len(nominated), 1)
        self.assertEqual(nominated[0]["status"], "PRIVATE_DRAFT")
        self.assertEqual(nominated[0]["visibility"], "PRIVATE")
        self.assertEqual(nominated[0]["origin"], "SYSTEM_OBSERVED")
        value = json.loads(nominated[0]["value"])
        self.assertEqual(value["nominated_by"], "model")
        self.assertTrue(json.loads(nominated[0]["evidence_refs"]))

    def test_the_nominator_only_ever_sees_this_task_s_own_evidence(self) -> None:
        self.run_task_to_acceptance()

        _report, evidence = self.captured[0]

        self.assertTrue(evidence, "it must be given something to cite")
        self.assertTrue(
            all(
                ref.startswith(("evt_", "version:", "assistance:"))
                for ref in evidence
            ),
            evidence,
        )

    def test_a_broken_provider_loses_nominations_not_the_report(self) -> None:
        class Exploding:
            def nominate(self, *_args, **_kwargs):
                raise RuntimeError("provider down")

        self.service.memory_nominator = Exploding()
        self.run_task_to_acceptance()

        # The counting rules still ran, the acceptance still stands, and the
        # task is still ACCEPTED -- a nomination is never on the critical path.
        action = next(iter(self.service.action_items()))
        self.assertEqual(action["status"], "ACCEPTED")

    def test_nothing_runs_when_no_nominator_is_injected(self) -> None:
        """The demo and the evaluation must not call a provider."""

        self.service.memory_nominator = None
        self.run_task_to_acceptance()

        self.assertEqual(self.captured, [])
        self.assertEqual(
            [row for row in self.drafts() if row["topic"] == "SCHEDULE_HABIT"],
            [],
        )


if __name__ == "__main__":
    unittest.main()
