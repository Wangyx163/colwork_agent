from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from collab_agent.annotation_check import check_annotation_file, check_case


def case(**overrides) -> dict:
    base = {
        "case_id": "c1",
        "meeting_date": "2026-03-09",
        "participants": ["王昱翔", "绒"],
        "blind": False,
        "transcript": (
            "王昱翔(00:00:00): 今天我们讨论街采的事。\n"
            "绒(00:01:12): 那我这边准备七八个采访问题发给子恒。\n"
            "王昱翔(00:02:00): 好。\n"
        ),
        "expected": [
            {
                "title": "准备街采候选问题",
                "item_type": "TASK",
                "deliverable": "7—8 个候选采访问题",
                "owner_name": "绒",
                "collaborator_names": [],
                "deadline_text": None,
                "deadline_iso": None,
                "source_timestamp": "00:01:12",
                "source_quote": "那我这边准备七八个采访问题发给子恒。",
            }
        ],
    }
    base.update(overrides)
    return base


def problems_of(target: dict, level: str = "ERROR") -> list[str]:
    return [p["message"] for p in check_case(target) if p["level"] == level]


class AnnotationCheckTests(unittest.TestCase):
    def test_a_correct_annotation_passes(self) -> None:
        self.assertEqual(problems_of(case()), [])

    def test_a_retyped_quote_is_caught(self) -> None:
        """The silent failure mode: a quote that was retyped instead of copied
        still reads fine to a human, but the scorer cannot locate it, so the
        item quietly counts as a miss for a reason unrelated to the extractor.
        """

        target = case()
        target["expected"][0]["source_quote"] = "我这边准备七八个采访问题发给子衡。"
        self.assertTrue(
            any("无法在逐字稿中定位" in message for message in problems_of(target))
        )

    def test_a_fabricated_timestamp_is_caught(self) -> None:
        target = case()
        target["expected"][0]["source_timestamp"] = "09:99:99"
        self.assertTrue(
            any("在逐字稿里不存在" in message for message in problems_of(target))
        )

    def test_an_owner_outside_the_roster_is_refused(self) -> None:
        """The roster is the permission boundary; the system never infers it."""

        target = case()
        target["expected"][0]["owner_name"] = "路人甲"
        self.assertTrue(
            any("不在 participants" in message for message in problems_of(target))
        )

    def test_an_unknown_item_type_is_refused(self) -> None:
        target = case()
        target["expected"][0]["item_type"] = "IDEA"
        self.assertTrue(
            any("item_type" in message for message in problems_of(target))
        )

    def test_two_items_landing_on_one_sentence_warn(self) -> None:
        """A sentence can genuinely carry two actions, so this is a warning and
        not an error -- but it is also exactly what a duplicated paste looks
        like, and only the annotator can tell the two apart."""

        target = case()
        target["expected"].append(dict(target["expected"][0], title="重复条目"))
        self.assertTrue(
            any(
                "引用同一句" in message
                for message in problems_of(target, level="WARNING")
            )
        )

    def test_a_too_short_quote_is_refused(self) -> None:
        target = case()
        target["expected"][0]["source_quote"] = "好。"
        self.assertTrue(
            any("无法可靠定位" in message for message in problems_of(target))
        )

    def test_a_meeting_with_no_action_items_warns_but_stays_valid(self) -> None:
        """A meeting genuinely containing nothing is a valid negative sample --
        the corpus has 10 such meetings in dev -- so it must not be an error."""

        target = case(expected=[])
        self.assertEqual(problems_of(target), [])
        self.assertTrue(
            any(
                "负样本" in message
                for message in problems_of(target, level="WARNING")
            )
        )

    def test_a_missing_roster_is_an_error(self) -> None:
        target = case(participants=[])
        self.assertTrue(
            any("参会名单" in message for message in problems_of(target))
        )

    def test_the_summary_reports_the_positive_rate(self) -> None:
        """Real corpora sit near 0.5%; a rate near 1.0 means the file is a toy
        and cannot support any claim about extraction quality."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gold.json"
            path.write_text(
                json.dumps({"cases": [case()]}, ensure_ascii=False), encoding="utf-8"
            )
            report = check_annotation_file(path)
        self.assertTrue(report["valid"])
        self.assertEqual(report["summary"]["annotated_items"], 1)
        self.assertEqual(report["summary"]["sentences"], 3)
        self.assertAlmostEqual(report["summary"]["positive_sentence_rate"], 0.3333, 3)

    def test_blind_cases_are_counted_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gold.json"
            path.write_text(
                json.dumps(
                    {"cases": [case(), case(case_id="c2", blind=True)]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            report = check_annotation_file(path)
        self.assertEqual(report["summary"]["cases"], 2)
        self.assertEqual(report["summary"]["blind_cases"], 1)


if __name__ == "__main__":
    unittest.main()


class CollaboratorFieldTests(unittest.TestCase):
    """The extractor has always been able to say «甲 and 乙 together», so a gold
    schema without that field forced annotators to write null for every jointly
    assigned task -- which would have scored a correct extraction as an error.
    """

    def test_a_named_collaborator_passes(self) -> None:
        target = case()
        target["expected"][0]["owner_name"] = "绒"
        target["expected"][0]["collaborator_names"] = ["王昱翔"]
        self.assertEqual(problems_of(target), [])

    def test_a_collaborator_outside_the_roster_is_refused(self) -> None:
        target = case()
        target["expected"][0]["collaborator_names"] = ["路人甲"]
        self.assertTrue(
            any("不在 participants" in message for message in problems_of(target))
        )

    def test_one_person_cannot_be_owner_and_collaborator(self) -> None:
        target = case()
        target["expected"][0]["owner_name"] = "绒"
        target["expected"][0]["collaborator_names"] = ["绒"]
        self.assertTrue(
            any("同时是 owner 和协作者" in message for message in problems_of(target))
        )

    def test_collaborators_without_an_owner_warn(self) -> None:
        target = case()
        target["expected"][0]["owner_name"] = None
        target["expected"][0]["collaborator_names"] = ["王昱翔"]
        self.assertTrue(
            any(
                "有协作者但没有 owner" in message
                for message in problems_of(target, level="WARNING")
            )
        )


class NullValueTests(unittest.TestCase):
    def test_a_present_but_null_field_is_refused(self) -> None:
        """Present-but-null is as unusable as absent, and it is the easier
        mistake to make when a template is filled in by hand. meeting_date in
        particular is what resolves «明天» into an absolute date."""

        target = case(meeting_date=None)
        self.assertTrue(
            any("meeting_date 为空" in message for message in problems_of(target))
        )
