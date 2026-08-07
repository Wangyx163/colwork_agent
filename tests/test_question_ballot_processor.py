from __future__ import annotations

import unittest

from collab_agent.question_ballot import (
    BailianQuestionBallotDraftProcessor,
    deterministic_ballot_options,
)


class QuestionBallotProcessorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = [
            {
                "source_ref": "upstream:a:v1",
                "action_item_id": "a",
                "accepted_version_id": "v1",
                "title": "甲的问题",
                "completion_report": "如何降低发布风险？；怎样验证用户需求？",
                "normalized_result": None,
            },
            {
                "source_ref": "upstream:b:v2",
                "action_item_id": "b",
                "accepted_version_id": "v2",
                "title": "乙的问题",
                "completion_report": "怎样验证用户需求？；什么指标能反映效果？",
                "normalized_result": None,
            },
        ]

    def test_rules_deduplicate_and_preserve_all_sources(self) -> None:
        options = deterministic_ballot_options(self.sources)
        self.assertEqual(len(options), 3)
        duplicated = next(
            option for option in options if option["text"] == "怎样验证用户需求？"
        )
        self.assertEqual(
            {ref["action_item_id"] for ref in duplicated["source_refs"]},
            {"a", "b"},
        )

    def test_bailian_contract_repairs_once_and_uses_qwen_plus(self) -> None:
        requests = []
        responses = iter(
            [
                {"choices": [{"message": {"content": '{"options": []}'}}]},
                {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"questions":['
                                    '{"question":"如何降低发布风险？",'
                                    '"source_ref":"upstream:a:v1"},'
                                    '{"question":"什么指标能反映效果？",'
                                    '"sources":["upstream:b:v2"]}]}'
                                )
                            }
                        }
                    ]
                },
            ]
        )

        def transport(payload):
            requests.append(payload)
            return next(responses)

        result = BailianQuestionBallotDraftProcessor(
            api_key="test", transport=transport
        ).process(self.sources)
        self.assertEqual(result["model"], "qwen-plus")
        self.assertEqual(result["repair_count"], 1)
        self.assertEqual(len(result["options"]), 2)
        self.assertEqual(
            result["context_manifest"]["purpose"],
            "QUESTION_BALLOT_DRAFT",
        )
        self.assertTrue(result["input_hash"])
        self.assertEqual(result["usage"]["total_tokens"], 0)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["temperature"], 0)


if __name__ == "__main__":
    unittest.main()
