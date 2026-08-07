from __future__ import annotations

import base64
import unittest

from collab_agent.attachments import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_COUNT,
    MAX_TOTAL_ATTACHMENT_BYTES,
    AttachmentLimitError,
    assert_within_upload_limits,
    extract_attachments,
)
from collab_agent.web import MAX_REQUEST_BYTES


def data_url(size: int, *, mime: str = "text/plain") -> str:
    encoded = base64.b64encode(b"x" * size).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def attachment(size: int, name: str = "notes.txt") -> dict:
    return {"name": name, "type": "text/plain", "size": size, "data": data_url(size)}


class UploadLimitTests(unittest.TestCase):
    def test_a_submission_within_the_limits_is_extracted(self) -> None:
        extracted = extract_attachments([attachment(1024)])
        self.assertEqual(len(extracted), 1)
        self.assertEqual(extracted[0]["extraction_status"], "EXTRACTED")

    def test_a_single_oversized_file_is_rejected_before_decoding(self) -> None:
        oversized = {
            "name": "big.txt",
            "type": "text/plain",
            "size": MAX_ATTACHMENT_BYTES + 1,
            "data": "data:text/plain;base64,eA==",
        }
        with self.assertRaisesRegex(AttachmentLimitError, "单文件"):
            assert_within_upload_limits([oversized])

    def test_the_declared_size_cannot_understate_the_encoded_payload(self) -> None:
        # A client that lies about `size` must still be measured by its data.
        lying = attachment(MAX_ATTACHMENT_BYTES + 1024, "lying.txt")
        lying["size"] = 10
        with self.assertRaises(AttachmentLimitError):
            assert_within_upload_limits([lying])

    def test_the_combined_total_is_capped(self) -> None:
        half = MAX_TOTAL_ATTACHMENT_BYTES // 2 + 1024
        files = [
            {
                "name": f"part-{index}.txt",
                "type": "text/plain",
                "size": half,
                "data": "data:text/plain;base64,eA==",
            }
            for index in range(2)
        ]
        with self.assertRaisesRegex(AttachmentLimitError, "总大小"):
            assert_within_upload_limits(files)

    def test_the_attachment_count_is_capped(self) -> None:
        files = [attachment(16, f"note-{index}.txt") for index in range(MAX_ATTACHMENT_COUNT + 1)]
        with self.assertRaisesRegex(AttachmentLimitError, "个附件"):
            assert_within_upload_limits(files)

    def test_extraction_enforces_the_limits_itself(self) -> None:
        # The service calls extract_attachments directly, so the guard cannot
        # live only in the HTTP layer.
        oversized = attachment(64, "ok.txt")
        oversized["size"] = MAX_ATTACHMENT_BYTES + 1
        with self.assertRaises(AttachmentLimitError):
            extract_attachments([oversized])

    def test_the_request_ceiling_leaves_room_for_base64_framing(self) -> None:
        self.assertGreater(
            MAX_REQUEST_BYTES, MAX_TOTAL_ATTACHMENT_BYTES * 4 // 3
        )


if __name__ == "__main__":
    unittest.main()
