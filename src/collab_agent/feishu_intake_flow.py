"""Turn a 妙记 link dropped in a chat into a served meeting.

The steps were all already here -- pull a transcript, reconcile a roster,
extract, import, serve -- as five terminal commands run by somebody who knew
the order. This is that sequence with the person taken out of the middle of it.

## Why it is not one straight line

Two of the steps are slow for unrelated reasons: extraction takes tens of
seconds because it is a model call, and roster confirmation takes as long as a
human takes to read a card. Running them in series means the second slow thing
starts only when the first finishes, for no reason at all -- extraction needs
the transcript and nothing else, and the roster needs the chat and nothing
else.

So they run at the same time and whichever finishes second finishes the intake.
The race is settled by a conditional UPDATE on the row's status, the same way
the Outbox settles two dispatchers: both sides try, one wins, the loser is a
no-op rather than a second import.

## What it will not decide

The roster. `propose_roster` returns three buckets -- spoke and in the chat,
spoke but not in the chat, in the chat but silent -- and this shows all three
and waits. Nothing here promotes a transcript's speaker list into a
participant list, because the roster is the authorization boundary and the one
rule about it is that people are not inferred from what was said.
"""

from __future__ import annotations

import json
from typing import Any

from . import episode_registry
from .feishu_minutes import MinutesError, intake as fetch_minutes
from .intake_cache import CacheMiss, IntakeCache, resolve as resolve_extraction
from .models import canonical_json, stable_hash


INTAKE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS meeting_intakes (
        intake_id TEXT PRIMARY KEY,
        minute_token TEXT NOT NULL,
        transcript_hash TEXT NOT NULL DEFAULT '',
        chat_id TEXT NOT NULL DEFAULT '',
        requested_by_open_id TEXT NOT NULL,
        coordinator_name TEXT NOT NULL DEFAULT '',
        organization_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        roster_proposal TEXT NOT NULL DEFAULT '{}',
        chosen_roster TEXT NOT NULL DEFAULT '',
        extraction_path TEXT NOT NULL DEFAULT '',
        episode_id TEXT NOT NULL DEFAULT '',
        slug TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        started_sim_time TEXT NOT NULL
    )
    """,
)

#: Card actions this flow answers.
ROSTER_CONFIRM = "INTAKE_ROSTER_CONFIRM"

#: Which bucket a confirm button takes. `spoke` is the common case in one tap;
#: `all` adds the people who were in the chat and said nothing, who are real
#: attendees often enough that making them a second round trip would be rude.
ROSTER_CHOICES = ("spoke", "all")


class IntakeError(RuntimeError):
    """The intake cannot go on, with a reason worth showing somebody."""


def ensure_schema(database: Any) -> None:
    with database.transaction() as cursor:
        for statement in INTAKE_SCHEMA_STATEMENTS:
            cursor.execute(statement)


def intake_id_for(minute_token: str) -> str:
    """One intake per 妙记, so pasting the link twice is not two meetings."""

    return f"intake_{stable_hash(minute_token)[:20]}"


def build_roster_card(
    *, intake_id: str, title: str, roster: dict[str, Any]
) -> dict[str, Any]:
    """Show all three buckets and let the coordinator pick, in one tap.

    All three are shown rather than only the confident one because they mean
    different things and only a person knows which applies: somebody who spoke
    but is not in the chat may be an external guest or a transcription variant
    of a name, and somebody in the chat who never spoke may have attended in
    silence or not attended at all.
    """

    spoke = [row["name"] for row in roster.get("spoke_and_in_chat", [])]
    outside = list(roster.get("spoke_but_not_in_chat", []))
    silent = list(roster.get("in_chat_but_silent", []))

    lines = [f"**{title}**", ""]
    lines.append(f"发言且在群里（{len(spoke)}）：{'、'.join(spoke) or '无'}")
    if silent:
        lines.append(f"在群里但全程没发言（{len(silent)}）：{'、'.join(silent)}")
    if outside:
        lines.append(
            f"发言但不在群里（{len(outside)}）：{'、'.join(outside)}\n"
            "这些人我不会自动加——可能是转写把名字写岔了，也可能是外部人员。"
        )
    lines.append("")
    lines.append("参会名单决定谁能看到和认领任务，所以由你定，我不从逐字稿猜。")

    actions = [
        {
            "tag": "button",
            "text": {
                "tag": "plain_text",
                "content": f"就这 {len(spoke)} 人",
            },
            "type": "primary",
            "value": {
                "action": ROSTER_CONFIRM,
                "intake_id": intake_id,
                "choice": "spoke",
            },
        }
    ]
    if silent:
        actions.append(
            {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": f"加上没发言的，共 {len(spoke) + len(silent)} 人",
                },
                "value": {
                    "action": ROSTER_CONFIRM,
                    "intake_id": intake_id,
                    "choice": "all",
                },
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "确认参会名单"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {"tag": "action", "actions": actions},
        ],
    }


class MeetingIntake:
    """Drive one 妙记 link from a chat message to a served meeting."""

    def __init__(
        self,
        database: Any,
        *,
        transport: Any,
        organization_name: str,
        cache: IntakeCache | None = None,
        extract: Any = None,
        mode: str = "cache",
        base_url: str = "",
        log: Any = None,
    ) -> None:
        self.database = database
        self.transport = transport
        self.organization_name = organization_name
        self.cache = cache or IntakeCache()
        self.extract = extract
        self.mode = mode
        self.base_url = base_url.rstrip("/")
        self.log = log or (lambda line: None)
        ensure_schema(database)

    # ---- reading -------------------------------------------------------

    def row(self, intake_id: str) -> dict[str, Any] | None:
        found = self.database.one(
            "SELECT * FROM meeting_intakes WHERE intake_id = ?", (intake_id,)
        )
        return dict(found) if found else None

    def _coordinator_name(self, open_id: str) -> str:
        row = self.database.one(
            "SELECT display_name FROM feishu_identity_bindings WHERE open_id = ?",
            (open_id,),
        )
        return str(row["display_name"]) if row else ""

    def _set(self, intake_id: str, **columns: Any) -> None:
        assignments = ", ".join(f"{name} = ?" for name in columns)
        with self.database.transaction() as cursor:
            cursor.execute(
                f"UPDATE meeting_intakes SET {assignments} WHERE intake_id = ?",
                (*columns.values(), intake_id),
            )

    # ---- step one: the transcript and the roster -----------------------

    def start(
        self, *, minute_token: str, chat_id: str, open_id: str, sim_time: str
    ) -> dict[str, Any]:
        """Fetch, propose a roster, and hand back the card asking about it.

        Deliberately does not extract: this call answers a person who is
        waiting, and extraction is what the caller starts on another thread the
        moment this returns.
        """

        coordinator = self._coordinator_name(open_id)
        if not coordinator:
            raise IntakeError(
                "我不知道你是谁——先 @ 我一下，带上会议和你的名字完成注册，"
                "再把妙记链接发我。"
            )
        intake_id = intake_id_for(minute_token)
        existing = self.row(intake_id)
        if existing and existing["status"] == "READY":
            return {
                "intake_id": intake_id,
                "status": "READY",
                "slug": existing["slug"],
                "url": self._url(existing["slug"]),
            }

        try:
            fetched = fetch_minutes(
                self.transport, minute_token=minute_token, chat_id=chat_id
            )
        except MinutesError as error:
            # Re-raised as this flow's own type, message intact. A caller
            # deciding what to show a person should not have to know which
            # transport was underneath.
            raise IntakeError(str(error)) from error
        transcript = fetched["transcript"]
        if not transcript.strip():
            raise IntakeError("这份妙记导出来是空的，确认它已经转写完成了吗？")
        transcript_path = self.cache.store_transcript(transcript)
        digest = stable_hash(transcript)
        roster = fetched["roster"]

        with self.database.transaction() as cursor:
            cursor.execute(
                "DELETE FROM meeting_intakes WHERE intake_id = ?", (intake_id,)
            )
            cursor.execute(
                "INSERT INTO meeting_intakes("
                "intake_id, minute_token, transcript_hash, chat_id, "
                "requested_by_open_id, coordinator_name, organization_name, "
                "status, roster_proposal, started_sim_time"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, 'AWAITING_ROSTER', ?, ?)",
                (
                    intake_id,
                    minute_token,
                    digest,
                    chat_id,
                    open_id,
                    coordinator,
                    self.organization_name,
                    canonical_json(roster),
                    sim_time,
                ),
            )
        return {
            "intake_id": intake_id,
            "status": "AWAITING_ROSTER",
            "transcript_path": str(transcript_path),
            "transcript": transcript,
            "line_count": fetched["line_count"],
            "speakers": fetched["speakers"],
            "roster": roster,
            "card": build_roster_card(
                intake_id=intake_id,
                title=f"{fetched['line_count']} 行逐字稿，"
                f"{len(fetched['speakers'])} 位发言人",
                roster=roster,
            ),
        }

    # ---- step two, in parallel with step three -------------------------

    def run_extraction(self, intake_id: str, transcript: str) -> dict[str, Any]:
        """The slow half. Safe to run on a worker thread while a person reads.

        A failure is recorded rather than raised out of the thread: the
        coordinator is waiting on a card, and a traceback in a log they cannot
        see is the same as silence.
        """

        try:
            entry = resolve_extraction(
                self.cache,
                transcript,
                mode=self.mode,
                extract=self.extract,
                name_hint=intake_id[-8:],
            )
        except CacheMiss as error:
            self._set(intake_id, status="FAILED", error=str(error))
            return {"status": "FAILED", "error": str(error)}
        except Exception as error:  # noqa: BLE001 - a thread must not lose this
            self._set(intake_id, status="FAILED", error=repr(error))
            return {"status": "FAILED", "error": repr(error)}
        self._set(intake_id, extraction_path=str(entry.extraction_path))
        return {"status": "EXTRACTED", "extraction_path": str(entry.extraction_path)}

    def confirm_roster(
        self, *, intake_id: str, choice: str, open_id: str
    ) -> dict[str, Any]:
        """Record who is in the meeting. Only the person who asked for it may.

        Not "any coordinator": at this moment there is no episode and therefore
        no roster to check anybody against, so the only defensible authority is
        that this is the person who started it.
        """

        row = self.row(intake_id)
        if row is None:
            raise IntakeError("这次导入已经过期了，把妙记链接重新发我一次。")
        if row["requested_by_open_id"] != open_id:
            raise PermissionError("只有发起这次导入的人可以确认名单")
        if choice not in ROSTER_CHOICES:
            raise IntakeError("不认识这个选择")

        roster = json.loads(row["roster_proposal"] or "{}")
        names = [entry["name"] for entry in roster.get("spoke_and_in_chat", [])]
        if choice == "all":
            names = names + list(roster.get("in_chat_but_silent", []))
        coordinator = row["coordinator_name"]
        if coordinator and coordinator not in names:
            # The person who convened it is in it. Leaving them out produced a
            # meeting whose coordinator could not open their own console.
            names.append(coordinator)
        if not names:
            raise IntakeError("这样一个人都不剩，没法建会。")
        self._set(intake_id, chosen_roster=canonical_json(names))
        return {"status": "ROSTER_CHOSEN", "names": names}

    # ---- step four: whoever finishes second -----------------------------

    def finish_if_ready(self, intake_id: str, *, sim_time: str) -> dict[str, Any]:
        """Import and register, but only once.

        Claimed with a conditional UPDATE for the same reason the Outbox is:
        the roster confirmation and the extraction finish on different threads,
        both then call this, and exactly one of them must do the import.
        """

        row = self.row(intake_id)
        if row is None:
            return {"status": "GONE"}
        if row["status"] == "READY":
            return {
                "status": "READY",
                "slug": row["slug"],
                "url": self._url(row["slug"]),
                "already": True,
            }
        if not row["chosen_roster"] or not row["extraction_path"]:
            return {"status": "WAITING"}

        with self.database.transaction() as cursor:
            claimed = cursor.execute(
                "UPDATE meeting_intakes SET status = 'IMPORTING' "
                "WHERE intake_id = ? AND status = 'AWAITING_ROSTER'",
                (intake_id,),
            ).rowcount
        if not claimed:
            return {"status": "WAITING"}

        try:
            outcome = self._import(row, sim_time=sim_time)
        except Exception as error:  # noqa: BLE001 - report, do not lose
            self._set(intake_id, status="FAILED", error=repr(error))
            raise
        self._set(
            intake_id,
            status="READY",
            episode_id=outcome["episode_id"],
            slug=outcome["slug"],
        )
        return {**outcome, "status": "READY", "url": self._url(outcome["slug"])}

    def _import(self, row: dict[str, Any], *, sim_time: str) -> dict[str, Any]:
        from .meeting import load_meeting_service  # noqa: PLC0415 - avoid a cycle

        names = json.loads(row["chosen_roster"])
        service = load_meeting_service(
            self.database,
            extraction_path=row["extraction_path"],
            transcript_path=str(self.cache.transcript_path(row["transcript_hash"])),
            organization_name=row["organization_name"],
            coordinator_name=row["coordinator_name"],
            participant_names=names,
        )
        source = episode_registry.register(
            self.database,
            episode_id=service.episode_id,
            extraction_path=row["extraction_path"],
            transcript_path=str(self.cache.transcript_path(row["transcript_hash"])),
            organization_name=row["organization_name"],
            coordinator_name=row["coordinator_name"],
            participant_names=names,
            timezone="Australia/Sydney",
            sim_time=sim_time,
            title=episode_registry.title_from_extraction(row["extraction_path"]),
        )
        return {
            "episode_id": service.episode_id,
            "slug": source.slug,
            "title": source.title,
            "participant_names": names,
        }

    def _url(self, slug: str) -> str:
        return f"{self.base_url}/{slug}/manage" if self.base_url else f"/{slug}/manage"
