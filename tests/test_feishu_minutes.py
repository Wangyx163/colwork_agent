from __future__ import annotations

import unittest

from collab_agent.feishu_minutes import (
    MinutesError,
    RecordingMinutesTransport,
    intake,
    parse_plain,
    parse_srt,
    parse_transcript,
    propose_roster,
    speakers_in,
    to_project_transcript,
)
from collab_agent.extraction import TRANSCRIPT_LINE_PATTERN


SRT = """1
00:00:02,000 --> 00:00:05,400
黄Z恒: 可以继续复盘的？

2
00:00:06,120 --> 00:00:12,000
王昱翔: 刚刚因为刚刚会议那个卡了

3
00:01:09,000 --> 00:01:20,000
王昱翔: 热点预测方法其实跟热点风格
"""

PLAIN = """黄Z恒 00:00:02  可以继续复盘的？
王昱翔 00:00:06  刚刚因为刚刚会议那个卡了
[00:01:09] 王昱翔: 热点预测方法其实跟热点风格
"""

MEMBERS = [
    {"open_id": "ou_1", "name": "王昱翔"},
    {"open_id": "ou_2", "name": "黄Z恒"},
    {"open_id": "ou_3", "name": "从未发言的人"},
]


class SrtTests(unittest.TestCase):
    def test_speaker_and_timestamp_are_recovered(self) -> None:
        lines = parse_srt(SRT)

        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0].speaker, "黄Z恒")
        self.assertEqual(lines[0].timestamp, "00:00:02")
        self.assertEqual(lines[0].text, "可以继续复盘的？")

    def test_the_start_time_is_used_not_the_end(self) -> None:
        self.assertEqual(parse_srt(SRT)[2].timestamp, "00:01:09")

    def test_a_line_without_a_speaker_prefix_still_parses(self) -> None:
        lines = parse_srt("1\n00:00:01,000 --> 00:00:02,000\n没有前缀的一句\n")

        self.assertEqual(lines[0].text, "没有前缀的一句")
        self.assertEqual(lines[0].speaker, "发言人")


class PlainTests(unittest.TestCase):
    def test_speaker_before_the_timestamp(self) -> None:
        lines = parse_plain(PLAIN)

        self.assertEqual(lines[0].speaker, "黄Z恒")
        self.assertEqual(lines[0].timestamp, "00:00:02")

    def test_speaker_after_a_bracketed_timestamp(self) -> None:
        lines = parse_plain(PLAIN)

        self.assertEqual(lines[2].speaker, "王昱翔")
        self.assertEqual(lines[2].timestamp, "00:01:09")


class FormatDetectionTests(unittest.TestCase):
    def test_either_shape_is_accepted(self) -> None:
        self.assertEqual(len(parse_transcript(SRT)), 3)
        self.assertEqual(len(parse_transcript(PLAIN)), 3)

    def test_an_unknown_shape_fails_with_the_evidence_to_fix_it(self) -> None:
        """A silent empty transcript would be far worse than a loud failure."""

        with self.assertRaises(MinutesError) as caught:
            parse_transcript("<html><body>登录后查看</body></html>")

        message = str(caught.exception)
        self.assertIn("登录后查看", message, "the raw body must be quoted back")

    def test_an_empty_export_is_refused_rather_than_treated_as_no_content(self) -> None:
        with self.assertRaises(MinutesError):
            parse_transcript("")


class ProjectFormatTests(unittest.TestCase):
    def test_output_matches_what_the_extractor_parses(self) -> None:
        """The whole point is to feed the existing pipeline unchanged."""

        rendered = to_project_transcript(parse_transcript(SRT))

        for line in rendered.splitlines():
            self.assertIsNotNone(
                TRANSCRIPT_LINE_PATTERN.match(line),
                f"extractor cannot parse {line!r}",
            )

    def test_speakers_are_ordered_by_how_much_they_said(self) -> None:
        self.assertEqual(speakers_in(parse_transcript(SRT)), ["王昱翔", "黄Z恒"])


class RosterTests(unittest.TestCase):
    """Neither the chat nor the transcript is the roster on its own."""

    def test_someone_who_spoke_and_is_in_the_chat_carries_their_open_id(self) -> None:
        roster = propose_roster(parse_transcript(SRT), MEMBERS)

        matched = {row["name"]: row["open_id"] for row in roster["spoke_and_in_chat"]}
        self.assertEqual(matched, {"王昱翔": "ou_1", "黄Z恒": "ou_2"})

    def test_a_silent_chat_member_is_reported_separately_not_dropped(self) -> None:
        roster = propose_roster(parse_transcript(SRT), MEMBERS)

        self.assertEqual(roster["in_chat_but_silent"], ["从未发言的人"])

    def test_a_speaker_missing_from_the_chat_is_flagged(self) -> None:
        roster = propose_roster(parse_transcript(SRT), [MEMBERS[0]])

        self.assertEqual(roster["spoke_but_not_in_chat"], ["黄Z恒"])

    def test_no_chat_still_yields_a_transcript(self) -> None:
        transport = RecordingMinutesTransport(transcript=SRT)

        result = intake(transport, minute_token="mt_1")

        self.assertEqual(result["line_count"], 3)
        self.assertEqual(result["roster"]["spoke_and_in_chat"], [])


class IntakeTests(unittest.TestCase):
    def test_intake_returns_a_loadable_transcript_and_a_roster_proposal(self) -> None:
        transport = RecordingMinutesTransport(transcript=SRT, members=MEMBERS)

        result = intake(transport, minute_token="mt_1", chat_id="oc_1")

        self.assertEqual(result["minute_token"], "mt_1")
        self.assertIn("王昱翔(00:00:06): ", result["transcript"])
        self.assertEqual(len(result["roster"]["spoke_and_in_chat"]), 2)

    def test_both_endpoints_are_called_once(self) -> None:
        transport = RecordingMinutesTransport(transcript=SRT, members=MEMBERS)

        intake(transport, minute_token="mt_1", chat_id="oc_1")

        self.assertEqual(
            [call["kind"] for call in transport.calls], ["transcript", "members"]
        )

    def test_a_chat_is_not_fetched_when_none_is_given(self) -> None:
        transport = RecordingMinutesTransport(transcript=SRT, members=MEMBERS)

        intake(transport, minute_token="mt_1")

        self.assertEqual([call["kind"] for call in transport.calls], ["transcript"])


if __name__ == "__main__":
    unittest.main()
