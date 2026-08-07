from __future__ import annotations

from copy import deepcopy
import http.client
import json
import unittest
from unittest.mock import patch

from collab_agent.task_result_processing import (
    BailianTaskResultProcessor,
    TaskResultProcessingError,
    build_task_result_context,
    validate_task_result,
)


class TaskResultProcessingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = build_task_result_context(
            action_item_id="ai_1",
            title="调研抖音底层算法",
            deliverable="调研报告",
            acceptance_criteria="说明核心机制",
            source_timestamp="00:10:00",
            source_quote="请调研抖音底层算法",
            version_id="ver_1",
            payload={"summary": "已完成", "links": ["https://example.com"]},
            attachments=[
                {
                    "name": "unrelated.pdf",
                    "type": "application/pdf",
                    "size": 100,
                    "extraction_status": "EXTRACTED",
                    "text_characters": 8,
                    "truncated": False,
                    "extracted_text": "状态缓存方法",
                }
            ],
            work_requirements="正文说明机制，证据单独列出",
            management_review_policy="负责人检查结论是否可追溯",
        )
        self.result = {
            "task_interpretation": "需要提交抖音算法调研报告",
            "task_alignment": {
                "status": "MISALIGNED",
                "reason": "附件内容是状态缓存方法，与任务主题不同",
                "confidence": 0.99,
            },
            "evidence_digest": {
                "title": "当前附件内容摘要",
                "summary": "附件讨论状态缓存，不能作为抖音算法调研成果",
                "key_points": [
                    {"text": "附件讨论状态缓存", "source_refs": ["attachment:0"]}
                ],
            },
            "normalized_result": None,
            "gaps": [
                {
                    "issue": "缺少抖音算法调研内容",
                    "severity": "BLOCKING",
                    "source_refs": ["task:contract", "attachment:0"],
                }
            ],
            "acceptance_advice": {
                "decision": "REVISE",
                "reasons": ["请提交与抖音算法相关的报告"],
            },
            "source_coverage": {
                "used_refs": ["task:contract", "submission:claim", "attachment:0"],
                "unreadable_or_uninspected": ["link:0"],
            },
        }

    def test_context_keeps_contract_claim_and_evidence_separate(self) -> None:
        self.assertEqual(self.context["task_contract"]["title"], "调研抖音底层算法")
        self.assertEqual(
            self.context["task_contract"]["work_requirements"],
            "正文说明机制，证据单独列出",
        )
        self.assertEqual(
            self.context["task_contract"]["management_review_policy"],
            "负责人检查结论是否可追溯",
        )
        self.assertEqual(self.context["submission_claim"]["summary"], "已完成")
        self.assertEqual(
            self.context["evidence"]["attachments"][0]["source_ref"],
            "attachment:0",
        )
        self.assertNotIn("data", self.context["evidence"]["attachments"][0])

    def test_misaligned_result_is_valid_and_requires_revision(self) -> None:
        result = validate_task_result(self.result, self.context)
        self.assertEqual(result["task_alignment"]["status"], "MISALIGNED")
        self.assertEqual(result["acceptance_advice"]["decision"], "REVISE")

    def test_key_point_cannot_cite_uninspected_link(self) -> None:
        self.result["evidence_digest"]["key_points"][0]["source_refs"] = ["link:0"]
        with self.assertRaisesRegex(TaskResultProcessingError, "inspected evidence"):
            validate_task_result(self.result, self.context)

    def test_gap_can_cite_uninspected_link_as_missing_evidence(self) -> None:
        self.result["gaps"][0] = {
            "issue": "提交了链接，但系统没有读取链接内容",
            "severity": "MAJOR",
            "source_refs": ["task:contract", "link:0"],
        }
        result = validate_task_result(self.result, self.context)
        self.assertEqual(
            result["gaps"][0]["source_refs"], ["link:0", "task:contract"]
        )

    def test_gap_still_rejects_a_hallucinated_source_ref(self) -> None:
        self.result["gaps"][0]["source_refs"] = ["link:99"]
        with self.assertRaisesRegex(TaskResultProcessingError, "link:99"):
            validate_task_result(self.result, self.context)

    def test_misaligned_result_cannot_recommend_acceptance(self) -> None:
        self.result["acceptance_advice"]["decision"] = "ACCEPT"
        with self.assertRaisesRegex(TaskResultProcessingError, "must be revised"):
            validate_task_result(self.result, self.context)

    def test_misaligned_evidence_cannot_become_a_task_result(self) -> None:
        self.result["normalized_result"] = {
            "title": "错误成果",
            "executive_summary": "不应生成",
            "key_points": [],
        }
        with self.assertRaisesRegex(TaskResultProcessingError, "cannot produce"):
            validate_task_result(self.result, self.context)

    def test_connection_drop_is_a_controlled_error(self) -> None:
        with patch(
            "collab_agent.task_result_processing.urllib.request.urlopen",
            side_effect=http.client.RemoteDisconnected("closed"),
        ):
            with self.assertRaisesRegex(TaskResultProcessingError, "request failed"):
                BailianTaskResultProcessor(
                    api_key="test-key", max_request_attempts=1
                ).process(self.context)

    def test_transient_connection_drop_is_retried_inside_adapter(self) -> None:
        class JsonResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "model": "qwen-plus",
                        "choices": [
                            {"message": {"content": json.dumps(self_outer.result)}}
                        ],
                    }
                ).encode("utf-8")

        self_outer = self
        with patch(
            "collab_agent.task_result_processing.urllib.request.urlopen",
            side_effect=[ConnectionResetError(10054, "reset"), JsonResponse()],
        ) as urlopen:
            _, metadata = BailianTaskResultProcessor(
                api_key="test-key", retry_backoff_seconds=0
            ).process(self.context)
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(metadata["request_attempts"], 2)

    def test_non_json_provider_response_has_structured_failure(self) -> None:
        class NonJsonResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self) -> bytes:
                return b"not-json"

        with patch(
            "collab_agent.task_result_processing.urllib.request.urlopen",
            return_value=NonJsonResponse(),
        ):
            with self.assertRaises(TaskResultProcessingError) as raised:
                BailianTaskResultProcessor(api_key="test-key").process(self.context)
        self.assertEqual(raised.exception.error_code, "INVALID_JSON")
        self.assertEqual(raised.exception.stage, "MODEL_RESPONSE")
        self.assertTrue(raised.exception.retryable)

    def test_missing_presentation_fields_are_normalized_without_retry(self) -> None:
        invalid = deepcopy(self.result)
        invalid["task_alignment"] = {
            "status": "PARTIAL",
            "reason": "证据只覆盖部分任务要求",
            "confidence": 0.8,
        }
        invalid["normalized_result"] = {
            "key_points": deepcopy(invalid["evidence_digest"]["key_points"]),
        }
        class JsonResponse:
            def __init__(self, candidate: dict):
                self.candidate = candidate

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "model": "qwen-plus",
                        "choices": [
                            {"message": {"content": json.dumps(self.candidate)}}
                        ],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                        },
                    }
                ).encode("utf-8")

        with patch(
            "collab_agent.task_result_processing.urllib.request.urlopen",
            return_value=JsonResponse(invalid),
        ) as urlopen:
            result, metadata = BailianTaskResultProcessor(
                api_key="test-key"
            ).process(self.context)
        self.assertEqual(result["normalized_result"]["title"], "当前附件内容摘要")
        self.assertEqual(
            result["normalized_result"]["executive_summary"],
            "附件讨论状态缓存，不能作为抖音算法调研成果",
        )
        self.assertEqual(metadata["repair_count"], 0)
        self.assertIn(
            "normalized_result.title<-evidence_digest.title",
            metadata["normalization_actions"],
        )
        self.assertEqual(metadata["usage"]["total_tokens"], 15)
        self.assertEqual(urlopen.call_count, 1)

    def test_invalid_evidence_citation_is_repaired_once(self) -> None:
        invalid = deepcopy(self.result)
        invalid["evidence_digest"]["key_points"][0]["source_refs"] = ["link:0"]
        repaired = deepcopy(self.result)

        class JsonResponse:
            def __init__(self, candidate: dict):
                self.candidate = candidate

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "model": "qwen-plus",
                        "choices": [
                            {"message": {"content": json.dumps(self.candidate)}}
                        ],
                        "usage": {"total_tokens": 15},
                    }
                ).encode("utf-8")

        with patch(
            "collab_agent.task_result_processing.urllib.request.urlopen",
            side_effect=[JsonResponse(invalid), JsonResponse(repaired)],
        ) as urlopen:
            result, metadata = BailianTaskResultProcessor(
                api_key="test-key"
            ).process(self.context)
        self.assertEqual(result["task_alignment"]["status"], "MISALIGNED")
        self.assertEqual(metadata["repair_count"], 1)
        self.assertIn("inspected evidence", metadata["contract_validation_errors"][0])
        self.assertEqual(metadata["usage"]["total_tokens"], 30)
        self.assertEqual(urlopen.call_count, 2)

    def test_repeated_semantic_contract_failure_stops_after_one_repair(self) -> None:
        invalid = deepcopy(self.result)
        invalid["evidence_digest"]["key_points"][0]["source_refs"] = ["link:0"]

        class JsonResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "model": "qwen-plus",
                        "choices": [
                            {"message": {"content": json.dumps(invalid)}}
                        ],
                    }
                ).encode("utf-8")

        with patch(
            "collab_agent.task_result_processing.urllib.request.urlopen",
            side_effect=[JsonResponse(), JsonResponse()],
        ) as urlopen:
            with self.assertRaises(TaskResultProcessingError) as raised:
                BailianTaskResultProcessor(api_key="test-key").process(self.context)
        self.assertEqual(raised.exception.error_code, "INVALID_SCHEMA")
        self.assertEqual(raised.exception.stage, "MODEL_RESPONSE")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(urlopen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
