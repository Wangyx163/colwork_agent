from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from collab_agent.feishu_minutes import minute_token_from
from collab_agent.intake_cache import CacheMiss, IntakeCache, resolve
from collab_agent.models import stable_hash


class MinuteLinkTests(unittest.TestCase):
    """People paste a link with Feishu's own tracking query on the end."""

    def test_a_pasted_link_yields_its_token(self) -> None:
        self.assertEqual(
            minute_token_from(
                "https://x.feishu.cn/minutes/obcnabc123def456?from=from_copylink"
            ),
            "obcnabc123def456",
        )

    def test_a_link_inside_a_message_is_found(self) -> None:
        self.assertEqual(
            minute_token_from("@机器人 https://a.feishu.cn/minutes/obcn9z8y7x6w 开个会"),
            "obcn9z8y7x6w",
        )

    def test_a_bare_token_still_works(self) -> None:
        """The CLI has always taken one, and the chat path disagreeing about
        what a 妙记 is would be a difference nobody could explain."""

        self.assertEqual(minute_token_from("obcnabc123def456"), "obcnabc123def456")

    def test_a_message_with_no_link_yields_nothing(self) -> None:
        self.assertEqual(minute_token_from("今天开会了"), "")


class IntakeCacheTests(unittest.TestCase):
    """Extraction is the one step that costs money and tens of seconds, and it
    is a pure function of the transcript. Paying for it twice is the failure
    this exists to prevent; silently paying for it during a demo is worse."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.cache = IntakeCache(
            extractions_dir=root / "extractions",
            transcripts_dir=root / "transcripts",
        )
        (root / "extractions").mkdir()
        self.transcript = "主持人(00:01:00): 请把周报整理出来\n"

    def write_extraction(self, name: str, transcript: str, marker: str) -> Path:
        path = self.cache.extractions_dir / name
        path.write_text(
            json.dumps(
                {"input_sha256": stable_hash(transcript), "marker": marker},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def test_an_extraction_written_before_any_of_this_counts_as_a_hit(self) -> None:
        """The key is a hash the extractions already carry, so there is no
        migration: every file already in var/extractions is already an entry."""

        self.write_extraction("old-file.json", self.transcript, "old")

        hit = self.cache.find(self.transcript)

        assert hit is not None
        self.assertEqual(hit.extraction_path.name, "old-file.json")

    def test_a_different_transcript_is_a_miss(self) -> None:
        self.write_extraction("old-file.json", self.transcript, "old")

        self.assertIsNone(self.cache.find("完全不同的逐字稿\n"))

    def test_the_newest_extraction_wins_a_duplicate(self) -> None:
        """Re-extracting is how a better prompt reaches an old transcript, and
        the reviewed output is the one that should be found."""

        old = self.write_extraction("a-old.json", self.transcript, "old")
        import os
        import time

        time.sleep(0.01)
        new = self.write_extraction("b-new.json", self.transcript, "new")
        os.utime(old, (1, 1))

        hit = self.cache.find(self.transcript)

        assert hit is not None
        self.assertEqual(hit.extraction_path, new)

    def test_cache_mode_refuses_a_miss_rather_than_calling_a_model(self) -> None:
        """The demo path is required to be deterministic, and "it usually hits
        the cache" is not determinism."""

        called: list[str] = []

        with self.assertRaises(CacheMiss):
            resolve(
                self.cache,
                self.transcript,
                mode="cache",
                extract=lambda *args: called.append("called"),
            )

        self.assertEqual(called, [])

    def test_live_mode_extracts_on_a_miss(self) -> None:
        def extract(transcript_path: Path, destination: Path) -> None:
            destination.write_text(
                json.dumps({"input_sha256": stable_hash(self.transcript)}),
                encoding="utf-8",
            )

        entry = resolve(self.cache, self.transcript, mode="live", extract=extract)

        self.assertTrue(entry.extraction_path.is_file())

    def test_live_mode_does_not_extract_when_it_can_hit(self) -> None:
        self.write_extraction("old-file.json", self.transcript, "old")
        called: list[str] = []

        entry = resolve(
            self.cache,
            self.transcript,
            mode="live",
            extract=lambda *args: called.append("called"),
        )

        self.assertEqual(called, [])
        self.assertEqual(entry.extraction_path.name, "old-file.json")

    def test_the_transcript_is_written_even_on_a_hit(self) -> None:
        """The episode is rebuilt from the transcript at every startup, so one
        that only ever lived in memory would make the meeting unserveable the
        moment the process restarted."""

        self.write_extraction("old-file.json", self.transcript, "old")

        entry = resolve(self.cache, self.transcript, mode="cache")

        self.assertTrue(entry.transcript_path.is_file())
        self.assertEqual(
            entry.transcript_path.read_text(encoding="utf-8"), self.transcript
        )

    def test_the_same_transcript_writes_one_file_not_two(self) -> None:
        self.write_extraction("old-file.json", self.transcript, "old")

        resolve(self.cache, self.transcript, mode="cache")
        resolve(self.cache, self.transcript, mode="cache")

        self.assertEqual(len(list(self.cache.transcripts_dir.glob("*.txt"))), 1)

    def test_an_unreadable_extraction_is_skipped_not_fatal(self) -> None:
        """One corrupt file in the directory must not take the cache down."""

        (self.cache.extractions_dir / "broken.json").write_text(
            "{ not json", encoding="utf-8"
        )
        self.write_extraction("good.json", self.transcript, "good")

        hit = self.cache.find(self.transcript)

        assert hit is not None
        self.assertEqual(hit.extraction_path.name, "good.json")

    def test_an_unknown_mode_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            resolve(self.cache, self.transcript, mode="whatever")


if __name__ == "__main__":
    unittest.main()
