from __future__ import annotations

import unittest

from collab_agent.extraction import BailianExtractor, summarize_token_calls


def _payload(total: int, *, index: int = 0, attempts: int = 1) -> dict:
    return {
        "model": "qwen-plus",
        "_adapter_request_attempts": attempts,
        "usage": {
            "prompt_tokens": total - 100,
            "completion_tokens": 100,
            "total_tokens": total,
        },
    }


class PerCallCaptureTests(unittest.TestCase):
    def test_one_row_per_call_in_order(self) -> None:
        calls = BailianExtractor._per_call_usage(
            [_payload(1800), _payload(2400), _payload(4910)]
        )

        self.assertEqual([c["call_index"] for c in calls], [1, 2, 3])
        self.assertEqual([c["total_tokens"] for c in calls], [1800, 2400, 4910])

    def test_a_payload_without_usage_is_skipped_not_zeroed(self) -> None:
        """A missing usage is unknown spend, not free spend."""

        calls = BailianExtractor._per_call_usage(
            [_payload(1800), {"model": "qwen-plus"}, _payload(2400)]
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual([c["total_tokens"] for c in calls], [1800, 2400])

    def test_retry_attempts_are_reported_not_folded_in(self) -> None:
        calls = BailianExtractor._per_call_usage([_payload(1800, attempts=3)])

        self.assertEqual(calls[0]["request_attempts"], 3)


class SummaryTests(unittest.TestCase):
    def test_quartiles_over_a_small_sample(self) -> None:
        calls = BailianExtractor._per_call_usage(
            [_payload(v) for v in (1800, 2400, 2780, 3200, 4910)]
        )

        summary = summarize_token_calls(calls)

        self.assertEqual(summary["calls"], 5)
        self.assertEqual(summary["total_tokens"], 15090)
        self.assertEqual(summary["median"], 2780)
        self.assertEqual(summary["min"], 1800)
        self.assertEqual(summary["max"], 4910)
        self.assertLess(summary["p25"], summary["median"])
        self.assertGreater(summary["p75"], summary["median"])

    def test_an_expensive_call_is_flagged_rather_than_smoothed_away(self) -> None:
        calls = BailianExtractor._per_call_usage(
            [_payload(v) for v in (1800, 1850, 1900, 1950, 9000)]
        )

        self.assertEqual(summarize_token_calls(calls)["outliers"], [9000])

    def test_an_even_spread_flags_nothing(self) -> None:
        calls = BailianExtractor._per_call_usage(
            [_payload(v) for v in (2000, 2200, 2400, 2600, 2800)]
        )

        self.assertEqual(summarize_token_calls(calls)["outliers"], [])

    def test_a_run_with_no_model_call_says_so(self) -> None:
        """The deterministic evaluation spends nothing; that is its point."""

        summary = summarize_token_calls([])

        self.assertEqual(summary["calls"], 0)
        self.assertEqual(summary["total_tokens"], 0)
        self.assertIsNone(summary["median"])
        self.assertIn("没有产生外部模型调用", summary["note"])

    def test_a_single_call_yields_degenerate_but_valid_quartiles(self) -> None:
        summary = summarize_token_calls(
            BailianExtractor._per_call_usage([_payload(3000)])
        )

        self.assertEqual(summary["median"], 3000)
        self.assertEqual(summary["p25"], 3000)
        self.assertEqual(summary["iqr"], 0)
        self.assertEqual(summary["outliers"], [])

    def test_the_total_matches_the_existing_aggregate(self) -> None:
        """The new breakdown must not disagree with the number already shipped."""

        payloads = [_payload(1800), _payload(2400), _payload(4910)]

        legacy = BailianExtractor._usage(payloads)["total_tokens"]
        detailed = summarize_token_calls(
            BailianExtractor._per_call_usage(payloads)
        )["total_tokens"]

        self.assertEqual(legacy, detailed)


if __name__ == "__main__":
    unittest.main()
