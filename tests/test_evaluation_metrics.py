from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from collab_agent.extraction_baselines import keyword_extractor
from collab_agent.extraction_evaluation import (
    LabelledMeeting,
    PUBLISHED_SENTENCE_F1_BASELINE,
    compare_extractors,
    evaluate_extractor,
    load_amc_a,
    load_project_cases,
    normalize,
    predicted_sentence_indices,
    score_items,
    score_sentences,
    split_sentences,
)


def meeting(sentences: list[str], positives: set[int], expected=None) -> LabelledMeeting:
    return LabelledMeeting(
        meeting_id="m1",
        transcript="\n".join(sentences),
        sentences=sentences,
        positive_sentence_indices=positives,
        expected_items=expected or [],
    )


class SentenceScoringTests(unittest.TestCase):
    def test_sentences_split_on_terminal_punctuation(self) -> None:
        text = "主持人 (00:01:00): 小王明天提交清单。大家还有问题吗？没有了！"
        self.assertEqual(
            split_sentences(text),
            ["小王明天提交清单。", "大家还有问题吗？", "没有了！"],
        )

    def test_normalisation_ignores_width_case_and_punctuation(self) -> None:
        self.assertEqual(normalize("Ｈｅｌｌｏ，世界！"), normalize("hello世界"))

    def test_an_item_is_credited_to_the_sentence_it_cites(self) -> None:
        sentences = ["小王明天提交清单。", "今天天气不错。"]
        target = meeting(sentences, {0})
        score = score_sentences(target, [{"source_quote": "小王明天提交清单"}])
        self.assertEqual(score["f1"], 1.0)

    def test_a_wrong_citation_costs_both_precision_and_recall(self) -> None:
        sentences = ["小王明天提交清单。", "今天天气不错。"]
        target = meeting(sentences, {0})
        score = score_sentences(target, [{"source_quote": "今天天气不错"}])
        self.assertEqual(score["true_positive"], 0)
        self.assertEqual(score["false_positive"], 1)
        self.assertEqual(score["false_negative"], 1)

    def test_one_item_is_credited_to_one_sentence_only(self) -> None:
        """A transcript is full of short backchannels, and a longer quote
        contains them as substrings. Crediting every substring hit turned a
        single extracted item into ten-plus predicted sentences and destroyed
        precision across the whole corpus."""

        sentences = [
            "对。",
            "是这个。",
            "嗯。",
            "小王明天提交访谈问题清单给团队。",
            "好的。",
        ]
        target = meeting(sentences, {3})
        predicted = predicted_sentence_indices(
            target, [{"source_quote": "小王明天提交访谈问题清单给团队。"}]
        )
        self.assertEqual(predicted, {3})

    def test_a_two_character_coincidence_is_not_a_citation(self) -> None:
        target = meeting(["对。", "小王明天提交清单。"], {1})
        predicted = predicted_sentence_indices(target, [{"source_quote": "对"}])
        self.assertEqual(predicted, set())

    def test_the_best_match_wins_when_several_overlap(self) -> None:
        sentences = ["提交清单。", "小王明天提交访谈问题清单给团队。"]
        target = meeting(sentences, {1})
        predicted = predicted_sentence_indices(
            target, [{"source_quote": "小王明天提交访谈问题清单给团队"}]
        )
        self.assertEqual(predicted, {1})

    def test_an_unlocatable_quote_earns_no_credit(self) -> None:
        """A quote that appears nowhere is a fabricated citation, so it must
        not be silently ignored by the scorer."""

        sentences = ["小王明天提交清单。"]
        target = meeting(sentences, {0})
        score = score_sentences(target, [{"source_quote": "这句话根本不存在于逐字稿"}])
        self.assertEqual(score["true_positive"], 0)
        self.assertEqual(score["false_negative"], 1)


class ItemScoringTests(unittest.TestCase):
    def test_matching_is_anchored_on_the_quote_not_the_wording(self) -> None:
        """Any extractor paraphrases titles; identity has to rest on evidence."""

        target = meeting(
            ["小王明天提交访谈问题清单。"],
            {0},
            expected=[
                {
                    "title": "提交访谈问题清单",
                    "source_quote": "小王明天提交访谈问题清单。",
                    "owner_name": "小王",
                }
            ],
        )
        score = score_items(
            target,
            [
                {
                    "title": "让小王把问题清单交上来",
                    "source_quote": "小王明天提交访谈问题清单。",
                    "owner_name": "小王",
                }
            ],
        )
        self.assertEqual(score["detection"]["f1"], 1.0)
        self.assertEqual(score["field_accuracy"]["owner_name"], 1.0)

    def test_a_wrong_owner_is_counted_even_when_the_item_matches(self) -> None:
        target = meeting(
            ["小王明天提交清单。"],
            {0},
            expected=[
                {"title": "提交清单", "source_quote": "小王明天提交清单。", "owner_name": "小王"}
            ],
        )
        score = score_items(
            target,
            [{"title": "提交清单", "source_quote": "小王明天提交清单。", "owner_name": "小李"}],
        )
        self.assertEqual(score["detection"]["f1"], 1.0)
        self.assertEqual(score["field_accuracy"]["owner_name"], 0.0)

    def test_ungrounded_quotes_are_reported(self) -> None:
        target = meeting(["小王明天提交清单。"], {0}, expected=[])
        score = score_items(target, [{"source_quote": "凭空捏造的一句话"}])
        self.assertEqual(score["ungrounded_quotes"], 1)
        self.assertEqual(score["quote_grounding_rate"], 0.0)

    def test_extra_predictions_are_false_positives(self) -> None:
        target = meeting(
            ["小王明天提交清单。", "小李后天汇报。"],
            {0, 1},
            expected=[{"title": "提交清单", "source_quote": "小王明天提交清单。"}],
        )
        score = score_items(
            target,
            [
                {"title": "提交清单", "source_quote": "小王明天提交清单。"},
                {"title": "多余任务", "source_quote": "小李后天汇报。"},
            ],
        )
        self.assertEqual(score["detection"]["false_positive"], 1)


class HarnessTests(unittest.TestCase):
    def test_an_extractor_failure_is_recorded_not_raised(self) -> None:
        def broken(_: LabelledMeeting):
            raise RuntimeError("provider down")

        result = evaluate_extractor(
            [meeting(["小王明天提交清单。"], {0})], broken, name="broken"
        )
        self.assertEqual(result["meetings_failed"], 1)
        self.assertEqual(result["meetings_scored"], 0)

    def test_the_published_reference_travels_with_every_report(self) -> None:
        """A score without the outside baseline and the annotator ceiling is
        not interpretable."""

        report = compare_extractors(
            [meeting(["小王明天提交清单。"], {0})],
            {"keyword_floor": keyword_extractor},
        )
        self.assertEqual(
            report["published_reference"]["positive_f1"],
            PUBLISHED_SENTENCE_F1_BASELINE["positive_f1"],
        )
        self.assertIn("Kappa", report["interpretation_ceiling"])
        self.assertIn("positive_sentence_rate", report["corpus"])

    def test_amc_a_jsonl_loads_with_sentence_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "amc.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps(record, ensure_ascii=False)
                    for record in [
                        {
                            "meeting_key": "R0001",
                            "sentences": [
                                {"text": "小王明天提交清单。", "label": 1},
                                {"text": "今天天气不错。", "label": 0},
                            ],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            meetings = load_amc_a(path)
        self.assertEqual(len(meetings), 1)
        self.assertEqual(meetings[0].meeting_id, "R0001")
        self.assertEqual(meetings[0].positive_sentence_indices, {0})
        self.assertAlmostEqual(meetings[0].positive_rate, 0.5)

    def test_project_cases_derive_sentence_labels_from_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "c1",
                                "transcript": "主持人 (00:01:00): 小王明天提交清单。今天天气不错。",
                                "expected": [
                                    {
                                        "title": "提交清单",
                                        "source_quote": "小王明天提交清单。",
                                    }
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            meetings = load_project_cases(path)
        self.assertEqual(len(meetings), 1)
        self.assertEqual(len(meetings[0].sentences), 2)
        self.assertEqual(meetings[0].positive_sentence_indices, {0})


class ProductMetricTests(unittest.TestCase):
    def test_token_accuracy_separates_estimator_versions(self) -> None:
        """Old and new estimators coexist in one database; mixing them would
        hide whether the fix worked."""

        from collab_agent.product_evaluation import token_accuracy_metrics

        class FakeDb:
            def all(self, *_args, **_kwargs):
                return [
                    {
                        "payload": json.dumps(
                            {
                                "invocation": {
                                    "context": {
                                        "estimator": "old.v1",
                                        "estimated_input_tokens": 500,
                                        "purpose": "TASK_RESULT_REVIEW",
                                    },
                                    "usage": {"prompt_tokens": 2000},
                                }
                            }
                        )
                    },
                    {
                        "payload": json.dumps(
                            {
                                "invocation": {
                                    "context": {
                                        "estimator": "qwen-bpe.v1",
                                        "estimated_input_tokens": 1900,
                                        "reserved_prompt_tokens": 1100,
                                        "purpose": "TASK_RESULT_REVIEW",
                                    },
                                    "usage": {
                                        "prompt_tokens": 4000,
                                        "first_request_prompt_tokens": 2000,
                                    },
                                }
                            }
                        )
                    },
                ]

        result = token_accuracy_metrics(FakeDb())["by_estimator"]
        self.assertEqual(result["old.v1"]["mean_estimated_over_actual"], 0.25)
        self.assertFalse(result["old.v1"]["counts_prompt_overhead"])
        # The new estimator must be compared against the first request only;
        # a repair re-sends the whole prompt and would double the denominator.
        self.assertEqual(result["qwen-bpe.v1"]["mean_estimated_over_actual"], 0.95)
        self.assertTrue(result["qwen-bpe.v1"]["counts_prompt_overhead"])

    def test_citation_fidelity_flags_conclusions_on_unread_sources(self) -> None:
        from collab_agent.product_evaluation import citation_fidelity_metrics

        class FakeDb:
            def all(self, *_args, **_kwargs):
                return [
                    {
                        "version_id": "v1",
                        "processing_result": json.dumps(
                            {
                                "source_coverage": {
                                    "used_refs": ["attachment:0"],
                                    "unreadable_or_uninspected": ["link:0"],
                                },
                                "evidence_digest": {
                                    "key_points": [
                                        {"text": "ok", "source_refs": ["attachment:0"]},
                                        {"text": "bad", "source_refs": ["link:0"]},
                                    ]
                                },
                            }
                        ),
                    }
                ]

        result = citation_fidelity_metrics(FakeDb())
        self.assertEqual(result["conclusion_points"], 2)
        self.assertEqual(result["conclusions_citing_unread_sources"], 1)
        self.assertEqual(result["citation_hallucination_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()


class FrozenStateTests(unittest.TestCase):
    """A held-out number only means something if the extractor was frozen
    before the held-out set existed. Recording what ran turns that ordering
    from a claim into something the reader can check."""

    def test_every_report_records_what_produced_it(self) -> None:
        from collab_agent.extraction import ACTION_ITEM_EXTRACTION_PROMPT_VERSION

        report = compare_extractors(
            [meeting(["小王明天提交清单。"], {0})],
            {"keyword_floor": keyword_extractor},
        )
        state = report["frozen_state"]
        self.assertEqual(
            state["prompt_version"], ACTION_ITEM_EXTRACTION_PROMPT_VERSION
        )
        self.assertIn("commit", state)
        self.assertIn("run_at", state)
        # A dirty tree means the committed prompt is not what actually ran.
        self.assertIn("working_tree_clean", state)


class RosterResolutionTests(unittest.TestCase):
    """A transcript calls people whatever the speakers call them. The extractor
    has no roster, so it writes the spoken form, and an exact string comparison
    then reports a correctly identified person as an error."""

    ROSTER = ["黄Z恒", "王昱翔", "榨椰汁", "张静雅", "绒", "宋潽暄"]

    def test_a_fuller_spoken_form_resolves_to_the_roster_entry(self) -> None:
        from collab_agent.extraction_evaluation import resolve_participant

        self.assertEqual(resolve_participant("苏绒", self.ROSTER), "绒")
        self.assertEqual(resolve_participant("静雅", self.ROSTER), "张静雅")

    def test_a_nickname_that_is_not_a_substring_stays_unresolved(self) -> None:
        """«子恒» for «黄Z恒» needs the human-confirmed alias map. Guessing here
        would let the scorer credit an identification the system never made."""

        from collab_agent.extraction_evaluation import resolve_participant

        self.assertIsNone(resolve_participant("子恒", self.ROSTER))

    def test_an_ambiguous_name_refuses_to_resolve(self) -> None:
        from collab_agent.extraction_evaluation import resolve_participant

        self.assertIsNone(resolve_participant("小明", ["小明月", "小明星"]))

    def test_a_sentence_fragment_does_not_swallow_a_roster_name(self) -> None:
        from collab_agent.extraction_evaluation import resolve_participant

        self.assertIsNone(
            resolve_participant("那你就需要在文案这边多下点功夫", self.ROSTER)
        )

    def test_owner_scoring_credits_the_same_person_named_differently(self) -> None:
        target = LabelledMeeting(
            meeting_id="m",
            transcript="就苏绒和金霞你们两个去写脚本。",
            sentences=["就苏绒和金霞你们两个去写脚本。"],
            positive_sentence_indices={0},
            expected_items=[
                {
                    "title": "写脚本",
                    "source_quote": "就苏绒和金霞你们两个去写脚本。",
                    "owner_name": "绒",
                }
            ],
            participants=self.ROSTER,
        )
        score = score_items(
            target,
            [
                {
                    "title": "写脚本",
                    "source_quote": "就苏绒和金霞你们两个去写脚本。",
                    "owner_name": "苏绒",
                }
            ],
        )
        self.assertEqual(score["field_accuracy"]["owner_name"], 1.0)

    def test_a_genuinely_wrong_person_is_still_wrong(self) -> None:
        target = LabelledMeeting(
            meeting_id="m",
            transcript="就苏绒去写脚本。",
            sentences=["就苏绒去写脚本。"],
            positive_sentence_indices={0},
            expected_items=[
                {"title": "写脚本", "source_quote": "就苏绒去写脚本。", "owner_name": "绒"}
            ],
            participants=self.ROSTER,
        )
        score = score_items(
            target,
            [{"title": "写脚本", "source_quote": "就苏绒去写脚本。", "owner_name": "王昱翔"}],
        )
        self.assertEqual(score["field_accuracy"]["owner_name"], 0.0)
