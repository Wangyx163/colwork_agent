from __future__ import annotations

import json
import unittest

from collab_agent.extraction import (
    ACTION_ITEM_EXTRACTION_PROMPT_VERSION,
    ACTION_ITEM_EXTRACTION_TOOLS_PROMPT_VERSION,
    BailianExtractor,
    ExtractionError,
    _system_prompt,
    _tools_system_prompt,
)


TRANSCRIPT = """王昱翔(00:01:00): 每个人会后准备七八个采访问题发给子恒
宋潽暄(00:01:30): 行
黄Z恒(00:02:30): 好我来整理
"""

QUOTE = "每个人会后准备七八个采访问题发给子恒"

FINAL_ANSWER = {
    "action_items": [
        {
            "title": "准备采访问题",
            "item_type": "TASK",
            "deliverable": "七到八个采访问题",
            "owner_name": None,
            "deadline_text": None,
            "deadline_iso": None,
            "source_timestamp": "00:01:00",
            "source_quote": QUOTE,
            "confidence": 0.9,
            "needs_confirmation": True,
            "uncertainties": [],
            "collaborator_names": [],
        }
    ]
}


def _assistant(content: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _tool_call(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
    }


class ScriptedExtractor(BailianExtractor):
    """A BailianExtractor whose HTTP layer replays a scripted conversation."""

    def __init__(self, script: list[dict], **kwargs) -> None:
        kwargs.setdefault("api_key", "test-key")
        super().__init__(**kwargs)
        self.script = list(script)
        self.requests: list[dict] = []

    def _request_once(self, messages, *, tools=None):
        self.requests.append({"messages": list(messages), "tools": tools})
        if not self.script:
            raise AssertionError("scripted extractor ran out of responses")
        return dict(self.script.pop(0))


class ToolLoopTests(unittest.TestCase):
    def _extract(self, script: list[dict], **kwargs) -> tuple[dict, ScriptedExtractor]:
        extractor = ScriptedExtractor(script, use_tools=True, **kwargs)
        result = extractor._extract_chunk(
            TRANSCRIPT,
            full_transcript=TRANSCRIPT,
            date_context="2026-03-09",
            chunk_index=1,
            chunk_count=1,
        )
        return result, extractor

    def test_a_tool_call_is_executed_and_its_result_fed_back(self) -> None:
        result, extractor = self._extract(
            [
                _tool_call("search_transcript", {"query": "采访问题"}),
                _assistant(json.dumps(FINAL_ANSWER, ensure_ascii=False)),
            ]
        )

        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["tool_summary"]["tool_rounds"], 2)
        self.assertEqual(
            [call["tool"] for call in result["tool_summary"]["tool_calls"]],
            ["search_transcript"],
        )

        # The second request must carry the assistant turn and the tool result.
        second = extractor.requests[1]["messages"]
        roles = [message.get("role") for message in second]
        self.assertEqual(roles[-2:], ["assistant", "tool"])
        tool_payload = json.loads(second[-1]["content"])
        self.assertEqual(tool_payload["match_type"], "exact")
        self.assertEqual(tool_payload["results"][0]["text"], QUOTE)

    def test_tools_are_offered_only_while_tools_are_enabled(self) -> None:
        _, with_tools = self._extract(
            [_assistant(json.dumps(FINAL_ANSWER, ensure_ascii=False))]
        )
        self.assertIsNotNone(with_tools.requests[0]["tools"])

        without = ScriptedExtractor(
            [_assistant(json.dumps(FINAL_ANSWER, ensure_ascii=False))],
            use_tools=False,
        )
        without._extract_chunk(
            TRANSCRIPT,
            full_transcript=TRANSCRIPT,
            date_context="2026-03-09",
            chunk_index=1,
            chunk_count=1,
        )
        self.assertIsNone(without.requests[0]["tools"])

    def test_json_format_is_not_forced_while_tools_are_offered(self) -> None:
        """Forcing json_object alongside tools makes providers drop the call."""

        extractor = BailianExtractor(api_key="k", use_tools=True)
        messages = [{"role": "user", "content": "x"}]

        with_tools = extractor._request_body(messages, tools=[{"a": 1}])
        without_tools = extractor._request_body(messages)

        self.assertNotIn("response_format", with_tools)
        self.assertEqual(with_tools["tool_choice"], "auto")
        self.assertEqual(
            without_tools["response_format"],
            {"type": "json_object"},
            "the no-tools path must keep its stricter guarantee",
        )

    def test_a_fenced_final_answer_is_still_parsed(self) -> None:
        fenced = "```json\n" + json.dumps(FINAL_ANSWER, ensure_ascii=False) + "\n```"

        result, _ = self._extract([_assistant(fenced)])

        self.assertEqual(len(result["items"]), 1)

    def test_malformed_tool_arguments_cost_a_round_not_the_extraction(self) -> None:
        broken = _tool_call("search_transcript", {})
        broken["choices"][0]["message"]["tool_calls"][0]["function"][
            "arguments"
        ] = "{not json"

        result, _ = self._extract(
            [broken, _assistant(json.dumps(FINAL_ANSWER, ensure_ascii=False))]
        )

        self.assertEqual(len(result["items"]), 1)

    def test_an_unknown_tool_is_reported_back_rather_than_fatal(self) -> None:
        result, extractor = self._extract(
            [
                _tool_call("delete_everything", {}),
                _assistant(json.dumps(FINAL_ANSWER, ensure_ascii=False)),
            ]
        )

        self.assertEqual(len(result["items"]), 1)
        tool_message = json.loads(extractor.requests[1]["messages"][-1]["content"])
        self.assertIn("error", tool_message)
        self.assertFalse(result["tool_summary"]["tool_calls"][0]["ok"])

    def test_round_limit_forces_a_final_answer_instead_of_looping(self) -> None:
        script = [_tool_call("list_speakers", {}) for _ in range(3)]
        script.append(_assistant(json.dumps(FINAL_ANSWER, ensure_ascii=False)))

        result, extractor = self._extract(script, max_tool_rounds=3)

        self.assertTrue(result["tool_summary"]["hit_round_limit"])
        self.assertEqual(len(result["items"]), 1)
        # The forcing turn must offer no tools, or the model can keep calling.
        self.assertIsNone(extractor.requests[-1]["tools"])

    def test_token_usage_from_every_round_is_kept(self) -> None:
        result, _ = self._extract(
            [
                _tool_call("search_transcript", {"query": "采访问题"}),
                _assistant(json.dumps(FINAL_ANSWER, ensure_ascii=False)),
            ]
        )

        self.assertEqual(
            len(result["response_payloads"]),
            2,
            "a tool round costs tokens and must not vanish from accounting",
        )

    def test_evidence_validation_still_applies_to_a_tool_run(self) -> None:
        """Tools assist the model; they do not exempt it from validation."""

        invented = json.loads(json.dumps(FINAL_ANSWER))
        invented["action_items"][0]["source_quote"] = "这句话逐字稿里根本没有"

        with self.assertRaises(ExtractionError):
            self._extract(
                [
                    _assistant(json.dumps(invented, ensure_ascii=False)),
                    # The repair round also fails to produce valid evidence.
                    _assistant(json.dumps(invented, ensure_ascii=False)),
                ]
            )


class EndToEndToolRunTests(unittest.TestCase):
    """The whole `extract()` path, still without a network call."""

    def test_tool_use_is_recorded_in_the_run_output(self) -> None:
        extractor = ScriptedExtractor(
            [
                _tool_call("search_transcript", {"query": "采访问题"}),
                _tool_call("list_speakers", {}, call_id="call_2"),
                _assistant(json.dumps(FINAL_ANSWER, ensure_ascii=False)),
            ],
            use_tools=True,
        )

        result = extractor.extract(TRANSCRIPT, meeting_date="2026-03-09")

        self.assertEqual(result["prompt_version"], ACTION_ITEM_EXTRACTION_TOOLS_PROMPT_VERSION)
        tool_use = result["tool_use"]
        self.assertEqual(tool_use["total_calls"], 2)
        self.assertEqual(tool_use["failed_calls"], 0)
        self.assertEqual(tool_use["chunks_hitting_round_limit"], 0)
        self.assertEqual(
            [call["tool"] for call in tool_use["per_chunk"][0]["tool_calls"]],
            ["search_transcript", "list_speakers"],
        )

    def test_a_plain_run_carries_no_tool_record(self) -> None:
        extractor = ScriptedExtractor(
            [_assistant(json.dumps(FINAL_ANSWER, ensure_ascii=False))],
            use_tools=False,
        )

        result = extractor.extract(TRANSCRIPT, meeting_date="2026-03-09")

        self.assertIsNone(result["tool_use"])
        self.assertEqual(
            result["prompt_version"], ACTION_ITEM_EXTRACTION_PROMPT_VERSION
        )

    def test_the_two_modes_do_not_share_a_checkpoint(self) -> None:
        """A cached one-shot result must not be replayed as a tools result."""

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            checkpoints = Path(directory)
            plain = ScriptedExtractor(
                [_assistant(json.dumps(FINAL_ANSWER, ensure_ascii=False))],
                use_tools=False,
            )
            plain.extract(
                TRANSCRIPT, meeting_date="2026-03-09", checkpoint_dir=checkpoints
            )

            tooled = ScriptedExtractor(
                [_assistant(json.dumps(FINAL_ANSWER, ensure_ascii=False))],
                use_tools=True,
            )
            tooled.extract(
                TRANSCRIPT, meeting_date="2026-03-09", checkpoint_dir=checkpoints
            )

            # If the checkpoint had been shared, the tools run would have
            # replayed the cached one and issued no request at all.
            self.assertEqual(len(tooled.requests), 1)
            self.assertEqual(len(list(checkpoints.glob("*.json"))), 2)


class PromptVersionTests(unittest.TestCase):
    def test_the_two_prompts_are_separately_versioned(self) -> None:
        self.assertNotEqual(
            ACTION_ITEM_EXTRACTION_PROMPT_VERSION,
            ACTION_ITEM_EXTRACTION_TOOLS_PROMPT_VERSION,
        )

    def test_the_reported_version_follows_the_mode(self) -> None:
        plain = BailianExtractor(api_key="k", use_tools=False)
        tooled = BailianExtractor(api_key="k", use_tools=True)

        self.assertEqual(
            plain.prompt_version, ACTION_ITEM_EXTRACTION_PROMPT_VERSION
        )
        self.assertEqual(
            tooled.prompt_version, ACTION_ITEM_EXTRACTION_TOOLS_PROMPT_VERSION
        )

    def test_the_tools_prompt_reuses_every_semantic_rule(self) -> None:
        """A score difference must be attributable to tools, not new wording."""

        self.assertIn(_system_prompt(), _tools_system_prompt())

    def test_the_tools_prompt_forbids_settling_for_a_near_quote(self) -> None:
        prompt = _tools_system_prompt()

        self.assertIn("search_transcript", prompt)
        self.assertIn("list_speakers", prompt)
        self.assertIn("放弃该候选", prompt)


if __name__ == "__main__":
    unittest.main()
