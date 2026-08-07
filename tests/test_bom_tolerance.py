from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from collab_agent.feishu_config import load_feishu_config
from collab_agent.meeting import load_meeting_service
from collab_agent.models import read_text_file
from collab_agent.store import Database


BOM = "﻿"

QUOTE = "王昱翔会后准备七八个采访问题发到群里"
TRANSCRIPT = f"王昱翔 (00:01:00): {QUOTE}\n"
EXTRACTION = {
    "provider": "test",
    "model": "test",
    "input_sha256": "f" * 64,
    "action_items": [
        {
            "title": "准备7-8个采访问题",
            "deliverable": "采访问题清单",
            "owner_name": None,
            "deadline_text": None,
            "deadline_iso": None,
            "source_timestamp": "00:01:00",
            "source_quote": QUOTE,
            "confidence": 0.95,
            "needs_confirmation": True,
            "uncertainties": [],
        }
    ],
}


class BomToleranceTests(unittest.TestCase):
    """Windows tools write a BOM; nothing a person authors may choke on it."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_reader_strips_a_bom(self) -> None:
        path = self.root / "bom.txt"
        path.write_text(BOM + "hello", encoding="utf-8")

        self.assertEqual(read_text_file(path), "hello")

    def test_reader_is_unchanged_without_a_bom(self) -> None:
        path = self.root / "plain.txt"
        path.write_text("hello", encoding="utf-8")

        self.assertEqual(read_text_file(path), "hello")

    def test_env_file_with_a_bom_still_yields_the_first_key(self) -> None:
        """A BOM would otherwise corrupt the name of the very first setting."""

        env = self.root / ".env.local"
        env.write_text(
            BOM + "FEISHU_APP_ID=cli_abc\nFEISHU_APP_SECRET=secret_xyz\n",
            encoding="utf-8",
        )

        config = load_feishu_config(env)

        self.assertEqual(config.app_id, "cli_abc")

    def test_meeting_loads_from_bom_encoded_files(self) -> None:
        extraction = self.root / "e.json"
        transcript = self.root / "t.txt"
        extraction.write_text(
            BOM + json.dumps(EXTRACTION, ensure_ascii=False), encoding="utf-8"
        )
        transcript.write_text(BOM + TRANSCRIPT, encoding="utf-8")
        database = Database(self.root / "m.sqlite3")
        self.addCleanup(database.close)
        database.initialize()

        service = load_meeting_service(
            database,
            extraction_path=extraction,
            transcript_path=transcript,
            organization_name="测试团队",
            coordinator_name="王昱翔",
            participant_names=["王昱翔"],
        )

        row = database.one(
            "SELECT count(*) AS n FROM action_items WHERE episode_id = ?",
            (service.episode_id,),
        )
        self.assertEqual(dict(row)["n"], 1)


if __name__ == "__main__":
    unittest.main()
