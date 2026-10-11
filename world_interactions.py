"""Phase 3C world operations. Pure proposal application; the turn engine commits.

See WORLD_INTERACTIONS.md for the operation contract and travel assumptions.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any


ID = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
CONDITIONS = {"occupied", "damaged", "recovering", "prosperous", "under_siege",
              "unsafe", "safe", "plague", "flooded", "burned"}
NPC_STATUSES = {"active", "injured", "missing", "imprisoned", "dead"}
RELATIONS = {"hostile", "wary", "neutral", "friendly", "trusting"}
QUEST_STATUSES = {"available", "active", "completed", "failed", "abandoned"}
OP_FIELDS = {
    "travel": ({"destination_burg_id", "mode", "duration_days"},
               {"border_strategy", "route_justification", "ability_id"}),
    "npc_create": ({"npc_id", "name", "role", "description", "motivation", "location_burg_id"}, {"faction_id"}),
    "npc_relationship_update": ({"npc_id", "target_id", "relation"}, set()),
    "npc_status_update": ({"npc_id", "status"}, set()),
    "quest_create": ({"quest_id", "title", "description", "giver_npc_id", "location_burg_id", "objectives"}, set()),
    "quest_activate": ({"quest_id"}, set()),
    "quest_progress": ({"quest_id", "objective_index"}, set()),
    "quest_complete": ({"quest_id"}, set()),
    "quest_fail": ({"quest_id"}, set()),
    "quest_abandon": ({"quest_id"}, set()),
    "world_event": ({"text", "status", "visibility"}, {"burg_ids", "state_ids"}),
    "settlement_condition_update": ({"burg_id", "condition", "status", "description"},
                                    {"controller_state_id", "controller_faction_id"}),
    "faction_update": ({"faction_id", "relationship"}, {"influence_burg_id"}),
}


class WorldOperationError(ValueError):
    """Rejected operation. Inputs remain untouched; `index` is zero based."""

    def __init__(self, index: int, message: str):
        self.index = index
        super().__init__(f"operation[{index}]: {message}")


@dataclass(frozen=True)
class WorldOperationResult:
    world: dict
    notes: dict
    gamestate: dict
    events: list[dict]
    audio_cues: list[dict]


def _fail(index: int, message: str):
    raise WorldOperationError(index, message)


def _text(value: Any, field: str, index: int, limit: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _fail(index, f"{field} must be nonempty text of at most {limit} characters")
    return value.strip()


def _slug(value: Any, field: str, index: int) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        _fail(index, f"{field} must be a snake_case ID")
    return value


def _int(value: Any, field: str, index: int) -> int:
    if type(value) is not int or value < 0:
        _fail(index, f"{field} must be a nonnegative integer ID")
    return value


def _entity(world: dict, collection: str, identifier: Any, index: int) -> dict:
    identifier = _int(identifier, f"{collection} ID", index)
    values = world["pack"].get(collection)
    if not isinstance(values, list):
        _fail(index, f"world pack.{collection} is missing")
    found = next((r for r in values if isinstance(r, dict) and r.get("i") == identifier), None)
    if found is None:
        _fail(index, f"unknown {collection} ID {identifier}")
    return found


def _npc(gs: dict, identifier: Any, index: int) -> dict:
    identifier = _slug(identifier, "npc_id", index)
    if identifier not in gs["npcs"]:
        _fail(index, f"unknown NPC {identifier}")
    return gs["npcs"][identifier]


def _quest(gs: dict, identifier: Any, index: int) -> dict:
    identifier = _slug(identifier, "quest_id", index)
    if identifier not in gs["quests"]:
        _fail(index, f"unknown quest {identifier}")
    return gs["quests"][identifier]


def _event(op: dict, kind: str, **details) -> dict:
    return {"id": op["id"], "type": kind, **details}


def _history(record: dict, op: dict, change: str):
    record.setdefault("interaction_history", []).append({"operation_id": op["id"],
                                                          "change": change, "reason": op.get("reason", "")})


def _travel(world: dict, gs: dict, op: dict, coastlines: dict | None, index: int):
    campaign = gs.get("campaign")
    if not isinstance(campaign, dict):
        _fail(index, "campaign is not initialized")
    location = campaign.get("current_location") or campaign.get("starting_location")
    if not isinstance(location, dict) or "burg_id" not in location:
        _fail(index, "current settlement is unknown")
    start = _entity(world, "burgs", location["burg_id"], index)
    end = _entity(world, "burgs", op["destination_burg_id"], index)
    if start["i"] == end["i"]:
        _fail(index, "destination is already the current settlement")
    mode = op["mode"]
    if not isinstance(mode, str) or mode not in ("walk", "ride", "ship", "teleport"):
        _fail(index, "mode must be walk, ride, ship, or teleport")
    duration = op["duration_days"]
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
        _fail(index, "duration_days must be finite and nonnegative")
    dx, dy = end["x"] - start["x"], end["y"] - start["y"]
    distance = math.hypot(dx, dy)
    src_feature, dst_feature = start["feature"], end["feature"]
    crossing = src_feature != dst_feature
    if mode == "teleport":
        ability = _slug(op.get("ability_id"), "ability_id", index)
        player = gs.get("player") or {}
        abilities = player.get("abilities", []) if isinstance(player, dict) else []
        known = isinstance(abilities, list) and any(ability == (a.get("id") if isinstance(a, dict) else a) for a in abilities)
        if not known:
            _fail(index, "teleport requires an established player ability")
        route_status = "ability"
        minimum = 0
    else:
        if coastlines is None:
            _fail(index, "coastlines.json data is required to verify travel geography")
        if str(coastlines.get("seed")) != str(world["metadata"].get("seed")) or (
            coastlines.get("map_width"), coastlines.get("map_height")) != (
            world["metadata"].get("width"), world["metadata"].get("height")):
            _fail(index, "coastlines do not match the world")
        try:
            from shapely.geometry import LineString, Point, shape
            lands = {e["feature_id"]: shape(e["geometry"]) for e in coastlines["landmasses"]}
            direct = LineString([(start["x"], start["y"]), (end["x"], end["y"])])
            if src_feature not in lands or dst_feature not in lands:
                _fail(index, "settlement land feature missing from coastlines")
            if not lands[src_feature].covers(Point(start["x"], start["y"])) or not lands[dst_feature].covers(Point(end["x"], end["y"])):
                _fail(index, "settlement is outside its authoritative land feature")
            land_corridor = not crossing and lands[src_feature].buffer(1e-7).covers(direct)
        except Exception as error:
            if isinstance(error, WorldOperationError):
                raise
            _fail(index, f"invalid coastline geometry: {error}")
        if mode in ("walk", "ride") and not land_corridor:
            _fail(index, "direct land path crosses water or another land feature; propose ship travel or an established route")
        if mode == "ship":
            if not start.get("port") or not end.get("port"):
                _fail(index, "ship travel requires ports at both settlements")
            if start["port"] != end["port"]:
                _fail(index, "ports reference different water bodies; a connecting passage is unverified")
            if not any(e.get("feature_id") == start["port"] for e in coastlines.get("water_bodies", [])):
                _fail(index, "port water body is missing from coastlines")
            if land_corridor:
                _fail(index, "ship travel has no verified water crossing")
            _text(op.get("route_justification"), "route_justification", index, 500)
            route_status = "water crossing; service and route supplied by narrative, not verified by map"
        else:
            route_status = "direct land corridor; road not verified"
        scale = world["metadata"].get("distanceScale")
        unit = world["metadata"].get("distanceUnit")
        if type(scale) in (int, float) and math.isfinite(scale) and scale > 0 and unit in ("mi", "km"):
            measured = distance * scale
            per_day = {"walk": 20, "ride": 40, "ship": 80}[mode] * (1.60934 if unit == "km" else 1)
        else:
            measured = distance
            per_day = {"walk": 5, "ride": 10, "ship": 20}[mode]
            unit = "map units"
        minimum = 0 if distance <= 2 else math.ceil(max(.25, measured / per_day) * 4) / 4
        if duration + 1e-9 < minimum:
            _fail(index, f"travel duration too short; minimum {minimum:g} day(s) by {mode}")
    border = start["state"] != end["state"]
    if border and mode != "teleport":
        if op.get("border_strategy") not in ("permit", "covert"):
            _fail(index, "crossing a political border requires border_strategy permit or covert")
        _text(op.get("route_justification"), "route_justification", index, 500)
    state = _entity(world, "states", end["state"], index)
    campaign["current_location"] = {"burg_id": end["i"], "name": end.get("name"),
                                    "state_id": end["state"], "state_name": state.get("name")}
    return _event(op, "travel", from_burg_id=start["i"], to_burg_id=end["i"],
                  distance_map_units=round(distance, 2), duration_days=duration,
                  minimum_duration_days=minimum, mode=mode, border_crossing=border,
                  route_status=route_status), ([{"kind": "ambience", "cue": "harbor"}] if end.get("port") else [])


def _apply(world: dict, notes: dict, gs: dict, op: dict, coastlines: dict | None, index: int):
    kind = op["type"]
    if kind == "travel":
        return _travel(world, gs, op, coastlines, index)
    if kind == "npc_create":
        identifier = _slug(op["npc_id"], "npc_id", index)
        if identifier in gs["npcs"]:
            _fail(index, f"NPC {identifier} already exists")
        burg = _entity(world, "burgs", op["location_burg_id"], index)
        npc = {"id": identifier, "name": _text(op["name"], "name", index, 100),
               "role": _text(op["role"], "role", index, 150),
               "description": _text(op["description"], "description", index),
               "motivation": _text(op["motivation"], "motivation", index),
               "location_burg_id": burg["i"], "state_id": burg["state"],
               "relationships": [], "status": "active", "interaction_history": []}
        if "faction_id" in op:
            faction = _slug(op["faction_id"], "faction_id", index)
            if faction not in world.get("factions", {}):
                _fail(index, f"unknown faction {faction}")
            npc["faction"] = faction
        gs["npcs"][identifier] = npc
        return _event(op, kind, npc_id=identifier), []
    if kind == "npc_relationship_update":
        npc = _npc(gs, op["npc_id"], index)
        target = op["target_id"]
        relation = op["relation"]
        if not isinstance(relation, str) or relation not in RELATIONS:
            _fail(index, "unsupported relationship")
        if target == "player":
            npc["player_relationship"] = relation
        else:
            _npc(gs, target, index)
            if target == op["npc_id"]:
                _fail(index, "NPC cannot relate to itself")
            relationships = npc.setdefault("relationships", [])
            entry = next((r for r in relationships if r.get("npc_id") == target), None)
            if entry:
                entry["relation"] = relation
            else:
                relationships.append({"npc_id": target, "relation": relation})
        _history(npc, op, f"relationship with {target}: {relation}")
        return _event(op, kind, npc_id=op["npc_id"], target_id=target, relation=relation), []
    if kind == "npc_status_update":
        npc = _npc(gs, op["npc_id"], index)
        status = op["status"]
        if not isinstance(status, str) or status not in NPC_STATUSES:
            _fail(index, "unsupported NPC status")
        previous = npc.get("status", "active")
        if previous == "dead" or previous == status:
            _fail(index, "NPC status transition is invalid")
        npc["status"] = status
        _history(npc, op, f"status: {previous} -> {status}")
        return _event(op, kind, npc_id=op["npc_id"], status=status), []
    if kind == "quest_create":
        identifier = _slug(op["quest_id"], "quest_id", index)
        if identifier in gs["quests"]:
            _fail(index, f"quest {identifier} already exists")
        _npc(gs, op["giver_npc_id"], index)
        _entity(world, "burgs", op["location_burg_id"], index)
        objectives = op["objectives"]
        if not isinstance(objectives, list) or not 1 <= len(objectives) <= 20:
            _fail(index, "objectives must have 1 to 20 entries")
        objectives = [_text(item, "objective", index, 300) for item in objectives]
        gs["quests"][identifier] = {"id": identifier, "title": _text(op["title"], "title", index, 150),
                                    "description": _text(op["description"], "description", index),
                                    "giver_npc_id": op["giver_npc_id"],
                                    "location_burg_id": op["location_burg_id"],
                                    "objectives": objectives, "completed_objectives": [],
                                    "status": "available", "history": []}
        return _event(op, kind, quest_id=identifier), []
    if kind.startswith("quest_"):
        quest = _quest(gs, op["quest_id"], index)
        status = quest.get("status", "available")
        if not isinstance(status, str) or status not in QUEST_STATUSES:
            _fail(index, "quest has unsupported status")
        if kind == "quest_activate" and status == "available":
            quest["status"] = "active"
        elif kind == "quest_progress" and status == "active":
            objective = _int(op["objective_index"], "objective_index", index)
            if objective >= len(quest["objectives"]) or objective in quest.get("completed_objectives", []):
                _fail(index, "objective is missing or already complete")
            quest.setdefault("completed_objectives", []).append(objective)
        elif kind == "quest_complete" and status == "active":
            if set(quest.get("completed_objectives", [])) != set(range(len(quest["objectives"]))):
                _fail(index, "all quest objectives must be complete first")
            quest["status"] = "completed"
        elif kind == "quest_fail" and status in ("available", "active"):
            quest["status"] = "failed"
        elif kind == "quest_abandon" and status in ("available", "active"):
            quest["status"] = "abandoned"
        else:
            _fail(index, f"cannot apply {kind} to a {status} quest")
        quest.setdefault("history", []).append({"operation_id": op["id"], "type": kind,
                                                "reason": op.get("reason", "")})
        cue = [{"kind": "sfx", "cue": "quest_complete"}] if kind == "quest_complete" else []
        return _event(op, kind, quest_id=op["quest_id"], status=quest["status"]), cue
    if kind == "settlement_condition_update":
        burg = _entity(world, "burgs", op["burg_id"], index)
        condition, status = op["condition"], op["status"]
        if not isinstance(condition, str) or condition not in CONDITIONS or status not in ("active", "resolved"):
            _fail(index, "unsupported settlement condition or status")
        conditions = burg.setdefault("campaign_conditions", {})
        previous = conditions.get(condition)
        if status == "resolved" and (not previous or previous["status"] != "active"):
            _fail(index, "cannot resolve a condition that is not active")
        if previous and previous["status"] == status:
            _fail(index, "settlement condition is already in that state")
        description = _text(op["description"], "description", index)
        previous_control = deepcopy(burg.get("campaign_political_control"))
        conditions[condition] = {"status": status, "description": description, "operation_id": op["id"]}
        if "controller_state_id" in op or "controller_faction_id" in op:
            if "controller_state_id" in op and "controller_faction_id" in op:
                _fail(index, "choose one political controller")
            if "controller_state_id" in op:
                controller = {"state_id": _entity(world, "states", op["controller_state_id"], index)["i"]}
            else:
                faction = _slug(op["controller_faction_id"], "controller_faction_id", index)
                if faction not in world.get("factions", {}):
                    _fail(index, f"unknown faction {faction}")
                controller = {"faction_id": faction}
            burg["campaign_political_control"] = controller
        world.setdefault("campaign_changelog", []).append({"operation_id": op["id"], "entity_type": "burg",
                                                            "entity_id": burg["i"], "condition": condition,
                                                            "previous_condition": deepcopy(previous),
                                                            "previous_political_control": previous_control,
                                                            "political_control": deepcopy(burg.get("campaign_political_control")),
                                                            "status": status, "reason": op.get("reason", "")})
        return _event(op, kind, burg_id=burg["i"], condition=condition, status=status), (
            [{"kind": "sfx", "cue": "danger"}] if status == "active" and condition in ("under_siege", "burned") else [])
    if kind == "faction_update":
        identifier = _slug(op["faction_id"], "faction_id", index)
        faction = world.get("factions", {}).get(identifier)
        if not isinstance(faction, dict):
            _fail(index, f"unknown faction {identifier}")
        relation = op["relationship"]
        if not isinstance(relation, str) or relation not in RELATIONS:
            _fail(index, "unsupported faction relationship")
        previous_relationship = faction.get("player_relationship")
        faction["player_relationship"] = relation
        if "influence_burg_id" in op:
            burg = _entity(world, "burgs", op["influence_burg_id"], index)
            faction.setdefault("influence_burg_ids", [])
            if burg["i"] not in faction["influence_burg_ids"]:
                faction["influence_burg_ids"].append(burg["i"])
        world.setdefault("campaign_changelog", []).append({"operation_id": op["id"], "entity_type": "faction",
                                                            "entity_id": identifier, "relationship": relation,
                                                            "previous_relationship": previous_relationship,
                                                            "reason": op.get("reason", "")})
        return _event(op, kind, faction_id=identifier, relationship=relation), []
    if kind == "world_event":
        status, visibility = op["status"], op["visibility"]
        if not isinstance(status, str) or status not in ("established", "planned", "rumor") or not isinstance(visibility, str) or visibility not in ("public", "dm_private"):
            _fail(index, "invalid event status or visibility")
        burg_ids = op.get("burg_ids", [])
        state_ids = op.get("state_ids", [])
        if not isinstance(burg_ids, list) or not isinstance(state_ids, list):
            _fail(index, "event references must be lists")
        for identifier in burg_ids:
            _entity(world, "burgs", identifier, index)
        for identifier in state_ids:
            _entity(world, "states", identifier, index)
        statement = _text(op["text"], "text", index)
        if status == "established" and visibility == "public":
            for secret in notes.get("secrets", []):
                if isinstance(secret, dict) and not secret.get("revealed") and isinstance(secret.get("text"), str):
                    if secret["text"].strip() and secret["text"].casefold() in statement.casefold():
                        _fail(index, "public event contains an unrevealed DM secret")
        entry = {"id": f"worldop_{op['id']}", "status": status, "visibility": visibility,
                 "text": statement, "turn": gs["campaign"].get("current_turn"),
                 "burg_ids": list(burg_ids), "state_ids": list(state_ids)}
        if status == "established" and visibility == "public":
            world.setdefault("world_events", []).append(deepcopy(entry))
        notes.setdefault("event_log", []).append(entry)
        if status == "established" and visibility == "public":
            return _event(op, kind, status=status, visibility=visibility, text=statement), []
        return _event(op, kind, status=status, visibility=visibility), []
    _fail(index, "unknown operation type")


def apply_world_operations(world: dict, notes: dict, gamestate: dict,
                           operations: list[dict], *, coastlines: dict | None = None) -> WorldOperationResult:
    """Validate and apply operations to copies. Never writes files or mutates inputs.

    Raises WorldOperationError at the first rejected operation. The caller owns
    turn numbering, player updates, and the final multi-file transaction.
    """
    if not isinstance(world, dict) or not isinstance(world.get("pack"), dict) or not isinstance(world.get("metadata"), dict):
        raise WorldOperationError(-1, "normalized world.json is required")
    if not isinstance(notes, dict) or not isinstance(gamestate, dict) or not isinstance(gamestate.get("campaign"), dict) or not isinstance(gamestate.get("npcs"), dict) or not isinstance(gamestate.get("quests"), dict):
        raise WorldOperationError(-1, "initialized notes.json and gamestate.json are required")
    if not isinstance(operations, list) or len(operations) > 50:
        raise WorldOperationError(-1, "operations must be a list of at most 50")
    new_world, new_notes, new_gs = deepcopy(world), deepcopy(notes), deepcopy(gamestate)
    receipts = new_gs.setdefault("world_operation_receipts", {})
    if not isinstance(receipts, dict):
        raise WorldOperationError(-1, "world_operation_receipts must be an object")
    if len(receipts) > 10000:
        raise WorldOperationError(-1, "world operation receipt limit exceeded")
    events, cues = [], []
    for index, op in enumerate(operations):
        if not isinstance(op, dict):
            _fail(index, "operation must be an object")
        kind = op.get("type")
        if not isinstance(kind, str) or kind not in OP_FIELDS:
            _fail(index, f"unknown operation type {kind!r}")
        required, optional = OP_FIELDS[kind]
        extra = set(op) - required - optional - {"type", "id", "reason"}
        missing = required - set(op)
        if missing or extra:
            _fail(index, f"missing fields {sorted(missing)}; unsupported fields {sorted(extra)}")
        identifier = _slug(op.get("id"), "id", index)
        if "reason" in op:
            _text(op["reason"], "reason", index, 500)
        try:
            raw = json.dumps(op, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            _fail(index, "operation must contain finite JSON values")
        if len(raw) > 8000:
            _fail(index, "operation exceeds 8000 characters")
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if identifier in receipts:
            if receipts[identifier] != digest:
                _fail(index, f"operation ID {identifier} was reused with different content")
            continue  # exact replay after a committed turn is harmless
        try:
            event, audio = _apply(new_world, new_notes, new_gs, op, coastlines, index)
        except (KeyError, TypeError, AttributeError, IndexError, OverflowError) as error:
            _fail(index, f"operation or campaign data is malformed ({type(error).__name__})")
        receipts[identifier] = digest
        events.append(event)
        cues.extend({"operation_id": identifier, **cue} for cue in audio)
    return WorldOperationResult(new_world, new_notes, new_gs, events, cues)
