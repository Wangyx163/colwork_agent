from __future__ import annotations

from typing import Any

from .models import parse_time


class ContentPackError(ValueError):
    pass


def _require(mapping: dict[str, Any], fields: set[str], path: str) -> None:
    missing = sorted(field for field in fields if field not in mapping)
    if missing:
        raise ContentPackError(f"{path} missing fields: {missing}")


def validate_content_pack(pack: dict[str, Any]) -> dict[str, Any]:
    _require(
        pack,
        {
            "pack_id",
            "pack_version",
            "source",
            "timezone",
            "start_sim_time",
            "evaluation_cutoff_sim_time",
            "organization",
            "actors",
            "transcript",
            "policy",
            "action_items",
        },
        "pack",
    )
    start = parse_time(pack["start_sim_time"])
    cutoff = parse_time(pack["evaluation_cutoff_sim_time"])
    if cutoff <= start:
        raise ContentPackError("evaluation_cutoff_sim_time must be after start_sim_time")

    source = pack["source"]
    _require(source, {"type", "license"}, "source")
    if source["type"] not in ("project_fixture", "external_subset"):
        raise ContentPackError("source.type must be project_fixture or external_subset")
    if source["type"] == "external_subset":
        _require(
            source,
            {"upstream_url", "retrieved_at", "upstream_sample_ids", "annotator"},
            "source",
        )

    actor_ids: set[str] = set()
    role_counts = {"COORDINATOR": 0, "PARTICIPANT": 0}
    role_aliases = {
        "COORDINATOR": "COORDINATOR",
        "AGGREGATOR": "COORDINATOR",
        "PARTICIPANT": "PARTICIPANT",
        "ACTION_OWNER": "PARTICIPANT",
    }
    actor_roles: dict[str, set[str]] = {}
    for index, actor in enumerate(pack["actors"]):
        _require(actor, {"actor_id", "display_name", "roles"}, f"actors[{index}]")
        actor_id = actor["actor_id"]
        if actor_id in actor_ids:
            raise ContentPackError(f"duplicate actor_id: {actor_id}")
        actor_ids.add(actor_id)
        roles = set(actor["roles"])
        unknown = roles - set(role_aliases)
        if unknown:
            raise ContentPackError(f"actor {actor_id} has unknown roles: {sorted(unknown)}")
        normalized_roles = {role_aliases[role] for role in roles}
        actor_roles[actor_id] = normalized_roles
        for role in roles:
            role_counts[role_aliases[role]] += 1
    if role_counts != {"COORDINATOR": 1, "PARTICIPANT": 3}:
        raise ContentPackError(f"P0 actor role counts are invalid: {role_counts}")

    if not pack["transcript"]:
        raise ContentPackError("transcript must not be empty")
    for index, line in enumerate(pack["transcript"]):
        _require(line, {"speaker", "text"}, f"transcript[{index}]")

    policy_fields = {
        "confirmation_timeout_hours",
        "check_in_lead_hours",
        "silence_window_hours",
        "inquiry_cooldown_hours",
        "signal_default_ttl_hours",
        "l2_wait_hours",
        "daily_touch_budget",
        "progress_window_hours",
        "outbox_max_attempts",
    }
    _require(pack["policy"], policy_fields, "policy")
    if any(pack["policy"][field] <= 0 for field in policy_fields):
        raise ContentPackError("all P0 policy values must be positive")

    if len(pack["action_items"]) != 4:
        raise ContentPackError("P0 requires exactly four action_items")
    action_ids: set[str] = set()
    deliverable_keys: set[str] = set()
    behaviors: set[str] = set()
    for index, item in enumerate(pack["action_items"]):
        _require(
            item,
            {
                "action_item_id",
                "title",
                "deliverable_key",
                "owner_actor_id",
                "team_required_by_sim_time",
                "source_span",
                "work_requirements",
                "management_review_policy",
                "required_fields",
                "behavior",
            },
            f"action_items[{index}]",
        )
        action_id = item["action_item_id"]
        deliverable_key = item["deliverable_key"]
        owner = item["owner_actor_id"]
        if action_id in action_ids:
            raise ContentPackError(f"duplicate action_item_id: {action_id}")
        if deliverable_key in deliverable_keys:
            raise ContentPackError(f"duplicate deliverable_key: {deliverable_key}")
        # A meeting extraction is a proposal, not an assignment.  P0's primary
        # path deliberately keeps the owner empty until an attendee claims the
        # coordinator-reviewed task.  Non-null owners remain supported for the
        # legacy confirmation compatibility path, but must still be attendees.
        if owner is not None and (
            owner not in actor_roles or "PARTICIPANT" not in actor_roles[owner]
        ):
            raise ContentPackError(f"action owner is not a PARTICIPANT: {owner}")
        team_required_by = parse_time(item["team_required_by_sim_time"])
        if not start < team_required_by <= cutoff:
            raise ContentPackError(
                f"team required time outside evaluation window: {action_id}"
            )
        if not str(item["work_requirements"]).strip():
            raise ContentPackError(f"work requirements are empty: {action_id}")
        if not str(item["management_review_policy"]).strip():
            raise ContentPackError(
                f"management review policy is empty: {action_id}"
            )
        if not item["required_fields"] or len(item["required_fields"]) != len(
            set(item["required_fields"])
        ):
            raise ContentPackError(f"invalid required_fields: {action_id}")
        action_ids.add(action_id)
        deliverable_keys.add(deliverable_key)
        item_behaviors = (
            item["behavior"]
            if isinstance(item["behavior"], list)
            else [item["behavior"]]
        )
        if not item_behaviors or any(
            not isinstance(value, str) or not value.strip()
            for value in item_behaviors
        ):
            raise ContentPackError(f"invalid behavior labels: {action_id}")
        behaviors.update(item_behaviors)
    expected_behaviors = {
        "NORMAL",
        "SILENT_IN_CHECK_WINDOW",
        "VERSION_REFRESH",
        "INVALID_THEN_FIX",
        "REQUEST_HELP",
    }
    if behaviors != expected_behaviors:
        raise ContentPackError(
            f"P0 behaviors must be {sorted(expected_behaviors)}, got {sorted(behaviors)}"
        )
    return pack
