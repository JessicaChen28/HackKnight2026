"""Phase 3B: validate and commit one Gemini-authored gameplay turn.

The authoritative world.json, gamestate.json and notes.json live together in a
campaign directory. World effects are delegated to registered Phase 3C handlers.
No Qt or ElevenLabs code runs here.
"""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from threading import Event
from typing import Callable

from campaign_init import (CampaignInitError, GeminiClient, build_world_brief,
                           parse_model_json, save_all)
from safe_storage import checked_path, read_json
from world_operations import WorldDispatcher, parse_operation, operation_kind


class TurnError(ValueError):
    """A turn was rejected; authoritative files have not been changed."""


@dataclass(frozen=True)
class TurnResult:
    turn: int
    narration: str
    dialogue: tuple[dict, ...]
    events: tuple[dict, ...]
    audio_cues: dict
    status: str
    player: dict
    location: str | None
    quests: tuple[str, ...]


SYSTEM_PROMPT = """You are the Dungeon Master for a single-player fantasy RPG.
Resolve the player's attempted action against the authoritative world, character,
NPCs, established lore and recent events. An attempted outcome is not a fact:
do not automatically grant attacks, persuasion, travel or implausible powers.
Preserve player agency, continuity, existing IDs and established facts.
Return exactly one JSON object with this shape:
{
  "narration": "What the player experiences after this attempt",
  "dialogue": [{"speaker_id": "existing NPC key", "text": "spoken words"}],
  "player_changes": {"health_delta": 0, "inventory_add": [], "inventory_remove": [],
                     "conditions_add": [], "conditions_remove": []},
  "operations": [],
  "dm_note": "Private continuity note, or empty string",
  "audio_cues": {"sfx": [], "ambience": null},
  "status": "continue"
}
Optional keys may be omitted except narration. Allowed operations and their
typed fields are listed in the user context. Only propose an operation when
its handler is available. Do not invent JSON patches, new NPC IDs, or world
facts that need an unavailable handler. Audio cues are descriptions only.
Never include private DM notes or secrets in narration or dialogue."""


def _check_cancelled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise TurnError("Turn cancelled; no state was saved.")


@contextmanager
def _turn_lock(directory: Path):
    """A nonblocking process lock held from snapshot read through commit."""
    path = checked_path(directory / ".turn.lock", directory)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    locked = False
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as error:
            raise TurnError("A turn is already processing for this campaign.") from error
        yield
    finally:
        if locked:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _load_campaign(directory: Path) -> tuple[dict, dict, dict]:
    values = tuple(read_json(directory / name) for name in ("world.json", "gamestate.json", "notes.json"))
    if any(not isinstance(value, dict) for value in values):
        raise TurnError("Campaign files must contain JSON objects.")
    return values


def _text(value: object, label: str, maximum: int = 4000, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise TurnError(f"{label} must be valid text of at most {maximum} characters.")
    return value.strip()


def _string_list(value: object, label: str, *, maximum: int = 20, item_limit: int = 200) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise TurnError(f"{label} must be a short list.")
    items = [_text(item, label, item_limit) for item in value]
    if len(set(items)) != len(items):
        raise TurnError(f"{label} contains duplicates.")
    return items


def _prompt(world: dict, gamestate: dict, notes: dict, action: str,
            available_operations: tuple[str, ...]) -> str:
    brief = build_world_brief(world, max_burgs=60)
    state_brief = {key: gamestate.get(key) for key in ("phase", "player", "campaign", "npcs", "quests")}
    state_brief["recent_history"] = gamestate.get("history", [])[-12:]
    notes_brief = {key: notes.get(key) for key in ("campaign", "established_lore", "plot_threads", "secrets")}
    notes_brief["recent_events"] = notes.get("event_log", [])[-12:]
    notes_brief["recent_turn_notes"] = notes.get("turn_notes", [])[-8:]
    return ("AUTHORITATIVE WORLD BRIEF:\n" + json.dumps(brief, separators=(",", ":"))
            + "\nGAMESTATE:\n" + json.dumps(state_brief, separators=(",", ":"))
            + "\nDM NOTES:\n" + json.dumps(notes_brief, separators=(",", ":"))
            + "\nAVAILABLE WORLD OPERATIONS:\n" + json.dumps(available_operations)
            + "\nOperation parameters: travel_request uses destination_burg_id OR direction and distance; "
              "npc_interaction uses npc_id and intent; quest_update uses quest_id and event; "
              "world_event uses description. Return operations: [] for actions without a supported world effect."
            + "\nPLAYER ATTEMPT:\n" + action)


def _apply_player_changes(proposal: object, player: dict) -> None:
    if proposal is None:
        return
    if not isinstance(proposal, dict) or set(proposal) - {
        "health_delta", "inventory_add", "inventory_remove", "conditions_add", "conditions_remove"
    }:
        raise TurnError("Player changes contain unsupported fields.")
    delta = proposal.get("health_delta")
    if delta is not None:
        health = player.get("health")
        if type(delta) is not int or not -100 <= delta <= 100:
            raise TurnError("Health change is incompatible with this character.")
        if delta:
            if type(health) is not int:
                raise TurnError("Health change is incompatible with this character.")
            maximum = player.get("max_health")
            new_health = health + delta
            if new_health < 0 or (maximum is not None and (type(maximum) is not int or new_health > maximum)):
                raise TurnError("Health change exceeds the character's bounds.")
            player["health"] = new_health
    for field, add_key, remove_key in (
        ("inventory", "inventory_add", "inventory_remove"),
        ("conditions", "conditions_add", "conditions_remove"),
    ):
        if add_key not in proposal and remove_key not in proposal:
            continue
        current = player.get(field)
        if not isinstance(current, list) or any(not isinstance(item, str) for item in current):
            raise TurnError(f"Character has no compatible {field} list.")
        additions = _string_list(proposal.get(add_key, []), add_key)
        removals = _string_list(proposal.get(remove_key, []), remove_key)
        if set(additions) & set(removals) or any(item in current for item in additions) or any(item not in current for item in removals):
            raise TurnError(f"Invalid {field} addition or removal.")
        updated = [item for item in current if item not in removals] + additions
        if len(updated) > 200:
            raise TurnError(f"Character {field} exceeds the supported size.")
        player[field] = updated


def _validate_proposal(response: dict, world: dict, gamestate: dict, notes: dict,
                       dispatcher: WorldDispatcher, next_turn: int, action: str) -> tuple[dict, dict, dict, TurnResult]:
    allowed = {"narration", "dialogue", "player_changes", "operations", "dm_note", "audio_cues", "status"}
    if set(response) - allowed:
        raise TurnError("Turn response contains unsupported fields.")
    narration = _text(response.get("narration"), "Narration", 8000)
    status = response.get("status", "continue")
    if status not in ("continue", "campaign_complete"):
        raise TurnError("Turn status is invalid.")
    dialogue = response.get("dialogue", [])
    if not isinstance(dialogue, list) or len(dialogue) > 20:
        raise TurnError("Dialogue must be a short list.")
    clean_dialogue = []
    for entry in dialogue:
        if not isinstance(entry, dict) or set(entry) != {"speaker_id", "text"}:
            raise TurnError("Dialogue entry is invalid.")
        speaker = _text(entry["speaker_id"], "Dialogue speaker", 80)
        if speaker not in gamestate.get("npcs", {}):
            raise TurnError("Dialogue references an unknown NPC.")
        clean_dialogue.append({"speaker_id": speaker, "text": _text(entry["text"], "Dialogue", 2000)})
    cues = response.get("audio_cues", {})
    if not isinstance(cues, dict) or set(cues) - {"sfx", "ambience"}:
        raise TurnError("Audio cues are invalid.")
    sfx = _string_list(cues.get("sfx", []), "Sound effect", maximum=3, item_limit=200)
    ambience = cues.get("ambience")
    if ambience is not None:
        ambience = _text(ambience, "Ambience", 200)
    note = _text(response.get("dm_note", ""), "DM note", 2000, empty=True)
    operations = response.get("operations", [])
    if not isinstance(operations, list) or len(operations) > 8:
        raise TurnError("World operations must be a short list.")
    parsed_operations = [parse_operation(operation) for operation in operations]
    valid_burgs = {burg["i"] for burg in world.get("pack", {}).get("burgs", []) if isinstance(burg, dict)}
    for operation in parsed_operations:
        kind = operation_kind(operation)
        if kind not in dispatcher.available_kinds():
            raise TurnError(f"No world handler is available for {kind}.")
        if kind == "travel_request" and operation.destination_burg_id is not None and operation.destination_burg_id not in valid_burgs:
            raise TurnError("Travel references an unknown settlement.")
        if kind == "npc_interaction" and operation.npc_id not in gamestate.get("npcs", {}):
            raise TurnError("NPC interaction references an unknown NPC.")
        if kind == "quest_update" and operation.quest_id not in gamestate.get("quests", {}):
            raise TurnError("Quest update references an unknown quest.")
    next_world, next_state, next_notes = deepcopy((world, gamestate, notes))
    _apply_player_changes(response.get("player_changes"), next_state["player"])
    events = []
    for operation in parsed_operations:
        outcome = dispatcher.dispatch(operation, next_world, next_state, next_notes)
        events.append({"type": operation_kind(operation), "request": asdict(operation),
                       "summary": outcome.summary, "details": list(outcome.events)})
    campaign = next_state["campaign"]
    campaign["current_turn"] = next_turn
    history = next_state.setdefault("history", [])
    if not isinstance(history, list):
        raise TurnError("Campaign history is invalid.")
    history.extend(({"turn": next_turn, "type": "player_action", "text": action},
                    {"turn": next_turn, "type": "narration", "text": narration}))
    history.extend({"turn": next_turn, "type": "dialogue", **entry} for entry in clean_dialogue)
    if note:
        turn_notes = next_notes.setdefault("turn_notes", [])
        if not isinstance(turn_notes, list):
            raise TurnError("DM turn notes are invalid.")
        turn_notes.append({"turn": next_turn, "text": note})
    player = next_state["player"]
    location = player.get("location") or next_state.get("location")
    if location is not None and not isinstance(location, str):
        location = None
    quests = tuple(str(quest.get("title", key)) for key, quest in next_state.get("quests", {}).items()
                   if isinstance(quest, dict) and quest.get("status") != "completed")
    result = TurnResult(next_turn, narration, tuple(clean_dialogue), tuple(events),
                        {"sfx": sfx, "ambience": ambience}, status, deepcopy(player), location, quests)
    return next_world, next_state, next_notes, result


def process_turn(directory: str | Path, player_action: str, *, client=None,
                 dispatcher: WorldDispatcher | None = None, expected_turn: int | None = None,
                 max_proposals: int = 2, cancel_event: Event | None = None,
                 progress: Callable[[str], None] | None = None) -> TurnResult:
    """Process one action, commit validated files, then return audio-ready cues.

    Caller may pass a fake client with generate(system_prompt, turns). The
    default client uses the same bounded Gemini configuration as Phase 2.
    """
    action = _text(player_action, "Player action", 2000)
    if type(max_proposals) is not int or not 1 <= max_proposals <= 3:
        raise ValueError("max_proposals must be between 1 and 3")
    directory = checked_path(directory)
    if not directory.is_dir():
        raise TurnError("Campaign directory does not exist.")
    dispatcher = dispatcher or WorldDispatcher()
    own_client = client is None
    with _turn_lock(directory):
        _check_cancelled(cancel_event)
        world, gamestate, notes = _load_campaign(directory)
        if gamestate.get("phase") != "playing":
            raise TurnError("Campaign must be in the playing phase.")
        if not isinstance(gamestate.get("player"), dict) or not gamestate["player"]:
            raise TurnError("Campaign has no accepted character.")
        campaign = gamestate.get("campaign")
        if not isinstance(campaign, dict) or type(campaign.get("current_turn")) is not int or campaign["current_turn"] < 0:
            raise TurnError("Campaign turn counter is invalid.")
        current_turn = campaign["current_turn"]
        if expected_turn is not None and current_turn != expected_turn:
            raise TurnError("Stale turn: the campaign has advanced.")
        next_turn = current_turn + 1
        turns = [("user", _prompt(world, gamestate, notes, action, dispatcher.available_kinds()))]
        if own_client:
            client = GeminiClient(progress=progress, cancel_event=cancel_event)
        try:
            for attempt in range(1, max_proposals + 1):
                _check_cancelled(cancel_event)
                if progress:
                    progress(f"Resolving turn {next_turn}, proposal {attempt}/{max_proposals}...")
                try:
                    raw = client.generate(SYSTEM_PROMPT, turns)
                except CampaignInitError as error:
                    reason = str(error).lower()
                    if "quota" in reason or "billing" in reason:
                        raise TurnError("Gemini quota is exhausted; no state was saved.") from error
                    if "timeout" in reason or "request limit" in reason or "timed out" in reason:
                        raise TurnError("Gemini timed out; no state was saved.") from error
                    raise TurnError("Gemini could not resolve this turn; no state was saved.") from error
                except TimeoutError as error:
                    raise TurnError("Gemini timed out; no state was saved.") from error
                _check_cancelled(cancel_event)
                try:
                    proposal = parse_model_json(raw)
                    next_world, next_state, next_notes, result = _validate_proposal(
                        proposal, world, gamestate, notes, dispatcher, next_turn, action)
                except (ValueError, TypeError, KeyError) as error:
                    if attempt == max_proposals:
                        raise TurnError("Gemini returned an invalid turn after the allowed proposals; no state was saved.") from error
                    turns += [("model", raw), ("user", "The previous response failed validation: "
                                                   + str(error)[:300] + ". Return the complete JSON object again "
                                                   "using only available world operations and valid character state.")]
                    continue
                _check_cancelled(cancel_event)
                if _load_campaign(directory) != (world, gamestate, notes):
                    raise TurnError("Stale turn: campaign files changed during the request.")
                changed = {"gamestate.json": (next_state, False)}
                if next_notes != notes:
                    changed["notes.json"] = (next_notes, False)
                if next_world != world:
                    changed["world.json"] = (next_world, True)
                save_all(directory, changed)
                if progress:
                    progress(f"Turn {next_turn} saved.")
                return result
        finally:
            if own_client:
                try:
                    client.close()
                except Exception:
                    # Closing the transport must not turn a committed turn
                    # into an apparent failure that a player might retry.
                    pass
    raise TurnError("Turn could not be resolved.")
