from __future__ import annotations

import unittest

from collab_agent.extraction import (
    ExtractedActionItem,
    validate_source_evidence,
)
from collab_agent.extraction_tools import (
    TOOL_SCHEMAS,
    TranscriptTools,
    parse_transcript,
)


TRANSCRIPT = """王昱翔(00:01:00): 每个人会后准备七八个采访问题发给子恒
宋潽暄(00:01:30): 行
王昱翔(00:02:00): 子恒整理出来之后我们每个人对问题打分投票
黄Z恒(00:02:30): 好我来整理
Jasmine(00:03:00): 那我负责联系场地
"""


class ParseTests(unittest.TestCase):
    def test_lines_are_numbered_from_one(self) -> None:
        lines = parse_transcript(TRANSCRIPT)

        self.assertEqual([line.index for line in lines], [1, 2, 3, 4, 5])
        self.assertEqual(lines[0].speaker, "王昱翔")
        self.assertEqual(lines[0].timestamp, "00:01:00")

    def test_unparseable_lines_are_dropped_not_guessed(self) -> None:
        lines = parse_transcript("没有时间戳的一行\n王昱翔(00:01:00): 有的\n")

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].text, "有的")

    def test_whitespace_is_collapsed_like_the_validator(self) -> None:
        lines = parse_transcript("王昱翔(00:01:00):   有   很多    空格  \n")

        self.assertEqual(lines[0].text, "有 很多 空格")


class SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = TranscriptTools(TRANSCRIPT)

    def test_exact_match_wins_and_excludes_fuzzy_noise(self) -> None:
        result = self.tools.search_transcript("采访问题")

        self.assertEqual(result["match_type"], "exact")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["timestamp"], "00:01:00")
        self.assertNotIn("similarity", result["results"][0])

    def test_fuzzy_fallback_reports_its_score(self) -> None:
        result = self.tools.search_transcript("每个人会后准备八个采访的问题发给子恒")

        self.assertEqual(result["match_type"], "fuzzy")
        self.assertTrue(result["results"])
        self.assertIn("similarity", result["results"][0])

    def test_no_match_returns_empty_rather_than_a_wrong_line(self) -> None:
        result = self.tools.search_transcript("完全不相干的内容比如量子计算机部署")

        self.assertEqual(result["results"], [])
        self.assertEqual(result["match_type"], "none")

    def test_limit_is_clamped(self) -> None:
        result = self.tools.search_transcript("我", limit=999)

        self.assertLessEqual(len(result["results"]), 8)


class ContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = TranscriptTools(TRANSCRIPT)

    def test_context_window_marks_the_anchor(self) -> None:
        result = self.tools.get_context("00:01:30", before=1, after=1)

        self.assertTrue(result["found"])
        anchors = [row for row in result["results"] if row["is_anchor"]]
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["speaker"], "宋潽暄")
        self.assertEqual(len(result["results"]), 3)

    def test_missing_timestamp_says_so_and_suggests_search(self) -> None:
        result = self.tools.get_context("09:99:99")

        self.assertFalse(result["found"])
        self.assertEqual(result["results"], [])
        self.assertIn("search_transcript", result["note"])


class SpeakerTests(unittest.TestCase):
    def test_speakers_come_from_the_transcript_only(self) -> None:
        result = TranscriptTools(TRANSCRIPT).list_speakers()

        names = [row["name"] for row in result["speakers"]]
        self.assertEqual(names[0], "王昱翔", "most talkative first")
        self.assertCountEqual(names, ["王昱翔", "宋潽暄", "黄Z恒", "Jasmine"])


class DispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = TranscriptTools(TRANSCRIPT)

    def test_unknown_tool_returns_an_error_instead_of_raising(self) -> None:
        result = self.tools.call("drop_database", {})

        self.assertIn("error", result)
        self.assertEqual(self.tools.call_log[-1]["ok"], False)

    def test_bad_arguments_cost_a_round_trip_not_the_extraction(self) -> None:
        result = self.tools.call("get_context", {"timestamp": "00:01:00", "before": "x"})

        self.assertIn("error", result)

    def test_every_call_is_logged_for_the_run_record(self) -> None:
        self.tools.call("list_speakers", {})
        self.tools.call("search_transcript", {"query": "采访问题"})

        self.assertEqual([entry["tool"] for entry in self.tools.call_log],
                         ["list_speakers", "search_transcript"])
        self.assertTrue(all(entry["ok"] for entry in self.tools.call_log))


class GroundingContractTests(unittest.TestCase):
    """The whole point: a copied tool result must survive validation."""

    def test_a_quote_copied_from_search_passes_evidence_validation(self) -> None:
        tools = TranscriptTools(TRANSCRIPT)
        hit = tools.search_transcript("采访问题")["results"][0]

        item = ExtractedActionItem(
            title="准备采访问题",
            item_type="TASK",
            deliverable="采访问题清单",
            owner_name=None,
            deadline_text=None,
            deadline_iso=None,
            source_timestamp=hit["timestamp"],
            source_quote=hit["text"],
            confidence=0.9,
            needs_confirmation=True,
            uncertainties=[],
            collaborator_names=[],
        )

        # Raises if the citation does not hold; that it does not raise is the
        # assertion.
        validate_source_evidence([item], TRANSCRIPT)

    def test_a_recalled_quote_still_fails_validation(self) -> None:
        """Guards the premise: validation is strict, so tools are not theatre."""

        item = ExtractedActionItem(
            title="准备采访问题",
            item_type="TASK",
            deliverable="采访问题清单",
            owner_name=None,
            deadline_text=None,
            deadline_iso=None,
            source_timestamp="00:01:00",
            source_quote="每个人会后准备8个采访问题发给子恒",  # 七八 -> 8
            confidence=0.9,
            needs_confirmation=True,
            uncertainties=[],
            collaborator_names=[],
        )

        with self.assertRaises(Exception):
            validate_source_evidence([item], TRANSCRIPT)


class SchemaTests(unittest.TestCase):
    def test_every_schema_matches_a_real_handler(self) -> None:
        tools = TranscriptTools(TRANSCRIPT)

        for schema in TOOL_SCHEMAS:
            name = schema["function"]["name"]
            result = tools.call(name, {"query": "x", "timestamp": "00:01:00"})
            self.assertNotIn(
                "error", result, f"schema {name} has no working handler"
            )

    def test_schemas_are_openai_tool_shaped(self) -> None:
        for schema in TOOL_SCHEMAS:
            self.assertEqual(schema["type"], "function")
            self.assertIn("name", schema["function"])
            self.assertIn("description", schema["function"])
            self.assertEqual(schema["function"]["parameters"]["type"], "object")


if __name__ == "__main__":
    unittest.main()
