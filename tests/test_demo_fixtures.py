from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from collab_agent.demo_fixtures import (
    GOLD_DERIVED_PROVIDER,
    gold_case_to_extraction,
    gold_to_extraction,
)
from collab_agent.extraction import validate_extraction


TRANSCRIPT = "王昱翔(00:01:00): 你们俩先列一个调研大纲。\n"

GOLD = {
    "schema_version": "annotation.v1",
    "cases": [
        {
            "case_id": "demo-case",
            "meeting_date": "2026-03-02",
            "participants": ["王昱翔", "黄Z恒", "宋潽暄"],
            "blind": False,
            "transcript": TRANSCRIPT,
            "expected": [
                {
                    "title": "列调研大纲",
                    "item_type": "TASK",
                    "deliverable": "调研大纲",
                    "owner_name": "黄Z恒",
                    "collaborator_names": ["宋潽暄"],
                    "deadline_text": None,
                    "deadline_iso": None,
                    "source_timestamp": "00:01:00",
                    "source_quote": "你们俩先列一个调研大纲。",
                }
            ],
        }
    ],
}


class GoldToExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.gold = self.root / "gold.json"
        self.gold.write_text(
            json.dumps(GOLD, ensure_ascii=False), encoding="utf-8"
        )

    def test_output_passes_the_extraction_contract(self) -> None:
        """A demo file that the loader rejects is worse than no demo file."""

        extraction = gold_case_to_extraction(GOLD["cases"][0])

        items = validate_extraction(extraction)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].owner_name, "黄Z恒")
        self.assertEqual(items[0].collaborator_names, ["宋潽暄"])

    def test_the_output_never_claims_to_be_model_output(self) -> None:
        extraction = gold_case_to_extraction(GOLD["cases"][0])

        self.assertEqual(extraction["provider"], GOLD_DERIVED_PROVIDER)
        self.assertTrue(extraction["source"]["derived_from_annotation"])

    def test_a_human_checked_item_still_needs_confirmation(self) -> None:
        """Confirmation is a workflow gate, not a statement about accuracy."""

        extraction = gold_case_to_extraction(GOLD["cases"][0])

        self.assertTrue(extraction["action_items"][0]["needs_confirmation"])

    def test_conversion_refuses_an_invalid_annotation(self) -> None:
        broken = json.loads(json.dumps(GOLD))
        broken["cases"][0]["expected"][0]["source_quote"] = "逐字稿里没有这句话"
        self.gold.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")

        with self.assertRaises(ValueError) as caught:
            gold_to_extraction(self.gold, self.root / "out.json")

        self.assertIn("标注校验", str(caught.exception))
        self.assertFalse((self.root / "out.json").exists())

    def test_conversion_writes_a_usable_file(self) -> None:
        result = gold_to_extraction(self.gold, self.root / "out.json")

        self.assertEqual(result["action_items"], 1)
        written = json.loads((self.root / "out.json").read_text(encoding="utf-8"))
        self.assertEqual(written["source"]["participants"], GOLD["cases"][0]["participants"])

    def test_several_cases_require_choosing_one(self) -> None:
        two = json.loads(json.dumps(GOLD))
        second = json.loads(json.dumps(GOLD["cases"][0]))
        second["case_id"] = "demo-case-2"
        two["cases"].append(second)
        self.gold.write_text(json.dumps(two, ensure_ascii=False), encoding="utf-8")

        with self.assertRaises(ValueError):
            gold_to_extraction(self.gold, self.root / "out.json")

        result = gold_to_extraction(
            self.gold, self.root / "out.json", case_id="demo-case-2"
        )
        self.assertEqual(result["case_id"], "demo-case-2")


if __name__ == "__main__":
    unittest.main()
