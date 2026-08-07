from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


def read_text_file(path: str | Path) -> str:
    """Read a UTF-8 text file, tolerating a byte order mark.

    Windows tools write one by default -- PowerShell's `Set-Content -Encoding
    utf8`, Notepad, most spreadsheet exports. Reading such a file as plain
    "utf-8" fails on the very first character, and the error names column 1 of
    a file that looks perfectly fine in an editor. `utf-8-sig` strips a BOM
    when present and behaves exactly like "utf-8" when it is not, so this is
    strictly more permissive and never changes the parsed content.

    This project is Windows-first, so every file a person might author or edit
    is read through here.
    """

    return Path(path).read_text(encoding="utf-8-sig")


class EpisodeStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    READY_FOR_FINAL_APPROVAL = "READY_FOR_FINAL_APPROVAL"
    APPROVED = "APPROVED"
    ARCHIVED = "ARCHIVED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class ActionItemStatus(StrEnum):
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    PENDING_ASSIGNMENT = "PENDING_ASSIGNMENT"
    NEEDS_REVISION = "NEEDS_REVISION"
    TRACKING = "TRACKING"
    PENDING_ACCEPTANCE = "PENDING_ACCEPTANCE"
    ACCEPTED = "ACCEPTED"
    AGGREGATED = "AGGREGATED"
    ARCHIVED = "ARCHIVED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"


class AssignmentRole(StrEnum):
    OWNER = "OWNER"
    COLLABORATOR = "COLLABORATOR"


class AssignmentResponse(StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    RETURNED = "RETURNED"
    SUPERSEDED = "SUPERSEDED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RETRY_WAIT = "RETRY_WAIT"
    DELIVERED = "DELIVERED"
    DEAD_LETTER = "DEAD_LETTER"


class ValidationStatus(StrEnum):
    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"


class SimulatedCrash(RuntimeError):
    """Raised by an explicit P0 crash injection point."""


# Returning a dispatch requires a reason, and every surface must offer the same
# ones. Kept here rather than in either surface so the web workbench and the
# Feishu card cannot drift apart: a reason recorded from one has to mean the
# same thing when read from the other.
ASSIGNMENT_RETURN_REASONS = (
    "任务定义不清楚，需要补充验收标准",
    "时间安排不可行，需要调整工期",
    "不该由我负责，请重新指派",
)


def parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("simulation timestamps must include a timezone")
    return parsed


def iso_time(value: str | datetime) -> str:
    return parse_time(value).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def effect_id(
    *,
    episode_id: str,
    subject_id: str,
    effect_type: str,
    trigger_key: str,
) -> str:
    digest = stable_hash(
        {
            "episode_id": episode_id,
            "subject_id": subject_id,
            "effect_type": effect_type,
            "trigger_key": trigger_key,
        }
    )
    return f"eff_{digest[:32]}"
