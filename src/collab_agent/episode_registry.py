"""Remember how each meeting was loaded, so one process can serve them all.

A `CoordinationService` needs the extraction and the transcript, not just the
episode row: the episode is the state, and those two files are what the state
was derived from. Reconstructing them out of the database would mean writing a
second, parallel definition of what a meeting is -- and the day the two
disagreed, the served meeting would quietly stop matching the audited one.

So this keeps a pointer instead. `load_meeting_service` is already idempotent
on a given extraction: running it a second time finds the existing episode and
attaches to it rather than importing anything twice. Re-running it per meeting
at startup is therefore not a workaround, it is the intended entry point --
this table just records which arguments to run it with.

The slug is stored here too, assigned once and never recomputed. A slug that
moved when a new meeting was imported would break links that had already been
sent, which is the one thing a readable URL exists to avoid.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .slugs import RESERVED, SLUG_PATTERN, build_slug, hash_slug


REGISTRY_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS episode_sources (
        episode_id TEXT PRIMARY KEY,
        slug TEXT NOT NULL UNIQUE,
        extraction_path TEXT NOT NULL,
        transcript_path TEXT NOT NULL,
        organization_name TEXT NOT NULL,
        coordinator_name TEXT NOT NULL,
        participant_names TEXT NOT NULL,
        timezone TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        registered_sim_time TEXT NOT NULL
    )
    """,
)


@dataclass(frozen=True)
class EpisodeSource:
    """One row: everything needed to bring a meeting back up."""

    episode_id: str
    slug: str
    extraction_path: str
    transcript_path: str
    organization_name: str
    coordinator_name: str
    participant_names: list[str]
    timezone: str
    title: str = ""

    @property
    def files_present(self) -> bool:
        return (
            Path(self.extraction_path).is_file()
            and Path(self.transcript_path).is_file()
        )


def title_from_extraction(extraction_path: str | Path) -> str:
    """What to call this meeting, taken from what it was called at the source.

    Feishu names a transcript `<timestamp>-<会议名>-逐字稿文本-N.txt`, and that
    middle part is the name a person gave the meeting -- which is the only name
    that helps them recognise it in a list. Deriving one from the coordinator
    and the import date instead produced five rows reading "王昱翔的会议 ·
    2026-08-12", because the import date is when the file was read, not when
    the meeting happened.
    """

    try:
        payload = json.loads(Path(extraction_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    filename = str(((payload.get("source") or {}).get("filename") or "")).strip()
    if not filename:
        return ""
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", filename)
    stem = re.sub(r"-逐字稿文本(-\d+)?$", "", stem)
    match = re.match(r"^(\d{4})(\d{2})(\d{2})\d*-(.+)$", stem)
    if not match:
        return stem[:60]
    year, month, day, name = match.groups()
    return f"{name.strip()[:40]} · {year}-{month}-{day}"


def ensure_schema(database: Any) -> None:
    with database.transaction() as cursor:
        for statement in REGISTRY_SCHEMA_STATEMENTS:
            cursor.execute(statement)


def _row_to_source(row: Any) -> EpisodeSource:
    raw = row["participant_names"]
    names = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    return EpisodeSource(
        episode_id=row["episode_id"],
        slug=row["slug"],
        extraction_path=row["extraction_path"],
        transcript_path=row["transcript_path"],
        organization_name=row["organization_name"],
        coordinator_name=row["coordinator_name"],
        participant_names=[str(name) for name in names],
        timezone=row["timezone"],
        title=str(row["title"] or ""),
    )


def list_sources(database: Any) -> list[EpisodeSource]:
    """Registered meetings, oldest first -- the order their slugs numbered in."""

    ensure_schema(database)
    return [
        _row_to_source(row)
        for row in database.all(
            "SELECT * FROM episode_sources "
            "ORDER BY registered_sim_time, episode_id"
        )
    ]


def source_for(database: Any, episode_id: str) -> EpisodeSource | None:
    ensure_schema(database)
    row = database.one(
        "SELECT * FROM episode_sources WHERE episode_id = ?", (episode_id,)
    )
    return _row_to_source(row) if row else None


def source_for_slug(database: Any, slug: str) -> EpisodeSource | None:
    ensure_schema(database)
    row = database.one("SELECT * FROM episode_sources WHERE slug = ?", (slug,))
    return _row_to_source(row) if row else None


def assign_slug(database: Any, *, episode_id: str, coordinator_name: str) -> str:
    """Pick this meeting's URL, once.

    The ordinal counts the coordinator's meetings that are already registered,
    so it is decided by history rather than by a scan that could produce a
    different answer next time. On any collision the meeting takes its hash
    slug: an unreadable URL is a cosmetic loss, and handing a reader somebody
    else's meeting is not.
    """

    ensure_schema(database)
    existing = database.one(
        "SELECT slug FROM episode_sources WHERE episode_id = ?", (episode_id,)
    )
    if existing:
        return existing["slug"]
    taken = {
        row["slug"]
        for row in database.all("SELECT slug FROM episode_sources", ())
    }
    same_coordinator = database.one(
        "SELECT COUNT(*) AS n FROM episode_sources WHERE coordinator_name = ?",
        (coordinator_name,),
    )
    ordinal = int((same_coordinator or {"n": 0})["n"]) + 1
    fallback = hash_slug(episode_id)
    slug = build_slug(coordinator_name, ordinal, fallback=fallback)
    if slug in taken or slug in RESERVED or not SLUG_PATTERN.match(slug):
        slug = fallback
    while slug in taken:
        # Only reachable if two episode_ids share a tail, which the hash makes
        # unlikely rather than impossible. Suffixing beats raising: a meeting
        # that exists and cannot be served is worse than an ugly URL.
        slug = f"{slug}x"
    return slug


def register(
    database: Any,
    *,
    episode_id: str,
    extraction_path: str | Path,
    transcript_path: str | Path,
    organization_name: str,
    coordinator_name: str,
    participant_names: list[str],
    timezone: str,
    sim_time: str,
    title: str = "",
) -> EpisodeSource:
    """Record how to bring this meeting back, keeping any slug already given.

    Paths are stored resolved. A relative path recorded from one working
    directory and re-read from another points at nothing, and the failure would
    look like a missing meeting rather than a missing file.
    """

    ensure_schema(database)
    slug = assign_slug(
        database, episode_id=episode_id, coordinator_name=coordinator_name
    )
    extraction = str(Path(extraction_path).resolve())
    transcript = str(Path(transcript_path).resolve())
    names = json.dumps(list(participant_names), ensure_ascii=False)
    with database.transaction() as cursor:
        existing = cursor.execute(
            "SELECT episode_id FROM episode_sources WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if existing:
            cursor.execute(
                "UPDATE episode_sources SET extraction_path = ?, "
                "transcript_path = ?, organization_name = ?, "
                "coordinator_name = ?, participant_names = ?, timezone = ?, "
                "title = ? WHERE episode_id = ?",
                (
                    extraction,
                    transcript,
                    organization_name,
                    coordinator_name,
                    names,
                    timezone,
                    title,
                    episode_id,
                ),
            )
        else:
            cursor.execute(
                "INSERT INTO episode_sources("
                "episode_id, slug, extraction_path, transcript_path, "
                "organization_name, coordinator_name, participant_names, "
                "timezone, title, registered_sim_time"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    episode_id,
                    slug,
                    extraction,
                    transcript,
                    organization_name,
                    coordinator_name,
                    names,
                    timezone,
                    title,
                    sim_time,
                ),
            )
    return EpisodeSource(
        episode_id=episode_id,
        slug=slug,
        extraction_path=extraction,
        transcript_path=transcript,
        organization_name=organization_name,
        coordinator_name=coordinator_name,
        participant_names=list(participant_names),
        timezone=timezone,
        title=title,
    )
