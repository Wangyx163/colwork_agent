"""Give each meeting a URL a person can read out loud.

`episode_meeting_9f3c1a...` is the right identifier for the database and the
wrong one for a chat message: nobody retypes it, and nobody can tell two of
them apart at a glance. So a meeting also gets a slug built from the one thing
everybody in the room already knows -- whose meeting it was -- plus which of
that person's meetings it is.

The ordinal is not decoration. One coordinator runs many meetings, and the
name alone would collide on the second one; numbering them in creation order
keeps every slug stable for as long as the meeting exists, which is what makes
a link worth sending.

Pinyin needs `pypinyin`, and this stays an optional extra rather than joining a
dependency list that is one package long. Without it the slug falls back to the
episode's own hash: uglier, still unique, still stable. Nothing but the reading
of the URL changes.
"""

from __future__ import annotations

import re
from typing import Any


#: Paths the console already owns. A meeting that slugged to one of these would
#: shadow a page for every meeting at once, so these are refused and the
#: fallback is used instead.
RESERVED = frozenset(
    {
        "api",
        "console",
        "manage",
        "tasks",
        "observatory",
        "diagnostics",
        "static",
        "favicon.ico",
    }
)

SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def _pinyin(name: str) -> str:
    """Latinise a display name, or return "" when that is not possible here."""

    try:
        from pypinyin import Style, lazy_pinyin  # noqa: PLC0415 - optional extra
    except ImportError:
        return ""
    try:
        # "default" keeps characters pinyin cannot read instead of dropping
        # them: 黄Z恒 has to stay distinguishable from 黄恒, and "ignore" made
        # both of them huangheng.
        parts = lazy_pinyin(name, style=Style.NORMAL, errors="default")
    except Exception:  # noqa: BLE001 - a slug is never worth an exception
        return ""
    return "".join(parts)


def name_stem(display_name: str) -> str:
    """The readable half of a slug, or "" when the name yields nothing usable.

    ASCII names pass through: `Jasmine` should read as `jasmine`, not as the
    pinyin of nothing. Mixed names keep whatever each half gives.
    """

    raw = (display_name or "").strip()
    if not raw:
        return ""
    latin = _pinyin(raw) or raw
    stem = re.sub(r"[^a-z0-9]+", "", latin.lower())
    return stem[:32]


def build_slug(display_name: str, ordinal: int, *, fallback: str) -> str:
    """`wangyuxiang01`, or the fallback when the name gives nothing to read.

    The ordinal is always two digits and always present, including on the
    first: `wangyuxiang01` and `wangyuxiang02` sort and read as a pair, while
    `wangyuxiang` and `wangyuxiang02` look like different kinds of thing.
    """

    stem = name_stem(display_name)
    if not stem or stem in RESERVED:
        return fallback
    candidate = f"{stem}{ordinal:02d}"
    if not SLUG_PATTERN.match(candidate) or candidate in RESERVED:
        return fallback
    return candidate


def hash_slug(episode_id: str) -> str:
    """The fallback: unique and stable, just not pronounceable."""

    tail = re.sub(r"[^a-z0-9]+", "", episode_id.lower())[-10:]
    return f"m-{tail or '0'}"


def slugs_for_episodes(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Map episode_id -> slug for a whole database at once.

    Done as a batch rather than per episode because the ordinal is a property
    of the *set*: which of this coordinator's meetings a meeting is cannot be
    answered by looking at that meeting alone. Rows are expected to carry
    `episode_id`, `owner_display_name` and `created_sim_time`.

    A collision -- two coordinators whose names latinise the same way -- gives
    the later meeting its hash slug instead of silently stealing the earlier
    one's URL. Losing a readable name is a cosmetic loss; handing out somebody
    else's meeting is not.
    """

    ordered = sorted(
        rows,
        key=lambda row: (
            str(row.get("created_sim_time") or ""),
            str(row.get("episode_id") or ""),
        ),
    )
    seen_per_owner: dict[str, int] = {}
    taken: set[str] = set()
    slugs: dict[str, str] = {}
    for row in ordered:
        episode_id = str(row.get("episode_id") or "")
        if not episode_id:
            continue
        name = str(row.get("owner_display_name") or "")
        stem = name_stem(name)
        seen_per_owner[stem] = seen_per_owner.get(stem, 0) + 1
        fallback = hash_slug(episode_id)
        slug = build_slug(name, seen_per_owner[stem], fallback=fallback)
        if slug in taken:
            slug = fallback
        taken.add(slug)
        slugs[episode_id] = slug
    return slugs
