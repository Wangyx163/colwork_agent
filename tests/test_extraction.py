from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from collab_agent.extraction import (
    ACTION_ITEM_EXTRACTION_PROMPT_VERSION,
    align_collaboration_evidence,
    align_owner_evidence,
    align_source_evidence,
    BailianExtractor,
    ExtractionError,
    normalize_extraction_payload,
    validate_extraction,
    validate_source_evidence,
)


def valid_item() -> dict:
    return {
        "title": "调研平台机制",
        "deliverable": "一份调研摘要",
        "owner_name": None,
        "deadline_text": None,
        "deadline_iso": None,
        "source_timestamp": "00:32:42",
        "source_quote": "这个就是需要有人去调研一下",
        "confidence": 0.86,
        "needs_confirmation": True,
        "uncertainties": ["负责人未明确", "截止时间未明确"],
        "collaborator_names": [],
    }


class ExtractionContractTests(unittest.TestCase):
    class _Response:
        def __init__(self, payload: bytes):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self) -> bytes:
            return self.payload

    def test_valid_unresolved_candidate(self) -> None:
        items = validate_extraction({"action_items": [valid_item()]})
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0].owner_name)
        self.assertTrue(items[0].needs_confirmation)

    def test_unresolved_candidate_cannot_skip_confirmation(self) -> None:
        item = valid_item()
        item["needs_confirmation"] = False
        with self.assertRaisesRegex(ExtractionError, "must need confirmation"):
            validate_extraction({"action_items": [item]})

    def test_structural_normalization_forces_unresolved_candidate_to_confirm(self) -> None:
        item = valid_item()
        item["owner_name"] = "  "
        item["deadline_text"] = ""
        item["needs_confirmation"] = False
        item["uncertainties"] = "截止时间未明确"
        payload, actions = normalize_extraction_payload({"action_items": [item]})
        items = validate_extraction(payload)
        self.assertIsNone(items[0].owner_name)
        self.assertTrue(items[0].needs_confirmation)
        self.assertIn("截止时间未明确", items[0].uncertainties)
        self.assertTrue(
            any("false_to_true_for_unresolved_fields" in action for action in actions)
        )

    def test_structural_normalization_does_not_invent_semantic_fields(self) -> None:
        item = valid_item()
        del item["title"]
        item["needs_confirmation"] = False
        payload, _ = normalize_extraction_payload({"action_items": [item]})
        with self.assertRaisesRegex(ExtractionError, "missing fields"):
            validate_extraction(payload)

    def test_model_contract_contradiction_is_normalized_without_another_call(self) -> None:
        transcript = "主持人(00:32:42): 这个就是需要有人去调研一下"
        item = valid_item()
        item["needs_confirmation"] = False
        response = {
            "model": "qwen-plus",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"action_items": [item]}, ensure_ascii=False
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 30},
        }
        with patch(
            "collab_agent.extraction.urllib.request.urlopen",
            return_value=self._Response(
                json.dumps(response, ensure_ascii=False).encode("utf-8")
            ),
        ) as urlopen:
            result = BailianExtractor(api_key="test-key").extract(transcript)
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(result["model_call_count"], 1)
        self.assertTrue(result["action_items"][0]["needs_confirmation"])
        self.assertEqual(result["normalization_count"], 1)
        self.assertEqual(result["summary"]["normalization_count"], 1)

    def test_duplicate_candidates_are_collapsed(self) -> None:
        item = valid_item()
        items = validate_extraction({"action_items": [item, dict(item)]})
        self.assertEqual(len(items), 1)

    def test_source_quote_must_exist_at_declared_timestamp(self) -> None:
        items = validate_extraction({"action_items": [valid_item()]})
        transcript = "主持人(00:32:42): 这个就是需要有人去调研一下"
        validate_source_evidence(items, transcript)
        with self.assertRaisesRegex(ExtractionError, "source quote is not present"):
            validate_source_evidence(
                items,
                "主持人(00:32:43): 这个就是需要有人去调研一下",
            )

    def test_speaker_suggestion_is_not_treated_as_owner_commitment(self) -> None:
        item = valid_item()
        item["owner_name"] = "Jasmine"
        item["source_timestamp"] = "00:01:44"
        item["source_quote"] = "我觉得可以针对不同的群体去做不同的预设一些问题"
        transcript = (
            "Jasmine(00:01:44): 我觉得可以针对不同的群体去做不同的预设一些问题，"
            "然后偏生活一些。"
        )
        items = validate_extraction({"action_items": [item]})
        aligned, repairs = align_owner_evidence(items, transcript)
        self.assertEqual(repairs, 1)
        self.assertIsNone(aligned[0].owner_name)
        self.assertTrue(aligned[0].needs_confirmation)

    def test_explicit_assignment_preserves_owner_hint(self) -> None:
        item = valid_item()
        item["owner_name"] = "静雅"
        item["source_timestamp"] = "00:34:25"
        item["source_quote"] = "静雅直接就起另一个思路"
        transcript = (
            "王昱翔(00:34:25): 静雅直接就起另一个思路，然后看看拍照挑战怎么做好。"
        )
        items = validate_extraction({"action_items": [item]})
        aligned, repairs = align_owner_evidence(items, transcript)
        self.assertEqual(repairs, 0)
        self.assertEqual(aligned[0].owner_name, "静雅")

    def test_explicit_named_collaboration_is_preserved(self) -> None:
        item = valid_item()
        item["collaborator_names"] = ["乙", "乙"]
        item["source_timestamp"] = "00:10:00"
        item["source_quote"] = "甲负责整理，乙配合甲一起完成"
        transcript = "主持人(00:10:00): 甲负责整理，乙配合甲一起完成"
        items = validate_extraction({"action_items": [item]})
        aligned, repairs = align_collaboration_evidence(items, transcript)
        self.assertEqual(repairs, 0)
        self.assertEqual(aligned[0].collaborator_names, ["乙"])

    def test_collaboration_without_matching_source_is_removed(self) -> None:
        item = valid_item()
        item["collaborator_names"] = ["乙"]
        transcript = "主持人(00:32:42): 这个就是需要有人去调研一下"
        items = validate_extraction({"action_items": [item]})
        aligned, repairs = align_collaboration_evidence(items, transcript)
        self.assertEqual(repairs, 1)
        self.assertEqual(aligned[0].collaborator_names, [])
        self.assertTrue(aligned[0].needs_confirmation)
        self.assertIn("明确合作指派", aligned[0].uncertainties[-1])

    def test_participant_cannot_assign_default_collaborators(self) -> None:
        item = valid_item()
        item["collaborator_names"] = ["乙"]
        item["source_timestamp"] = "00:10:00"
        item["source_quote"] = "甲负责整理，乙配合甲一起完成"
        transcript = "普通参会者(00:10:00): 甲负责整理，乙配合甲一起完成"
        items = validate_extraction({"action_items": [item]})
        aligned, repairs = align_collaboration_evidence(
            items, transcript, authorized_speaker="会议组织人"
        )
        self.assertEqual(repairs, 1)
        self.assertEqual(aligned[0].collaborator_names, [])

    def test_source_quote_can_be_aligned_to_exact_utterance(self) -> None:
        item = valid_item()
        item["source_quote"] = "然后就是抖音指数怎么把它用好就可以了"
        items = validate_extraction({"action_items": [item]})
        transcript = (
            "主持人(00:32:42): 只要是调研一下抖音指数怎么把它用好就可以了，"
            "因为它跟热点强相关。"
        )
        aligned, repairs = align_source_evidence(items, transcript)
        self.assertEqual(repairs, 1)
        self.assertIn(aligned[0].source_quote, transcript)
        validate_source_evidence(aligned, transcript)

    def test_unique_exact_quote_rebinds_wrong_timestamp_without_model_retry(self) -> None:
        item = {
            "title": "完善拍照挑战思路",
            "deliverable": "拍照挑战思路",
            "owner_name": "Jasmine",
            "deadline_text": None,
            "deadline_iso": None,
            "source_timestamp": "00:34:47",
            "source_quote": (
                "静雅你完善一下那个谁，那个子恒的另一个就是拍照的计划，"
                "去完善一下那个子恒拍照那个思路"
            ),
            "confidence": 0.88,
            "needs_confirmation": True,
            "uncertainties": ["截止时间未明确"],
        }
        transcript = (
            "王昱翔(00:34:05): OK，那就普宣子恒。静雅你完善一下那个谁，"
            "那个子恒的另一个就是拍照的计划，去完善一下那个子恒拍照那个思路。\n"
            "Jasmine(00:34:47): 行，那我讲一下。"
        )
        response = {
            "model": "qwen-plus",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"action_items": [item]}, ensure_ascii=False
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 30},
        }
        with patch(
            "collab_agent.extraction.urllib.request.urlopen",
            return_value=self._Response(
                json.dumps(response, ensure_ascii=False).encode("utf-8")
            ),
        ) as urlopen:
            result = BailianExtractor(api_key="test-key").extract(transcript)
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(result["model_call_count"], 1)
        self.assertEqual(result["summary"]["evidence_repairs"], 1)
        self.assertEqual(result["summary"]["evidence_model_repair_count"], 0)
        self.assertEqual(result["action_items"][0]["source_timestamp"], "00:34:05")
        validate_source_evidence(
            validate_extraction({"action_items": result["action_items"]}),
            transcript,
        )

    def test_unique_long_anchor_rebinds_a_composite_model_quote(self) -> None:
        item = valid_item()
        item["title"] = "确认候选人账号和内容方向"
        item["deliverable"] = "候选人账号及内容方向确认结果"
        item["source_timestamp"] = "00:36:12"
        item["source_quote"] = (
            "我再问问他，问他第一个是他账号是啥，然后我想问问他的那个大概方向"
            "是什么……我还要再确定一下他是不是社牛"
        )
        transcript = (
            "王昱翔(00:35:22): 如果是这方面，我还要再确定一下他是不是社牛。\n"
            "王昱翔(00:36:12): 他账号我没问他，我没问他账号是啥？\n"
            "王昱翔(00:36:17): 我再问问他，问他第一个是他账号是啥，然后我想问问他的那个大概方向是什么行，就是他愿不愿意社恐。"
        )
        items = validate_extraction({"action_items": [item]})
        aligned, repairs = align_source_evidence(items, transcript)
        self.assertEqual(repairs, 1)
        self.assertEqual(aligned[0].source_timestamp, "00:36:17")
        self.assertLessEqual(len(aligned[0].source_quote), 160)
        validate_source_evidence(aligned, transcript)

    def test_ambiguous_long_anchor_is_not_guessed(self) -> None:
        item = valid_item()
        item["source_timestamp"] = "00:00:01"
        item["source_quote"] = "请把完整问题清单整理出来并且发到群里进行确认"
        transcript = (
            "甲(00:10:00): 请把完整问题清单整理出来，后续再讨论。\n"
            "乙(00:20:00): 请把完整问题清单整理出来，稍后发给大家。"
        )
        items = validate_extraction({"action_items": [item]})
        with self.assertRaises(ExtractionError) as raised:
            align_source_evidence(items, transcript)
        self.assertEqual(raised.exception.error_code, "INVALID_CITATION")

    def test_model_extraction_records_pre_episode_invocation_manifest(self) -> None:
        transcript = "主持人(00:32:42): 这个就是需要有人去调研一下"
        response = {
            "model": "qwen-plus",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"action_items": [valid_item()]}, ensure_ascii=False
                        )
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
            },
        }
        with patch(
            "collab_agent.extraction.urllib.request.urlopen",
            return_value=self._Response(
                json.dumps(response, ensure_ascii=False).encode("utf-8")
            ),
        ):
            result = BailianExtractor(api_key="test-key").extract(
                transcript, meeting_date="2026-08-06"
            )
        invocation = result["invocation"]
        self.assertEqual(
            result["prompt_version"], ACTION_ITEM_EXTRACTION_PROMPT_VERSION
        )
        self.assertEqual(invocation["principal"]["actor_id"], "SYSTEM")
        self.assertIsNone(invocation["principal"]["episode_id"])
        self.assertEqual(invocation["purpose"], "ACTION_ITEM_EXTRACTION")
        self.assertEqual(invocation["field_allowlist"], ["transcript", "meeting_date"])
        self.assertEqual(invocation["output_status"], "SUCCEEDED")

    def test_unaligned_quote_gets_one_evidence_only_model_repair(self) -> None:
        initial = {
            "title": "完善拍照挑战思路",
            "deliverable": "拍照挑战思路",
            "owner_name": "Jasmine",
            "deadline_text": None,
            "deadline_iso": None,
            "source_timestamp": "00:34:47",
            "source_quote": "行，那我完善一下",
            "confidence": 0.88,
            "needs_confirmation": True,
            "uncertainties": ["截止时间未明确"],
        }
        repaired = dict(initial)
        repaired["source_timestamp"] = "00:34:25"
        repaired["source_quote"] = (
            "静雅直接就起另一个思路，然后就我看看那个子恒那个拍照挑战怎么把它做好"
        )
        transcript = (
            "王昱翔(00:34:25): 静雅直接就起另一个思路，然后就我看看那个子恒那个拍照挑战怎么把它做好\n"
            "Jasmine(00:34:47): 行，那我讲一下。"
        )

        def response(candidate: dict) -> ExtractionContractTests._Response:
            payload = {
                "model": "qwen-plus",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"action_items": [candidate]}, ensure_ascii=False
                            )
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            }
            return self._Response(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
            )

        with patch(
            "collab_agent.extraction.urllib.request.urlopen",
            side_effect=[response(initial), response(repaired)],
        ) as urlopen:
            result = BailianExtractor(api_key="test-key").extract(
                transcript, meeting_date="2026-03-09"
            )
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(result["model_call_count"], 2)
        self.assertEqual(result["usage"]["total_tokens"], 60)
        self.assertEqual(result["summary"]["evidence_model_repair_count"], 1)
        self.assertEqual(result["summary"]["evidence_rejected_count"], 0)
        self.assertEqual(result["action_items"][0]["source_timestamp"], "00:34:25")
        self.assertIn(
            result["action_items"][0]["source_quote"],
            transcript,
        )

    def test_evidence_repair_cannot_change_task_semantics(self) -> None:
        initial = valid_item()
        initial["source_quote"] = "不存在的改写证据"
        changed = dict(initial)
        changed["title"] = "模型擅自修改的任务"
        changed["source_quote"] = "这个就是需要有人去调研一下"
        transcript = "主持人(00:32:42): 这个就是需要有人去调研一下"

        def response(candidate: dict) -> ExtractionContractTests._Response:
            payload = {
                "model": "qwen-plus",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"action_items": [candidate]}, ensure_ascii=False
                            )
                        }
                    }
                ],
            }
            return self._Response(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
            )

        with patch(
            "collab_agent.extraction.urllib.request.urlopen",
            side_effect=[response(initial), response(changed)],
        ):
            with self.assertRaises(ExtractionError) as raised:
                BailianExtractor(api_key="test-key").extract(transcript)
        self.assertEqual(raised.exception.error_code, "INVALID_SCHEMA")
        self.assertEqual(raised.exception.stage, "SOURCE_EVIDENCE_REPAIR")
        self.assertFalse(raised.exception.retryable)

    def test_non_json_extraction_response_is_a_structured_failure(self) -> None:
        with patch(
            "collab_agent.extraction.urllib.request.urlopen",
            return_value=self._Response(b"not-json"),
        ):
            with self.assertRaises(ExtractionError) as raised:
                BailianExtractor(
                    api_key="test-key", max_request_attempts=1
                ).extract(
                    "主持人(00:00:01): 请整理结论"
                )
        self.assertEqual(raised.exception.error_code, "INVALID_JSON")
        self.assertEqual(raised.exception.stage, "MODEL_RESPONSE")
        self.assertTrue(raised.exception.retryable)

    def test_transient_transport_failure_retries_without_new_model_call(self) -> None:
        transcript = "主持人(00:32:42): 这个就是需要有人去调研一下"
        response = {
            "model": "qwen-plus",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"action_items": [valid_item()]}, ensure_ascii=False
                        )
                    }
                }
            ],
            "usage": {"total_tokens": 30},
        }
        with patch(
            "collab_agent.extraction.urllib.request.urlopen",
            side_effect=[
                ConnectionResetError("remote reset"),
                self._Response(
                    json.dumps(response, ensure_ascii=False).encode("utf-8")
                ),
            ],
        ) as urlopen:
            result = BailianExtractor(
                api_key="test-key",
                max_request_attempts=3,
                retry_backoff_seconds=0,
            ).extract(transcript)
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(result["model_call_count"], 1)
        self.assertEqual(result["transport_attempt_count"], 2)

    def test_long_transcript_is_chunked_by_complete_utterance_lines(self) -> None:
        first_line = "甲(00:10:00): " + ("背景说明" * 180)
        second_line = (
            "主持人(00:32:42): "
            + ("补充上下文" * 150)
            + "这个就是需要有人去调研一下"
        )
        transcript = first_line + "\n" + second_line

        def response(items: list[dict]) -> ExtractionContractTests._Response:
            payload = {
                "model": "qwen-plus",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"action_items": items}, ensure_ascii=False
                            )
                        }
                    }
                ],
                "usage": {"total_tokens": 10},
            }
            return self._Response(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
            )

        with patch(
            "collab_agent.extraction.urllib.request.urlopen",
            side_effect=[response([]), response([valid_item()])],
        ) as urlopen:
            result = BailianExtractor(
                api_key="test-key",
                max_chunk_characters=1000,
                chunk_overlap_lines=0,
            ).extract(transcript)
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(result["chunk_count"], 2)
        self.assertEqual(result["model_call_count"], 2)
        self.assertEqual(result["transport_attempt_count"], 2)
        self.assertEqual(result["summary"]["total"], 1)

    def test_successful_chunks_resume_from_hash_scoped_checkpoints(self) -> None:
        first_line = "甲(00:10:00): " + ("背景说明" * 180)
        second_line = (
            "主持人(00:32:42): "
            + ("补充上下文" * 150)
            + "这个就是需要有人去调研一下"
        )
        transcript = first_line + "\n" + second_line

        def response(items: list[dict]) -> ExtractionContractTests._Response:
            payload = {
                "model": "qwen-plus",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"action_items": items}, ensure_ascii=False
                            )
                        }
                    }
                ],
                "usage": {"total_tokens": 10},
            }
            return self._Response(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
            )

        with TemporaryDirectory() as temporary_directory:
            checkpoint_dir = Path(temporary_directory)
            extractor = BailianExtractor(
                api_key="test-key",
                max_chunk_characters=1000,
                chunk_overlap_lines=0,
            )
            with patch(
                "collab_agent.extraction.urllib.request.urlopen",
                side_effect=[response([]), response([valid_item()])],
            ):
                first = extractor.extract(
                    transcript, checkpoint_dir=checkpoint_dir
                )
            with patch(
                "collab_agent.extraction.urllib.request.urlopen",
                side_effect=AssertionError("checkpoint miss"),
            ) as urlopen:
                resumed = extractor.extract(
                    transcript, checkpoint_dir=checkpoint_dir
                )
        self.assertEqual(first["checkpoint_hit_count"], 0)
        self.assertEqual(resumed["checkpoint_hit_count"], 2)
        self.assertEqual(resumed["summary"]["total"], 1)
        self.assertEqual(urlopen.call_count, 0)
