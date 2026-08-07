from __future__ import annotations

import contextlib
import unittest
from unittest import mock

from collab_agent import context_budget
from collab_agent.context_budget import (
    ContextBudgetError,
    bound_task_result_context,
    count_text_tokens,
    estimate_tokens,
    token_estimator_name,
)


@contextlib.contextmanager
def without_tokenizer():
    """Force the offline path without reloading the module.

    Reloading would rebind ContextBudgetError to a new class object and make
    assertRaises stop matching.
    """

    with mock.patch.object(context_budget, "_tokenizer", None), mock.patch.object(
        context_budget, "_tokenizer_loaded", True
    ):
        yield


class TokenEstimationTests(unittest.TestCase):
    def test_the_fallback_never_undercounts_serialized_json(self) -> None:
        """The old bytes/3 estimator undercounted JSON with long hex ids at
        0.72x of the real Qwen count, which let over-budget contexts through."""

        with without_tokenizer():
            self.assertEqual(
                token_estimator_name(), context_budget.FALLBACK_ESTIMATOR
            )
            sample = (
                '{"version_id":"ver_8186ea12467041a7baa267f982542288",'
                '"title":"完成街采视频方案","status":"PENDING_ACCEPTANCE"}'
            )
            legacy = len(sample.encode("utf-8")) // 3
            self.assertGreater(
                count_text_tokens(sample),
                legacy,
                "the fallback must be more conservative than bytes/3 on JSON",
            )

    def test_the_fallback_is_deterministic(self) -> None:
        with without_tokenizer():
            sample = "每个人准备七八个采访问题发给子恒" * 20
            self.assertEqual(count_text_tokens(sample), count_text_tokens(sample))

    def test_empty_text_costs_nothing(self) -> None:
        self.assertEqual(count_text_tokens(""), 0)

    def test_estimator_name_is_recorded_in_the_manifest(self) -> None:
        _, manifest = bound_task_result_context(
            {"processing_purpose": "TASK_RESULT_REVIEW", "task_contract": {}},
            token_budget=5_000,
        )
        self.assertEqual(manifest["estimator"], token_estimator_name())

    def test_reserved_prompt_tokens_are_charged_to_the_budget(self) -> None:
        """The system prompt travels with every request; a guard that ignores it
        admits contexts whose real request runs far over budget."""

        context = {
            "processing_purpose": "TASK_RESULT_REVIEW",
            "submission_claim": {"content": "问题清单" * 400},
        }
        _, unreserved = bound_task_result_context(context, token_budget=4_000)
        _, reserved = bound_task_result_context(
            context, token_budget=4_000, reserved_tokens=1_200
        )
        self.assertEqual(reserved["reserved_prompt_tokens"], 1_200)
        self.assertEqual(reserved["token_budget"], 4_000)
        self.assertEqual(reserved["context_token_budget"], 2_800)
        self.assertEqual(
            reserved["estimated_input_tokens"],
            reserved["estimated_context_tokens"] + 1_200,
        )
        self.assertLessEqual(
            reserved["estimated_context_tokens"],
            unreserved["estimated_context_tokens"],
        )
        self.assertLessEqual(reserved["estimated_input_tokens"], 4_000)

    def test_prompt_overhead_larger_than_budget_fails_closed(self) -> None:
        with self.assertRaisesRegex(ContextBudgetError, "prompt overhead"):
            bound_task_result_context(
                {"processing_purpose": "TASK_RESULT_REVIEW"},
                token_budget=500,
                reserved_tokens=800,
            )

    def test_task_result_prompt_overhead_is_counted(self) -> None:
        from collab_agent.task_result_processing import (
            task_result_prompt_overhead_tokens,
        )

        overhead = task_result_prompt_overhead_tokens()
        # The system prompt is ~2600 characters of Chinese contract text; any
        # value near zero means it stopped being counted.
        self.assertGreater(overhead, 500)
        self.assertLess(overhead, 5_000)

    def test_estimate_accepts_strings_and_objects(self) -> None:
        self.assertGreater(estimate_tokens("一段中文"), 0)
        self.assertGreater(estimate_tokens({"a": "一段中文"}), 0)


class UsageAccountingTests(unittest.TestCase):
    def test_repair_calls_are_separated_from_the_first_request(self) -> None:
        """Summing both requests conflates attempt cost with prompt size; the
        pre-call estimate can only be compared against the first request."""

        from collab_agent.task_result_processing import BailianTaskResultProcessor

        usage = BailianTaskResultProcessor._usage(
            [
                {"usage": {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}},
                {"usage": {"prompt_tokens": 1050, "completion_tokens": 120, "total_tokens": 1170}},
            ]
        )
        self.assertEqual(usage["prompt_tokens"], 2050)
        self.assertEqual(usage["first_request_prompt_tokens"], 1000)
        self.assertEqual(usage["request_count"], 2)

    def test_a_single_request_reports_itself_as_the_first(self) -> None:
        from collab_agent.task_result_processing import BailianTaskResultProcessor

        usage = BailianTaskResultProcessor._usage(
            [{"usage": {"prompt_tokens": 800, "completion_tokens": 50, "total_tokens": 850}}]
        )
        self.assertEqual(usage["prompt_tokens"], 800)
        self.assertEqual(usage["first_request_prompt_tokens"], 800)
        self.assertEqual(usage["request_count"], 1)


if __name__ == "__main__":
    unittest.main()
