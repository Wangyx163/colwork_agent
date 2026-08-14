from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from collab_agent import gold_harvest
from collab_agent.meeting import load_meeting_service
from collab_agent.store import Database


def extraction(quotes: list[str]) -> dict:
    return {
        "provider": "fixture",
        "model": "deterministic",
        "input_sha256": "c" * 63 + "3",
        "action_items": [
            {
                "title": f"任务：{quote}",
                "deliverable": "一页说明",
                "owner_name": None,
                "deadline_text": None,
                "deadline_iso": None,
                "source_timestamp": f"00:0{index + 1}:00",
                "source_quote": quote,
                "confidence": 0.9,
                "needs_confirmation": True,
                "uncertainties": [],
            }
            for index, quote in enumerate(quotes)
        ],
    }


class GoldHarvestTests(unittest.TestCase):
    """Item gold, produced by using the product rather than by annotating.

    `item_level_detection` has been null in every report ever run, because the
    public corpus labels sentences and nobody wrote item gold for it. So the
    project has been steering on a metric that measures whether the extractor
    pointed at the right lines, not whether it produced the right task list.

    A coordinator reviewing candidates is already labelling them, and the
    system already records every decision.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        extraction_path = self.root / "extraction.json"
        transcript = self.root / "transcript.txt"
        quotes = ["请整理周报", "预算表要核一遍", "顺便说一句天气不错"]
        extraction_path.write_text(
            json.dumps(extraction(quotes), ensure_ascii=False), encoding="utf-8"
        )
        transcript.write_text(
            "".join(
                f"主持人(00:0{index + 1}:00): {quote}\n"
                for index, quote in enumerate(quotes)
            ),
            encoding="utf-8",
        )
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.db.initialize()
        self.service = load_meeting_service(
            self.db,
            extraction_path=extraction_path,
            transcript_path=transcript,
            organization_name="收金标测试团队",
            coordinator_name="会议负责人",
            participant_names=["同事甲"],
        )
        self.coordinator = self.service.aggregator_actor_id
        self.items = list(self.service.action_items())

    def harvest(self) -> dict:
        return gold_harvest.harvest(
            self.db, episode_id=self.service.episode_id, run_id=self.service.run_id
        )

    # ---- the four decisions become four labels --------------------------

    def test_untouched_candidates_are_gold_positives(self) -> None:
        payload = self.harvest()

        self.assertEqual(payload["counts"]["kept"], 3)
        self.assertEqual(len(payload["expected_items"]), 3)

    def test_ignoring_a_candidate_records_a_false_positive(self) -> None:
        """Said by the person who would have had to do the work, which is a
        better judge than anybody reading the transcript afterwards."""

        self.service.ignore_action(
            self.items[2]["action_item_id"],
            actor_id=self.coordinator,
            reason="这只是随口一说",
            message_id="ignore-1",
        )

        payload = self.harvest()

        self.assertEqual(payload["counts"]["ignored"], 1)
        self.assertEqual(payload["counts"]["kept"], 2)
        self.assertEqual(len(payload["expected_items"]), 2)

    def test_a_rejected_quote_is_kept_rather_than_dropped(self) -> None:
        """An extractor that stops proposing this one has improved, and a gold
        file listing only the good ones cannot tell that from an extractor that
        found less of everything."""

        self.service.ignore_action(
            self.items[2]["action_item_id"],
            actor_id=self.coordinator,
            reason="这只是随口一说",
            message_id="ignore-1",
        )

        payload = self.harvest()

        self.assertEqual(payload["rejected_quotes"], ["顺便说一句天气不错"])

    def test_a_hand_added_task_records_a_false_negative(self) -> None:
        self.service.add_action_item(
            actor_id=self.coordinator,
            title="散会前口头提的那件事",
            deliverable="一页说明",
            source_note="逐字稿里没录到",
            message_id="add-1",
        )

        payload = self.harvest()

        self.assertEqual(payload["counts"]["added"], 1)
        added = [
            item for item in payload["expected_items"] if item["label"] == "ADDED"
        ]
        self.assertEqual(len(added), 1)
        self.assertFalse(added[0]["grounded"])

    # ---- what the numbers may and may not claim -------------------------

    def test_precision_counts_every_proposal(self) -> None:
        """Unbiased: every candidate was looked at, and the population is
        complete."""

        self.service.ignore_action(
            self.items[2]["action_item_id"],
            actor_id=self.coordinator,
            reason="随口一说",
            message_id="ignore-1",
        )

        numbers = gold_harvest.review_confusion(self.harvest())

        self.assertEqual(numbers["proposed"], 3)
        self.assertAlmostEqual(numbers["precision"], 2 / 3, places=3)

    def test_recall_is_returned_as_a_bound_and_says_so(self) -> None:
        """A task nobody remembered is absent from both the extraction and the
        gold, and no amount of usage surfaces it. Calling this "recall" would
        be the most flattering number in the file and the least true."""

        self.service.add_action_item(
            actor_id=self.coordinator,
            title="漏了的那件",
            deliverable="说明",
            source_note="口头提的",
            message_id="add-1",
        )

        numbers = gold_harvest.review_confusion(self.harvest())

        self.assertIn("recall_lower_bound", numbers)
        self.assertNotIn("recall", numbers)
        self.assertTrue(numbers["why_recall_is_a_bound"])

    def test_f1_is_labelled_as_an_upper_bound(self) -> None:
        numbers = gold_harvest.review_confusion(self.harvest())

        self.assertIn("f1_upper_bound", numbers)
        self.assertNotIn("f1", numbers)

    def test_pooling_several_meetings_sums_the_decisions(self) -> None:
        """One meeting is an anecdote."""

        pooled = gold_harvest.combine(
            [
                {"counts": {"kept": 10, "ignored": 0, "added": 1, "reworded": 0}},
                {"counts": {"kept": 2, "ignored": 2, "added": 0, "reworded": 0}},
            ]
        )

        self.assertEqual(pooled["proposed"], 14)
        self.assertEqual(pooled["meetings"], 2)
        self.assertAlmostEqual(pooled["precision"], 12 / 14, places=3)

    def test_the_file_carries_its_own_provenance(self) -> None:
        """Stated in a field rather than in a README nobody reads next to the
        number: these are one team's judgements about their own meetings."""

        payload = self.harvest()

        self.assertIn("复核决定", payload["provenance"])

    def test_it_writes_where_the_evaluation_can_read_it(self) -> None:
        destination = self.root / "gold" / "meeting.json"

        gold_harvest.write_gold(
            self.db,
            episode_id=self.service.episode_id,
            run_id=self.service.run_id,
            destination=destination,
        )

        self.assertTrue(destination.is_file())
        self.assertEqual(
            json.loads(destination.read_text(encoding="utf-8"))["schema_version"],
            "harvested-gold.v1",
        )


if __name__ == "__main__":
    unittest.main()
