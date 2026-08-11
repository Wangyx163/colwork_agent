"""Reuse an extraction that already exists for this exact transcript.

Extraction is the one step in the intake that costs money and tens of seconds,
and it is a pure function of the transcript: the same text yields the same
items, which is why `input_sha256` is `stable_hash(transcript)` and why the
episode id is derived from it. Two people sending the same 妙记 link, or one
person retrying after a timeout, should not pay for it twice.

## Why there is no migration

The key is a hash the extractions already carry. Every file already written to
`var/extractions/` -- including the four from before any of this existed -- is
therefore already a cache entry, found by reading its own `input_sha256`. There
is nothing to convert and nothing to keep in step; deleting the index would
lose no information, because the index *is* the files.

## Why the mode is explicit

`cache` never calls a model. `live` extracts on a miss. The demo runs on
`cache` and a miss there is refused with the reason rather than quietly
becoming a model call, because the demo path is required to be deterministic
and "it usually hits the cache" is not determinism.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import stable_hash


#: Where extractions have always been written. Kept as the cache directory
#: rather than a new one so that everything already there counts.
DEFAULT_EXTRACTIONS = Path("var/extractions")

#: Transcripts pulled from Feishu. Written under the hash so a re-fetch of the
#: same meeting overwrites itself rather than accumulating copies.
DEFAULT_TRANSCRIPTS = Path("var/transcripts")


class CacheMiss(LookupError):
    """No extraction exists for this transcript, and this mode will not make one."""


@dataclass(frozen=True)
class CacheEntry:
    transcript_hash: str
    extraction_path: Path
    transcript_path: Path


class IntakeCache:
    """Find, and record, the extraction for a given transcript."""

    def __init__(
        self,
        *,
        extractions_dir: Path | str = DEFAULT_EXTRACTIONS,
        transcripts_dir: Path | str = DEFAULT_TRANSCRIPTS,
    ) -> None:
        self.extractions_dir = Path(extractions_dir)
        self.transcripts_dir = Path(transcripts_dir)

    # ---- reading -------------------------------------------------------

    def index(self) -> dict[str, Path]:
        """transcript hash -> the extraction built from it.

        Rebuilt by reading the files each time rather than kept in a table. The
        directory is small, the read is cheap, and a stored index is one more
        thing that can disagree with what is on disk -- which for a cache means
        confidently returning a path to a file somebody deleted.

        On a duplicate the newest file wins: re-extracting the same meeting is
        how a better prompt version reaches an old transcript, and the reviewed
        output is the one that should be found.
        """

        found: dict[str, tuple[float, Path]] = {}
        if not self.extractions_dir.is_dir():
            return {}
        for path in self.extractions_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            digest = str(payload.get("input_sha256") or "").strip()
            if not digest:
                continue
            stamp = path.stat().st_mtime
            if digest not in found or stamp > found[digest][0]:
                found[digest] = (stamp, path)
        return {digest: path for digest, (_, path) in found.items()}

    def find(self, transcript: str) -> CacheEntry | None:
        digest = stable_hash(transcript)
        extraction = self.index().get(digest)
        if extraction is None:
            return None
        return CacheEntry(
            transcript_hash=digest,
            extraction_path=extraction,
            transcript_path=self.transcript_path(digest),
        )

    def transcript_path(self, digest: str) -> Path:
        return self.transcripts_dir / f"{digest[:20]}.txt"

    # ---- writing -------------------------------------------------------

    def store_transcript(self, transcript: str) -> Path:
        """Put the fetched text where a served meeting can keep reading it.

        Needed even on a cache hit: the episode is rebuilt from the transcript
        at every startup, so a transcript that only ever lived in memory would
        make the meeting unserveable the moment the process restarted.
        """

        digest = stable_hash(transcript)
        path = self.transcript_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_text(encoding="utf-8") != transcript:
            path.write_text(transcript, encoding="utf-8")
        return path

    def extraction_path_for(self, digest: str, *, name_hint: str = "") -> Path:
        stem = "".join(
            char for char in name_hint if char.isalnum() or char in "-_"
        )[:40]
        suffix = f"{stem}-{digest[:12]}" if stem else digest[:20]
        return self.extractions_dir / f"{suffix}.json"


def resolve(
    cache: IntakeCache,
    transcript: str,
    *,
    mode: str,
    extract: Any = None,
    name_hint: str = "",
) -> CacheEntry:
    """The extraction for this transcript, from cache or by making one.

    `extract` is injected rather than imported so the offline tests exercise
    this whole path without a provider -- the same shape the project uses for
    every other model call.
    """

    if mode not in {"cache", "live"}:
        raise ValueError("intake mode must be cache or live")
    transcript_path = cache.store_transcript(transcript)
    hit = cache.find(transcript)
    if hit is not None:
        return CacheEntry(
            transcript_hash=hit.transcript_hash,
            extraction_path=hit.extraction_path,
            transcript_path=transcript_path,
        )
    digest = stable_hash(transcript)
    if mode == "cache":
        raise CacheMiss(
            f"no extraction cached for transcript {digest[:12]}; "
            "run with intake mode 'live' to extract it"
        )
    if extract is None:
        raise CacheMiss(
            f"no extraction cached for transcript {digest[:12]} and no "
            "extractor was provided"
        )
    destination = cache.extraction_path_for(digest, name_hint=name_hint)
    destination.parent.mkdir(parents=True, exist_ok=True)
    extract(transcript_path, destination)
    if not destination.is_file():
        raise CacheMiss(f"extraction wrote nothing for {digest[:12]}")
    return CacheEntry(
        transcript_hash=digest,
        extraction_path=destination,
        transcript_path=transcript_path,
    )
