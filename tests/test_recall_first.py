from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collab_agent.extraction_evaluation import (
    LabelledMeeting,
    evaluate_extractor,
    score_items,
)
from collab_agent.extraction_baselines import project_chain_extractor
from collab_agent.recall import (
    RawCandidate,
    RecallFirstExtractor,
    WindowPolicy,
    assert_emit_coverage,
    build_recall_windows,
    build_transcript_units,
    resolve_evidence,
    rule_recall,
    union_candidates,
)


class RecallWindowTests(unittest.TestCase):
    def test_each_unit_is_emitted_once_while_neighbours_are_context(self) -> None:
        transcript = "\n".join(
            f"成员({index // 60:02d}:{index % 60:02d}:00): 第{index}段讨论，需要补充一些内容。"
            for index in range(12)
        )
        units = build_transcript_units(transcript)
        windows = build_recall_windows(
            units,
            WindowPolicy(
                total_characters=240,
                left_characters=60,
                emit_characters=120,
                right_characters=60,
            ),
        )
        coverage = assert_emit_coverage(units, windows)
        self.assertEqual(coverage["coverage_rate"], 1.0)
        self.assertGreater(len(windows), 1)
        self.assertTrue(windows[1].left_context_ids)
        self.assertTrue(windows[0].right_context_ids)

    def test_short_confirmation_expands_left_against_full_transcript(self) -> None:
        units = build_transcript_units(
            "主持人(00:00:01): 请整理客户问题清单并发给大家。\n"
            "小王(00:00:02): 好的。"
        )
        candidate = RawCandidate(
            candidate_id="rc_test",
            anchor_unit_ids=[units[1].unit_id],
            kind_hints=["TASK"],
            trigger_sources=["MODEL"],
        )
        resolved = resolve_evidence(candidate, units)
        self.assertTrue(resolved["sufficient"])
        self.assertEqual(
            resolved["candidate"].anchor_unit_ids,
            [units[0].unit_id],
        )
        self.assertIn(units[1].unit_id, resolved["context_unit_ids"])

    def test_rule_confirmation_enriches_previous_candidate_without_own_card(self) -> None:
        units = build_transcript_units(
            "主持人(00:00:01): 请整理客户问题清单并发给大家。\n"
            "小王(00:00:02): 好的。"
        )
        candidates = rule_recall(units)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].anchor_unit_ids, [units[0].unit_id])
        self.assertIn(units[1].unit_id, candidates[0].support_unit_ids)
        self.assertIn("SHORT_CONFIRMATION_SUPPORT", candidates[0].quality_flags)

    def test_confirmation_can_retain_a_previously_too_weak_action(self) -> None:
        units = build_transcript_units(
            "主持人(00:00:01): 整理客户问题清单。\n"
            "小王(00:00:02): 好的。"
        )
        candidates = rule_recall(units)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].anchor_unit_ids, [units[0].unit_id])
        self.assertEqual(candidates[0].support_unit_ids, [units[1].unit_id])

    def test_generic_recruitment_and_schedule_terms_extend_rule_recall(self) -> None:
        units = build_transcript_units(
            "成员(00:00:01): \u4e0a\u5348\u5f00\u59cb\u62db\u8058\u5e76\u5b89\u6392\u4eba\u5458\u96c6\u5408\u3002"
        )
        self.assertTrue(rule_recall(units))

    def test_linked_evidence_keeps_the_bounded_units_between_anchor_and_support(self) -> None:
        units = build_transcript_units(
            "\n".join(
                [
                    "成员(00:00:01): \u8bf7\u63d0\u4ea4\u65b9\u6848\u3002",
                    "成员(00:00:02): \u9891\u7387\u662f\u6bcf\u5468\u4e00\u6b21\u3002",
                    "成员(00:00:03): \u9700\u8981\u5148\u68c0\u67e5\u65f6\u95f4\u51b2\u7a81\u3002",
                    "成员(00:00:04): \u597d\u7684\u3002",
                ]
            )
        )
        resolution = resolve_evidence(
            RawCandidate(
                candidate_id="bridge",
                anchor_unit_ids=[units[0].unit_id],
                support_unit_ids=[units[3].unit_id],
                kind_hints=["TASK"],
                trigger_sources=["MODEL"],
            ),
            units,
        )
        self.assertIn(units[1].unit_id, resolution["evidence_unit_ids"])
        self.assertIn(units[2].unit_id, resolution["evidence_unit_ids"])
        self.assertEqual(
            sum(
                step["reason"] == "LINKED_EVIDENCE_BRIDGE"
                for step in resolution["expansion_steps"]
            ),
            2,
        )

    def test_recovered_context_candidate_merges_with_emit_owned_duplicate(self) -> None:
        recovered = RawCandidate(
            candidate_id="recovered",
            anchor_unit_ids=["u1"],
            support_unit_ids=["u0"],
            kind_hints=["TASK"],
            reason_code="seen from context",
            trigger_sources=["MODEL"],
            quality_flags=["VISIBLE_CONTEXT_ANCHOR_RECOVERED"],
        )
        owned = RawCandidate(
            candidate_id="owned",
            anchor_unit_ids=["u1"],
            support_unit_ids=["u2"],
            kind_hints=["TASK"],
            reason_code="seen from emit",
            trigger_sources=["MODEL"],
        )
        merged = union_candidates([recovered, owned])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].support_unit_ids, ["u0", "u2"])
        self.assertNotIn(
            "VISIBLE_CONTEXT_ANCHOR_RECOVERED", merged[0].quality_flags
        )
        self.assertIn(
            "VISIBLE_CONTEXT_DUPLICATE_MERGED", merged[0].quality_flags
        )


class RecallPipelineTests(unittest.TestCase):
    @staticmethod
    def _empty_model(messages):
        if "discover possible" in messages[0]["content"]:
            return {"candidates": []}
        return {"items": []}

    def test_bad_candidate_does_not_remove_valid_candidate(self) -> None:
        def complete(messages):
            if "discover possible" in messages[0]["content"]:
                return {
                    "candidates": [
                        {
                            "anchor_unit_ids": ["u000000.000"],
                            "support_unit_ids": [],
                            "kind_hints": ["TASK"],
                            "reason_code": "explicit follow-up",
                        },
                        {
                            "anchor_unit_ids": ["u999999.000"],
                            "support_unit_ids": [],
                            "kind_hints": ["TASK"],
                            "reason_code": "bad id",
                        },
                    ]
                }
            return {
                "items": [
                    {
                        "candidate_id": __import__("json").loads(
                            messages[1]["content"]
                        )["candidates"][0]["candidate_id"],
                        "title": "提交客户方案",
                        "item_type": "TASK",
                        "deliverable": "客户方案文档",
                        "owner_name": "小王",
                        "deadline_text": "明天",
                        "deadline_iso": None,
                        "confidence": 0.8,
                        "needs_confirmation": True,
                        "uncertainties": ["日期待确认"],
                        "collaborator_names": [],
                    }
                ]
            }

        result = RecallFirstExtractor(complete_json=complete).extract(
            "小王(00:00:01): 我来整理客户方案，明天提交。"
        )
        self.assertTrue(result["action_items"])
        self.assertTrue(
            any(
                failure["stage"] == "MODEL_DISCOVERY_VALIDATION"
                for failure in result["failures"]
            )
        )
        self.assertEqual(result["action_items"], result["draft_items"])

    def test_model_outage_keeps_rule_only_drafts(self) -> None:
        def unavailable(_messages):
            raise RuntimeError("provider unavailable")

        result = RecallFirstExtractor(complete_json=unavailable).extract(
            "小王(00:00:01): 我来整理客户方案，明天提交。"
        )
        self.assertEqual(result["pipeline_status"], "DEGRADED")
        self.assertTrue(result["raw_candidates"])
        self.assertTrue(result["draft_items"])
        self.assertTrue(
            all("RULE" in item["trigger_sources"] for item in result["draft_items"])
        )

    def test_discovery_only_mode_skips_structuring_calls(self) -> None:
        calls = 0

        def complete(messages):
            nonlocal calls
            calls += 1
            self.assertIn("discover possible", messages[0]["content"])
            return {"candidates": []}

        result = RecallFirstExtractor(
            complete_json=complete,
            structure_candidates=False,
        ).extract("小王(00:00:01): 我来整理客户方案，明天提交。")
        self.assertEqual(calls, 1)
        self.assertEqual(result["pipeline_status"], "SUCCEEDED")
        self.assertEqual(
            result["invocation"]["purpose"],
            "RECALL_FIRST_CANDIDATE_DISCOVERY_EVALUATION",
        )

    def test_pipe_delimited_kind_hint_is_not_silently_downgraded_to_task(self) -> None:
        def complete(_messages):
            return {
                "candidates": [
                    {
                        "anchor_unit_ids": ["u000000.000"],
                        "support_unit_ids": [],
                        "kind_hints": ["TASK|COMMITMENT|DECISION"],
                        "reason_code": "possible follow-up",
                    }
                ]
            }

        result = RecallFirstExtractor(
            complete_json=complete,
            structure_candidates=False,
        ).extract("成员(00:00:01): 这个方案作为后续事项。")
        model_candidate = next(
            candidate
            for candidate in result["raw_candidates"]
            if "MODEL" in candidate["trigger_sources"]
        )
        self.assertEqual(
            model_candidate["kind_hints"],
            ["TASK", "COMMITMENT", "DECISION"],
        )

    def test_invalid_or_excessive_support_does_not_delete_a_valid_anchor(self) -> None:
        transcript = "\n".join(
            ["成员(00:00:01): 这个方案作为后续事项。"]
            + [
                f"成员(00:00:{index + 2:02d}): 第{index}条相关背景说明。"
                for index in range(8)
            ]
        )

        def complete(_messages):
            return {
                "candidates": [
                    {
                        "anchor_unit_ids": ["u0.0"],
                        "support_unit_ids": [
                            *(f"u{index:06d}.000" for index in range(1, 9)),
                            "u999999.000",
                        ],
                        "kind_hints": ["TASK"],
                        "reason_code": "possible follow-up",
                    }
                ]
            }

        result = RecallFirstExtractor(
            complete_json=complete,
            structure_candidates=False,
            model_support_character_budget=120,
        ).extract(transcript)
        model_candidate = next(
            candidate
            for candidate in result["raw_candidates"]
            if "MODEL" in candidate["trigger_sources"]
        )
        self.assertEqual(model_candidate["anchor_unit_ids"], ["u000000.000"])
        self.assertIn(
            "MODEL_UNIT_ID_CANONICALIZED",
            model_candidate["quality_flags"],
        )
        self.assertNotIn("u999999.000", model_candidate["support_unit_ids"])
        self.assertLess(len(model_candidate["support_unit_ids"]), 8)
        self.assertIn(
            "UNKNOWN_MODEL_SUPPORT_DROPPED",
            model_candidate["quality_flags"],
        )
        self.assertIn(
            "MODEL_SUPPORT_CHARACTER_BUDGET_TRIMMED",
            model_candidate["quality_flags"],
        )

    def test_window_checkpoint_is_versioned_by_model_and_prompt(self) -> None:
        calls = 0

        def complete(_messages):
            nonlocal calls
            calls += 1
            return {"candidates": []}

        transcript = "成员(00:00:01): 讨论后续安排。"
        with tempfile.TemporaryDirectory() as directory:
            for model in ("model-a", "model-a", "model-b"):
                RecallFirstExtractor(
                    complete_json=complete,
                    model=model,
                    structure_candidates=False,
                ).extract(transcript, checkpoint_dir=directory)
            with patch(
                "collab_agent.recall.RECALL_PROMPT_VERSION",
                "meeting-recall-first.test-new-prompt",
            ):
                RecallFirstExtractor(
                    complete_json=complete,
                    model="model-a",
                    structure_candidates=False,
                ).extract(transcript, checkpoint_dir=directory)
            self.assertEqual(calls, 3)
            self.assertEqual(len(list(Path(directory).glob("recall-*.json"))), 3)

    def test_window_profiles_have_distinct_evaluation_signatures(self) -> None:
        small = project_chain_extractor(
            window_policy=WindowPolicy(
                total_characters=2000,
                left_characters=400,
                emit_characters=1200,
                right_characters=400,
            ),
            output="raw_candidates",
            structure_candidates=False,
        )
        current = project_chain_extractor(
            window_policy=WindowPolicy(),
            output="raw_candidates",
            structure_candidates=False,
        )
        self.assertNotEqual(small.run_signature, current.run_signature)

    def test_time_only_candidate_is_retained_as_review_hint(self) -> None:
        calls = 0

        def complete(_messages):
            nonlocal calls
            calls += 1
            return {
                "candidates": [
                    {
                        "anchor_unit_ids": ["u000000.000"],
                        "support_unit_ids": [],
                        "kind_hints": ["COMMITMENT"],
                        "reason_code": "time fragment",
                    }
                ]
            }

        result = RecallFirstExtractor(complete_json=complete).extract(
            "成员(00:00:01): 明天下午。"
        )
        self.assertEqual(calls, 1)
        self.assertEqual(result["draft_items"], [])
        self.assertEqual(len(result["review_hints"]), 1)
        self.assertEqual(result["review_hints"][0]["status"], "OPEN")

    def test_weak_decision_alone_is_visible_only_as_review_hint(self) -> None:
        result = RecallFirstExtractor(complete_json=self._empty_model).extract(
            "成员(00:00:01): 我只是有一点选择困难。"
        )
        self.assertEqual(len(result["raw_candidates"]), 1)
        self.assertEqual(result["draft_items"], [])
        self.assertEqual(len(result["review_hints"]), 1)
        self.assertEqual(
            result["review_hints"][0]["reason_code"],
            "WEAK_DECISION_UNCORROBORATED",
        )

    def test_weak_decision_with_action_and_object_can_be_a_draft(self) -> None:
        result = RecallFirstExtractor(complete_json=self._empty_model).extract(
            "主持人(00:00:01): 同意由小王明天提交客户方案。"
        )
        self.assertEqual(len(result["draft_items"]), 1)
        self.assertEqual(result["review_hints"], [])

    def test_weak_decision_with_only_a_time_is_still_a_hint(self) -> None:
        result = RecallFirstExtractor(complete_json=self._empty_model).extract(
            "主持人(00:00:01): 同意明天。"
        )
        self.assertEqual(result["draft_items"], [])
        self.assertEqual(len(result["review_hints"]), 1)
        self.assertEqual(
            result["review_hints"][0]["reason_code"],
            "WEAK_DECISION_UNCORROBORATED",
        )

    def test_confirmed_contentless_action_does_not_borrow_next_topic(self) -> None:
        result = RecallFirstExtractor(complete_json=self._empty_model).extract(
            "主持人(00:00:01): 这个先做一下。\n"
            "小王(00:00:02): 好的。\n"
            "成员(00:00:03): 客户方案最近很受欢迎。"
        )
        self.assertEqual(result["draft_items"], [])
        self.assertEqual(len(result["review_hints"]), 1)
        self.assertEqual(
            result["review_hints"][0]["reason_code"],
            "ACTION_OBJECT_OR_RESULT_MISSING",
        )

    def test_assignment_only_does_not_borrow_an_unrelated_neighbour_action(self) -> None:
        result = RecallFirstExtractor(complete_json=self._empty_model).extract(
            "成员(00:00:01): 房地产现在要不要买？\n"
            "主持人(00:00:02): 请整理客户问题清单。"
        )
        self.assertTrue(
            any(
                hint["reason_code"] == "ASSIGNMENT_WITHOUT_ANCHORED_ACTION"
                for hint in result["review_hints"]
            )
        )
        self.assertEqual(len(result["draft_items"]), 1)

    def test_negated_strong_decision_is_not_promoted(self) -> None:
        result = RecallFirstExtractor(complete_json=self._empty_model).extract(
            "成员(00:00:01): 未来十几年都不能确定。"
        )
        self.assertEqual(result["draft_items"], [])
        self.assertEqual(len(result["review_hints"]), 1)
        self.assertEqual(
            result["review_hints"][0]["reason_code"],
            "NEGATED_DECISION_REQUIRES_REVIEW",
        )


class EvaluationSemanticsTests(unittest.TestCase):
    def test_recall_evidence_coverage_does_not_pretend_support_is_a_prediction(self) -> None:
        target = LabelledMeeting(
            meeting_id="evidence",
            transcript="金标行动。\n非金标说明。",
            sentences=["金标行动。", "非金标说明。"],
            positive_sentence_indices={0},
        )

        def support_only(_meeting):
            support_only.last_artifact = {
                "pipeline_status": "SUCCEEDED",
                "units": [
                    {"unit_id": "u0", "line_index": 0},
                    {"unit_id": "u1", "line_index": 1},
                ],
                "raw_candidates": [
                    {
                        "anchor_unit_ids": ["u1"],
                        "support_unit_ids": ["u0"],
                    }
                ],
                "draft_items": [],
                "review_hints": [{"evidence_unit_ids": ["u0", "u1"]}],
                "coverage": {},
                "failures": [],
            }
            return [{"source_quote": "非金标说明。"}]

        support_only.last_artifact = None
        report = evaluate_extractor([target], support_only, name="support-only")
        self.assertEqual(report["sentence_level_positive_f1"]["true_positive"], 0)
        coverage = report["recall_gold_coverage"]
        self.assertEqual(coverage["raw_anchor"]["hits"], 0)
        self.assertEqual(coverage["explicit_candidate_evidence"]["hits"], 1)
        self.assertEqual(coverage["routed_candidate_evidence"]["hits"], 1)

    def test_zero_label_meeting_is_not_named_product_empty(self) -> None:
        meeting = LabelledMeeting(
            meeting_id="amc-zero",
            transcript="我们可以继续讨论方案。",
            sentences=["我们可以继续讨论方案。"],
            positive_sentence_indices=set(),
        )

        def one_prediction(_meeting):
            return [{"source_quote": "我们可以继续讨论方案。"}]

        report = evaluate_extractor([meeting], one_prediction, name="probe")
        self.assertEqual(report["amc_zero_label_meetings"], 1)
        self.assertEqual(report["amc_zero_label_meetings_kept_clean"], 0)
        self.assertEqual(
            report["prediction_rate_on_amc_zero_label_meetings"], 1.0
        )

    def test_sentence_only_corpus_marks_item_detection_na(self) -> None:
        meeting = LabelledMeeting(
            meeting_id="amc-1",
            transcript="明天提交方案。",
            sentences=["明天提交方案。"],
            positive_sentence_indices={0},
        )
        self.assertIsNone(score_items(meeting, [
            {"title": "提交方案", "source_quote": "明天提交方案。"}
        ])["detection"])

    def test_failed_meeting_counts_as_false_negative_and_is_resumable(self) -> None:
        meeting = LabelledMeeting(
            meeting_id="amc-fail",
            transcript="明天提交方案。",
            sentences=["明天提交方案。"],
            positive_sentence_indices={0},
        )

        def broken(_meeting):
            raise RuntimeError("provider down")

        broken.run_signature = "chain.v1"

        with tempfile.TemporaryDirectory() as directory:
            first = evaluate_extractor(
                [meeting],
                broken,
                name="chain",
                checkpoint_dir=Path(directory),
            )
            self.assertEqual(
                first["sentence_level_positive_f1"]["false_negative"], 1
            )
            self.assertEqual(first["meetings_failed"], 1)

            called = False

            def must_not_run(_meeting):
                nonlocal called
                called = True
                return [
                    {
                        "title": "提交方案",
                        "source_quote": "明天提交方案。",
                    }
                ]

            must_not_run.run_signature = "chain.v1"

            second = evaluate_extractor(
                [meeting],
                must_not_run,
                name="chain",
                checkpoint_dir=Path(directory),
                resume=True,
            )
            self.assertTrue(called)
            self.assertEqual(second["meetings_failed"], 0)
            self.assertEqual(
                second["sentence_level_positive_f1"]["false_negative"], 0
            )


if __name__ == "__main__":
    unittest.main()
