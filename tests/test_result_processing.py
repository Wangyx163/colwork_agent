from __future__ import annotations

import base64
from copy import deepcopy
import http.client
import json
import unittest
from unittest.mock import patch

from collab_agent.attachments import extract_attachment_text
from collab_agent.result_processing import (
    BailianResultOrganizer,
    ResultProcessingError,
    build_deterministic_report,
    validate_organized_report,
)


class ResultProcessingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = [
            {
                "action_item_id": "ai_research",
                "title": "调研平台机制",
                "version_id": "ver_current",
                "payload": {
                    "summary": "推荐机制与完播率相关",
                    "content": "调研正文",
                    "links": ["https://example.com/source"],
                },
                "attachments": [],
                "accepted_task_result": {
                    "accepted_task_result_id": "result_current",
                    "completion_report": "机制调研已完成并通过验收",
                    "normalized_result": None,
                },
            }
        ]

    def test_deterministic_report_is_readable_and_cited(self) -> None:
        report = build_deterministic_report(self.sources)
        validated = validate_organized_report(report, self.sources)
        self.assertEqual(
            validated["sections"][0]["summary"],
            "机制调研已完成并通过验收",
        )
        self.assertEqual(
            validated["sections"][0]["source_version_id"], "ver_current"
        )
        self.assertEqual(
            validated["sections"][0]["accepted_task_result_id"],
            "result_current",
        )
        self.assertEqual(
            validated["key_findings"][0]["source_version_ids"], ["ver_current"]
        )

    def test_stale_or_invented_model_citation_is_rejected(self) -> None:
        report = build_deterministic_report(self.sources)
        report["sections"][0]["source_version_id"] = "ver_old"
        with self.assertRaisesRegex(ResultProcessingError, "stale version"):
            validate_organized_report(report, self.sources)

    def test_stale_accepted_result_citation_is_rejected(self) -> None:
        report = build_deterministic_report(self.sources)
        report["sections"][0]["accepted_task_result_id"] = "result_old"
        with self.assertRaisesRegex(ResultProcessingError, "stale accepted"):
            validate_organized_report(report, self.sources)

    def test_text_attachment_is_extracted_without_forwarding_binary_data(self) -> None:
        content = "附件中的有效调研结论"
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        result = extract_attachment_text(
            {
                "name": "result.txt",
                "type": "text/plain",
                "size": len(content.encode("utf-8")),
                "data": f"data:text/plain;base64,{encoded}",
            }
        )
        self.assertEqual(result["extraction_status"], "EXTRACTED")
        self.assertEqual(result["extracted_text"], content)
        self.assertNotIn("data", result)

    def test_unsupported_and_malformed_attachments_are_classified(self) -> None:
        # PDF/text/DOCX/XLSX/PPTX are the supported evidence formats; anything
        # else must stay UNSUPPORTED rather than silently contribute no text.
        unsupported = extract_attachment_text(
            {
                "name": "result.png",
                "type": "image/png",
                "size": 4,
                "data": "data:image/png;base64,AAAAAA==",
            }
        )
        self.assertEqual(unsupported["extraction_status"], "UNSUPPORTED")
        self.assertNotIn("data", unsupported)
        malformed = extract_attachment_text(
            {
                "name": "broken.pdf",
                "type": "application/pdf",
                "size": 8,
                "data": "not-a-data-url",
            }
        )
        self.assertEqual(malformed["extraction_status"], "FAILED")
        self.assertTrue(malformed["extraction_error"])
        self.assertNotIn("data", malformed)

    def test_final_organizer_non_json_response_is_controlled(self) -> None:
        class NonJsonResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self) -> bytes:
                return b"not-json"

        with patch(
            "collab_agent.result_processing.urllib.request.urlopen",
            return_value=NonJsonResponse(),
        ):
            with self.assertRaisesRegex(ResultProcessingError, "non-JSON"):
                BailianResultOrganizer(api_key="test-key").organize(self.sources)

    def test_bailian_connection_drop_becomes_a_controlled_fallback_error(self) -> None:
        with patch(
            "collab_agent.result_processing.urllib.request.urlopen",
            side_effect=http.client.RemoteDisconnected("closed"),
        ):
            with self.assertRaisesRegex(ResultProcessingError, "request failed"):
                BailianResultOrganizer(
                    api_key="test-key", max_request_attempts=1
                ).organize(self.sources)

    def test_transient_connection_drop_is_retried_inside_adapter(self) -> None:
        expected = build_deterministic_report(self.sources)

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
                            {"message": {"content": json.dumps(expected)}}
                        ],
                    }
                ).encode("utf-8")

        with patch(
            "collab_agent.result_processing.urllib.request.urlopen",
            side_effect=[ConnectionResetError(10054, "reset"), JsonResponse()],
        ) as urlopen:
            _, metadata = BailianResultOrganizer(
                api_key="test-key", retry_backoff_seconds=0
            ).organize(self.sources)
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(metadata["request_attempts"], 2)

    def test_missing_report_title_is_normalized_without_retry(self) -> None:
        repaired = build_deterministic_report(self.sources)
        invalid = deepcopy(repaired)
        invalid.pop("title")

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
            "collab_agent.result_processing.urllib.request.urlopen",
            return_value=JsonResponse(invalid),
        ) as urlopen:
            report, metadata = BailianResultOrganizer(
                api_key="test-key"
            ).organize(self.sources)
        self.assertEqual(report["title"], "会议行动项成果汇总")
        self.assertEqual(metadata["repair_count"], 0)
        self.assertIn("title<-deterministic_title", metadata["normalization_actions"])
        self.assertEqual(metadata["usage"]["total_tokens"], 15)
        self.assertEqual(urlopen.call_count, 1)

    def test_stale_report_citation_is_repaired_once(self) -> None:
        repaired = build_deterministic_report(self.sources)
        invalid = deepcopy(repaired)
        invalid["sections"][0]["source_version_id"] = "ver_stale"

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
            "collab_agent.result_processing.urllib.request.urlopen",
            side_effect=[JsonResponse(invalid), JsonResponse(repaired)],
        ) as urlopen:
            report, metadata = BailianResultOrganizer(
                api_key="test-key"
            ).organize(self.sources)
        self.assertEqual(report["sections"][0]["source_version_id"], "ver_current")
        self.assertEqual(metadata["repair_count"], 1)
        self.assertIn("stale version", metadata["contract_validation_errors"][0])
        self.assertEqual(metadata["usage"]["total_tokens"], 30)
        self.assertEqual(urlopen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
