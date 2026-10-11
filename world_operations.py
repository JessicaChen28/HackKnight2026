"""Phase 3B/3C boundary: validated requests for trusted world handlers.

The model proposes one of these operations. Phase 3C registers handlers; only
handler code may alter the detached world, gamestate, and notes dictionaries.
No model-supplied JSON path or arbitrary patch is accepted here.
"""

from dataclasses import dataclass, field
import math
from typing import Literal, Protocol


Direction = Literal["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]
OperationKind = Literal["travel_request", "npc_interaction", "quest_update", "world_event"]


@dataclass(frozen=True)
class TravelRequest:
    destination_burg_id: int | None = None
    direction: Direction | None = None
    distance: float | None = None  # map distance units; Phase 3C decides movement


@dataclass(frozen=True)
class NpcInteraction:
    npc_id: str
    intent: str


@dataclass(frozen=True)
class QuestUpdate:
    quest_id: str
    event: str


@dataclass(frozen=True)
class WorldEvent:
    description: str


WorldOperation = TravelRequest | NpcInteraction | QuestUpdate | WorldEvent


@dataclass(frozen=True)
class OperationOutcome:
    """Handler's player-safe summary and structured event metadata."""

    summary: str
    events: tuple[dict, ...] = field(default_factory=tuple)


class WorldHandler(Protocol):
    def __call__(self, operation: WorldOperation, world: dict, gamestate: dict,
                 notes: dict) -> OperationOutcome: ...


def operation_kind(operation: WorldOperation) -> OperationKind:
    return {
        TravelRequest: "travel_request", NpcInteraction: "npc_interaction",
        QuestUpdate: "quest_update", WorldEvent: "world_event",
    }[type(operation)]


def parse_operation(value: object) -> WorldOperation:
    """Strictly parse a model proposal; unknown fields and patches are rejected."""
    if not isinstance(value, dict):
        raise ValueError("World operation must be an object")
    kind = value.get("type")
    allowed = {
        "travel_request": {"type", "destination_burg_id", "direction", "distance"},
        "npc_interaction": {"type", "npc_id", "intent"},
        "quest_update": {"type", "quest_id", "event"},
        "world_event": {"type", "description"},
    }
    if not isinstance(kind, str) or kind not in allowed or set(value) - allowed[kind]:
        raise ValueError("Unknown world operation or unsupported fields")
    if kind == "travel_request":
        destination = value.get("destination_burg_id")
        direction, distance = value.get("direction"), value.get("distance")
        if destination is not None and (type(destination) is not int or destination < 0):
            raise ValueError("Travel destination must be an existing burg ID")
        directions = {"north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"}
        if direction is not None and (not isinstance(direction, str) or direction not in directions):
            raise ValueError("Travel direction is invalid")
        if distance is not None and (type(distance) not in (int, float) or not math.isfinite(distance) or not 0 < distance <= 1000):
            raise ValueError("Travel distance is invalid")
        if (destination is None and (direction is None or distance is None)) or (destination is not None and (direction is not None or distance is not None)):
            raise ValueError("Travel needs either a destination or a direction and distance")
        return TravelRequest(destination, direction, distance)
    if kind == "npc_interaction":
        npc_id, intent = value.get("npc_id"), value.get("intent")
        if not isinstance(npc_id, str) or not npc_id or not isinstance(intent, str) or not 0 < len(intent) <= 500:
            raise ValueError("NPC interaction needs an ID and short intent")
        return NpcInteraction(npc_id, intent)
    if kind == "quest_update":
        quest_id, event = value.get("quest_id"), value.get("event")
        if not isinstance(quest_id, str) or not quest_id or not isinstance(event, str) or not 0 < len(event) <= 500:
            raise ValueError("Quest update needs an ID and short event")
        return QuestUpdate(quest_id, event)
    description = value.get("description")
    if not isinstance(description, str) or not 0 < len(description) <= 500:
        raise ValueError("World event needs a short description")
    return WorldEvent(description)


class WorldDispatcher:
    def __init__(self):
        self._handlers: dict[str, WorldHandler] = {}

    def register(self, kind: OperationKind, handler: WorldHandler) -> None:
        if kind not in ("travel_request", "npc_interaction", "quest_update", "world_event"):
            raise ValueError("Unknown world operation kind")
        self._handlers[kind] = handler

    def available_kinds(self) -> tuple[str, ...]:
        return tuple(self._handlers)

    def dispatch(self, operation: WorldOperation, world: dict, gamestate: dict,
                 notes: dict) -> OperationOutcome:
        kind = operation_kind(operation)
        handler = self._handlers.get(kind)
        if handler is None:
            raise ValueError(f"No world handler registered for {kind}")
        result = handler(operation, world, gamestate, notes)
        if not isinstance(result, OperationOutcome) or not isinstance(result.summary, str) or not result.summary.strip():
            raise ValueError("World handler returned an invalid outcome")
        return result
