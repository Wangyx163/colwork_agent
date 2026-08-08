"""Pull a meeting transcript and its roster out of Feishu.

What this removes
-----------------
Today a meeting enters the system through three manual steps: someone exports
a transcript, retypes the attendee list into `--participant` flags, and binds
each person's open_id by hand. All three already exist inside Feishu -- the
minutes hold the transcript with speakers and timestamps, and the chat holds
the members with their open_ids. This fetches them.

What it deliberately does not do
--------------------------------
Create the episode. The roster is this project's authorisation boundary, and
"everyone currently in the chat" is not the same thing as "who attended the
meeting" -- a group keeps people who joined later and people who never spoke.
So this proposes a roster and prints the exact command to load it; a person
still decides who is on it. Removing the typing is worth doing; removing the
decision is not.

On the transcript format
------------------------
The export accepts `file_format` with `need_speaker` and `need_timestamp`, but
the precise shape of what comes back could not be confirmed from the published
documentation, and this has not been run against a live tenant. Rather than
hardcode one guessed layout, `parse_transcript` recognises SRT (a real spec)
and a line-oriented fallback, and raises with the first part of the raw body
when neither fits -- so the first real run diagnoses itself instead of failing
silently or, worse, producing plausible-looking wrong lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol


SRT_TIME = re.compile(
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{1,3})\s*-->"
)
# Located rather than matched in one pass: a single regex spanning speaker,
# timestamp and text kept mis-reading the seconds field as the separator.
PLAIN_TIME = re.compile(r"(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2}))?")
SPEAKER_PREFIX = re.compile(r"^(?P<speaker>[^:：]{1,24})\s*[:：]\s*(?P<text>.+)$")


class MinutesError(RuntimeError):
    """Raised when Feishu content cannot be turned into a usable transcript."""


@dataclass(frozen=True)
class TranscriptLine:
    speaker: str
    timestamp: str
    text: str

    def render(self) -> str:
        return f"{self.speaker}({self.timestamp}): {self.text}"


class MinutesTransport(Protocol):
    def get_transcript(self, minute_token: str, *, file_format: str = "srt") -> str: ...

    def get_chat_members(self, chat_id: str) -> list[dict[str, Any]]: ...


class RecordingMinutesTransport:
    """Offline transport, so the whole intake path is testable without a tenant."""

    def __init__(
        self,
        *,
        transcript: str = "",
        members: list[dict[str, Any]] | None = None,
    ) -> None:
        self.transcript = transcript
        self.members = members or []
        self.calls: list[dict[str, Any]] = []

    def get_transcript(self, minute_token: str, *, file_format: str = "srt") -> str:
        self.calls.append(
            {"kind": "transcript", "token": minute_token, "format": file_format}
        )
        return self.transcript

    def get_chat_members(self, chat_id: str) -> list[dict[str, Any]]:
        self.calls.append({"kind": "members", "chat_id": chat_id})
        return list(self.members)


class LarkMinutesTransport:
    """Real transport. Needs the minutes and chat read scopes to be granted."""

    def __init__(self, config: Any) -> None:
        import lark_oapi as lark

        self._client = (
            lark.Client.builder()
            .app_id(config.app_id)
            .app_secret(config.app_secret)
            .build()
        )

    def get_transcript(self, minute_token: str, *, file_format: str = "srt") -> str:
        from lark_oapi.api.minutes.v1 import GetMinuteTranscriptRequest

        request = (
            GetMinuteTranscriptRequest.builder()
            .minute_token(minute_token)
            .file_format(file_format)
            .need_speaker(True)
            .need_timestamp(True)
            .build()
        )
        response = self._client.minutes.v1.minute_transcript.get(request)
        if not response.success():
            raise MinutesError(
                f"妙记导出失败 code={response.code} msg={response.msg}；"
                "确认已开通「查看、下载妙记」权限，且应用可访问该妙记"
            )
        raw = getattr(response, "raw", None)
        body = getattr(raw, "content", b"") if raw is not None else b""
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        return str(body or "")

    def get_chat_members(self, chat_id: str) -> list[dict[str, Any]]:
        from lark_oapi.api.im.v1 import GetChatMembersRequest

        members: list[dict[str, Any]] = []
        page_token = None
        while True:
            builder = (
                GetChatMembersRequest.builder()
                .chat_id(chat_id)
                .member_id_type("open_id")
                .page_size(100)
            )
            if page_token:
                builder = builder.page_token(page_token)
            response = self._client.im.v1.chat_members.get(builder.build())
            if not response.success():
                raise MinutesError(
                    f"获取群成员失败 code={response.code} msg={response.msg}；"
                    "确认已开通「获取群成员信息」权限，且机器人在该群里"
                )
            data = response.data
            for item in getattr(data, "items", None) or []:
                members.append(
                    {
                        "open_id": getattr(item, "member_id", "") or "",
                        "name": getattr(item, "name", "") or "",
                    }
                )
            page_token = getattr(data, "page_token", None)
            if not getattr(data, "has_more", False) or not page_token:
                break
        return members


def _timestamp(hours: str, minutes: str, seconds: str | None) -> str:
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds or 0):02d}"


def parse_srt(raw: str) -> list[TranscriptLine]:
    """Parse SRT, which is an actual specification rather than a guess."""

    lines: list[TranscriptLine] = []
    for block in re.split(r"\n\s*\n", str(raw or "").replace("\r\n", "\n")):
        match = SRT_TIME.search(block)
        if not match:
            continue
        body_lines = [
            line.strip()
            for line in block.splitlines()
            if line.strip() and not SRT_TIME.search(line) and not line.strip().isdigit()
        ]
        if not body_lines:
            continue
        body = " ".join(body_lines)
        speaker = "发言人"
        prefixed = SPEAKER_PREFIX.match(body)
        if prefixed:
            speaker = prefixed.group("speaker").strip()
            body = prefixed.group("text").strip()
        lines.append(
            TranscriptLine(
                speaker=speaker,
                timestamp=_timestamp(
                    match.group("h"), match.group("m"), match.group("s")
                ),
                text=body,
            )
        )
    return lines


def parse_plain(raw: str) -> list[TranscriptLine]:
    """Parse a line-oriented export, tolerating where the speaker sits.

    Done in three steps rather than one regex: find the timestamp, take what
    precedes it as a possible speaker, and take what follows as the text --
    lifting a "name:" prefix out of the text only when no speaker was found
    before the timestamp. Both observed layouts fall out of that without the
    parser having to know which one it is looking at.
    """

    lines: list[TranscriptLine] = []
    for line in str(raw or "").replace("\r\n", "\n").splitlines():
        line = line.strip()
        if not line:
            continue
        match = PLAIN_TIME.search(line)
        if not match:
            continue

        before = line[: match.start()].strip(" \t[(【（-—·")
        after = line[match.end() :].lstrip(" \t]）】)")
        after = after.lstrip(":：").strip()
        if not after:
            continue

        speaker = before
        text = after
        if not speaker:
            prefixed = SPEAKER_PREFIX.match(after)
            if prefixed:
                speaker = prefixed.group("speaker").strip()
                text = prefixed.group("text").strip()
        if not speaker:
            speaker = "发言人"
        if not text:
            continue
        lines.append(
            TranscriptLine(
                speaker=speaker,
                timestamp=_timestamp(
                    match.group("h"), match.group("m"), match.group("s")
                ),
                text=text,
            )
        )
    return lines


def parse_transcript(raw: str) -> list[TranscriptLine]:
    """Recognise the export, or fail with enough of it to diagnose the shape."""

    for parser in (parse_srt, parse_plain):
        lines = parser(raw)
        if lines:
            return lines
    sample = re.sub(r"\s+", " ", str(raw or ""))[:300]
    raise MinutesError(
        "无法解析妙记导出内容：既不是 SRT 也不是可识别的逐行格式。"
        f"原始开头：{sample!r}。"
        "请把这段贴给开发，以便按真实格式补一个解析分支"
    )


def to_project_transcript(lines: list[TranscriptLine]) -> str:
    """Render as `speaker(HH:MM:SS): text`, the shape the extractor expects."""

    return "".join(f"{line.render()}\n" for line in lines)


def speakers_in(lines: list[TranscriptLine]) -> list[str]:
    """Who actually spoke, most talkative first."""

    counts: dict[str, int] = {}
    for line in lines:
        counts[line.speaker] = counts.get(line.speaker, 0) + 1
    return [
        name
        for name, _ in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


def propose_roster(
    lines: list[TranscriptLine], members: list[dict[str, Any]]
) -> dict[str, Any]:
    """Reconcile who spoke against who is in the chat.

    Neither list is the roster on its own. A chat keeps people who joined after
    the meeting and people who never attended; a transcript misses anyone who
    stayed silent. Both are reported so the person deciding sees the difference
    rather than inheriting one list's blind spot.
    """

    spoke = speakers_in(lines)
    member_names = [str(member.get("name") or "").strip() for member in members]
    member_names = [name for name in member_names if name]

    matched: list[dict[str, Any]] = []
    spoke_only: list[str] = []
    for speaker in spoke:
        member = next(
            (m for m in members if str(m.get("name") or "").strip() == speaker), None
        )
        if member:
            matched.append({"name": speaker, "open_id": member.get("open_id", "")})
        else:
            spoke_only.append(speaker)
    silent = [name for name in member_names if name not in spoke]

    return {
        "spoke_and_in_chat": matched,
        "spoke_but_not_in_chat": spoke_only,
        "in_chat_but_silent": silent,
    }


def intake(
    transport: MinutesTransport,
    *,
    minute_token: str,
    chat_id: str = "",
    file_format: str = "srt",
) -> dict[str, Any]:
    """Fetch a transcript and, when a chat is given, a roster proposal."""

    raw = transport.get_transcript(minute_token, file_format=file_format)
    lines = parse_transcript(raw)
    members = transport.get_chat_members(chat_id) if chat_id else []
    return {
        "minute_token": minute_token,
        "transcript": to_project_transcript(lines),
        "line_count": len(lines),
        "speakers": speakers_in(lines),
        "roster": propose_roster(lines, members),
    }
