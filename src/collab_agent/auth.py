from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

from .models import canonical_json
from .store import Database


class PrincipalRole(StrEnum):
    COORDINATOR = "COORDINATOR"
    PARTICIPANT = "PARTICIPANT"
    SYSTEM = "SYSTEM"


ROLE_ALIASES = {
    "AGGREGATOR": PrincipalRole.COORDINATOR,
    "COORDINATOR": PrincipalRole.COORDINATOR,
    "ACTION_OWNER": PrincipalRole.PARTICIPANT,
    "PARTICIPANT": PrincipalRole.PARTICIPANT,
}


@dataclass(frozen=True)
class Principal:
    actor_id: str
    episode_id: str
    roles: frozenset[PrincipalRole]
    auth_source: str
    session_id: str

    def has_role(self, role: PrincipalRole) -> bool:
        return role in self.roles

    @property
    def is_coordinator(self) -> bool:
        return self.has_role(PrincipalRole.COORDINATOR)

    @property
    def is_participant(self) -> bool:
        return self.has_role(PrincipalRole.PARTICIPANT)


class PrincipalError(PermissionError):
    pass


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


class VirtualSessionPrincipalProvider:
    """Stateless signed sessions for the P0 virtual-role workbench.

    The actor may be selected only from the current EpisodeParticipant set.
    Business endpoints receive the resolved Principal and never trust an
    actor_id supplied in their request body.
    """

    def __init__(
        self,
        database: Database,
        *,
        episode_id: str,
        secret: str | bytes | None = None,
    ):
        self.db = database
        self.episode_id = episode_id
        if secret is None:
            secret = secrets.token_bytes(32)
        elif isinstance(secret, str):
            secret = secret.encode("utf-8")
        if len(secret) < 16:
            raise ValueError("virtual session secret must be at least 16 bytes")
        self._secret = secret

    def list_selectable_actors(self) -> list[dict[str, Any]]:
        rows = self.db.all(
            """
            SELECT a.actor_id, a.display_name, ep.role
            FROM episode_participants ep
            JOIN actors a ON a.actor_id = ep.actor_id
            WHERE ep.episode_id = ? AND a.status = 'ACTIVE'
            ORDER BY a.display_name, a.actor_id, ep.role
            """,
            (self.episode_id,),
        )
        actors: dict[str, dict[str, Any]] = {}
        for row in rows:
            mapped = ROLE_ALIASES.get(row["role"])
            if mapped is None:
                continue
            actor = actors.setdefault(
                row["actor_id"],
                {
                    "actor_id": row["actor_id"],
                    "display_name": row["display_name"],
                    "roles": set(),
                },
            )
            actor["roles"].add(mapped.value)
        return [
            {**actor, "roles": sorted(actor["roles"])}
            for actor in actors.values()
            if actor["roles"]
        ]

    def _roles_for_actor(self, actor_id: str) -> frozenset[PrincipalRole]:
        rows = self.db.all(
            """
            SELECT ep.role
            FROM episode_participants ep
            JOIN actors a ON a.actor_id = ep.actor_id
            WHERE ep.episode_id = ? AND ep.actor_id = ? AND a.status = 'ACTIVE'
            """,
            (self.episode_id, actor_id),
        )
        roles = {
            ROLE_ALIASES[row["role"]]
            for row in rows
            if row["role"] in ROLE_ALIASES
        }
        if not roles:
            raise PrincipalError("actor is not an active participant in this meeting")
        return frozenset(roles)

    def issue(self, actor_id: str) -> dict[str, Any]:
        roles = self._roles_for_actor(actor_id)
        payload = {
            "actor_id": actor_id,
            "episode_id": self.episode_id,
            "roles": sorted(role.value for role in roles),
            "auth_source": "virtual_session",
            "session_id": f"vs_{uuid4().hex}",
        }
        encoded = _b64encode(canonical_json(payload).encode("utf-8"))
        signature = _b64encode(
            hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        return {
            "token": f"{encoded}.{signature}",
            "principal": payload,
        }

    def resolve(self, token: str) -> Principal:
        try:
            encoded, signature = token.split(".", 1)
        except ValueError as error:
            raise PrincipalError("invalid virtual session token") from error
        expected = hmac.new(
            self._secret, encoded.encode("ascii"), hashlib.sha256
        ).digest()
        try:
            supplied = _b64decode(signature)
        except Exception as error:
            raise PrincipalError("invalid virtual session signature") from error
        if _b64encode(supplied) != signature:
            raise PrincipalError("non-canonical virtual session signature")
        if not hmac.compare_digest(expected, supplied):
            raise PrincipalError("invalid virtual session signature")
        try:
            payload = json.loads(_b64decode(encoded))
        except Exception as error:
            raise PrincipalError("invalid virtual session payload") from error
        if payload.get("episode_id") != self.episode_id:
            raise PrincipalError("virtual session belongs to another episode")
        actor_id = str(payload.get("actor_id", ""))
        current_roles = self._roles_for_actor(actor_id)
        token_roles = frozenset(
            PrincipalRole(value) for value in payload.get("roles", [])
        )
        if token_roles != current_roles:
            raise PrincipalError("virtual session roles are stale")
        return Principal(
            actor_id=actor_id,
            episode_id=self.episode_id,
            roles=current_roles,
            auth_source=str(payload.get("auth_source", "virtual_session")),
            session_id=str(payload.get("session_id", "")),
        )

    def resolve_authorization_header(self, header: str | None) -> Principal:
        if not header or not header.startswith("Bearer "):
            raise PrincipalError("missing virtual session")
        return self.resolve(header.removeprefix("Bearer ").strip())


class AuthorizationService:
    def __init__(self, database: Database, *, episode_id: str):
        self.db = database
        self.episode_id = episode_id

    def require_episode(self, principal: Principal) -> None:
        if principal.episode_id != self.episode_id:
            raise PrincipalError("principal belongs to another episode")

    def require_coordinator(self, principal: Principal) -> None:
        self.require_episode(principal)
        if not principal.is_coordinator:
            raise PrincipalError("only the meeting coordinator may perform this action")

    def require_participant(self, principal: Principal) -> None:
        self.require_episode(principal)
        if not principal.is_participant:
            raise PrincipalError("only a meeting participant may perform this action")

    def require_action_owner(self, principal: Principal, action_item_id: str) -> None:
        self.require_participant(principal)
        row = self.db.one(
            "SELECT owner_actor_id FROM action_items "
            "WHERE episode_id = ? AND action_item_id = ?",
            (self.episode_id, action_item_id),
        )
        if not row:
            raise KeyError(action_item_id)
        if row["owner_actor_id"] != principal.actor_id:
            raise PrincipalError("only the task executor may perform this action")

    def require_action_contributor(
        self, principal: Principal, action_item_id: str
    ) -> None:
        """Authorize the owner or a collaborator on the same ActionItem.

        Collaborators are derived from meeting-grounded metadata or an active
        assistance request.  They do not create a second task or workflow.
        """

        self.require_participant(principal)
        row = self.db.one(
            "SELECT owner_actor_id, proposal_metadata, status, definition_version "
            "FROM action_items "
            "WHERE episode_id = ? AND action_item_id = ?",
            (self.episode_id, action_item_id),
        )
        if not row:
            raise KeyError(action_item_id)
        if row["owner_actor_id"] == principal.actor_id:
            return
        assignment_count = self.db.one(
            "SELECT COUNT(*) AS count FROM action_item_assignments "
            "WHERE action_item_id = ?",
            (action_item_id,),
        )
        if assignment_count and int(assignment_count["count"]) > 0:
            accepted_assignment = self.db.one(
                "SELECT 1 FROM action_item_assignments WHERE action_item_id = ? "
                "AND definition_version = ? AND actor_id = ? "
                "AND assignment_role = 'COLLABORATOR' "
                "AND response_status = 'ACCEPTED'",
                (
                    action_item_id,
                    int(row["definition_version"] or 1),
                    principal.actor_id,
                ),
            )
            if accepted_assignment and row["status"] in {
                "TRACKING",
                "PENDING_ACCEPTANCE",
                "ACCEPTED",
                "AGGREGATED",
            }:
                return
        else:
            # Backward-compatible authorization for meetings created before ADR-035.
            raw_metadata = row["proposal_metadata"] or "{}"
            metadata = (
                json.loads(raw_metadata)
                if isinstance(raw_metadata, str)
                else dict(raw_metadata)
            )
            if principal.actor_id in metadata.get("collaborator_actor_ids", []):
                return
        active_help = self.db.one(
            "SELECT 1 FROM assistance_requests WHERE episode_id = ? "
            "AND action_item_id = ? AND target_actor_id = ? "
            "AND status IN ('OPEN','ACKNOWLEDGED')",
            (self.episode_id, action_item_id, principal.actor_id),
        )
        if active_help:
            return
        raise PrincipalError(
            "only the task executor or an active collaborator may perform this action"
        )


#: Where an operator credential comes from when somebody sets one themselves.
OPERATOR_TOKEN_ENV = "COLWORK_OPERATOR_TOKEN"


def resolve_operator_token() -> tuple[str, str]:
    """The credential for the Observatory, and where it came from.

    The Observatory is an operator surface, not a role inside a meeting, and
    that distinction is what fixes a real hole rather than papering over it.
    While it was gated on "are you this meeting's coordinator" it also took the
    episode to show as a query parameter -- harmless when a process served one
    meeting, because the only episode you could name was your own, and a
    cross-meeting read the moment one process served several.

    At the root there is no episode, so there is no roster, so "are you the
    coordinator" is not a question that can be asked. A separate credential is
    therefore not a workaround for the move; it is the reason the move is
    correct.

    Returns (token, source). Generated per process when nothing is set: a
    default of "open" would mean every deployment that never read the docs is
    serving every meeting's audit trail to anyone who finds the URL.
    """

    from os import environ  # noqa: PLC0415 - read at call time, not at import
    from secrets import token_urlsafe  # noqa: PLC0415

    configured = (environ.get(OPERATOR_TOKEN_ENV) or "").strip()
    if configured:
        return configured, "env"
    return token_urlsafe(24), "generated"


def operator_token_matches(expected: str, authorization_header: str | None) -> bool:
    """Whether a request carries the operator credential.

    Compared with `compare_digest` because this is a secret, and a plain `==`
    on a secret leaks its length and prefix through timing. Cheap to do right.
    """

    from secrets import compare_digest  # noqa: PLC0415

    if not expected:
        return False
    raw = (authorization_header or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if not raw:
        return False
    return compare_digest(raw, expected)
