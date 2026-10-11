"""Replaceable game/world service interfaces and an offline mock implementation."""

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from time import sleep
from typing import Callable, Protocol


Progress = Callable[[str], None]


class GameError(ValueError):
    """A short error suitable for display to the player."""


def check_cancelled(cancel: Event) -> None:
    if cancel.is_set():
        raise GameError("Operation cancelled.")


@dataclass
class WorldRef:
    name: str
    directory: str | None = None
    seed: str | None = None


@dataclass
class CampaignIntro:
    world: WorldRef
    text: str


@dataclass
class CharacterRevision:
    questions: list[str]
    adjustments: list[dict] = field(default_factory=list)


@dataclass
class GameSession:
    world: WorldRef
    introduction: str
    character_description: str
    character_name: str
    location: str
    inventory: list[str] = field(default_factory=list)
    quests: list[str] = field(default_factory=list)
    history: list[dict[str, str]] = field(default_factory=list)
    turn: int = 0
    character_narration: str = ""
    npcs: list[str] = field(default_factory=list)
    nearby: list[str] = field(default_factory=list)
    last_narration: str = ""
    audio_cues: dict = field(default_factory=dict)


def session_from_campaign(world_ref: WorldRef, introduction: str, description: str,
                          character_narration: str = "") -> GameSession:
    """Build the desktop view from committed campaign files, including on reload."""
    from safe_storage import read_json

    if not world_ref.directory:
        raise GameError("This campaign has no saved world directory.")
    directory = Path(world_ref.directory)
    world = read_json(directory / "world.json")
    state = read_json(directory / "gamestate.json")
    if not isinstance(world, dict) or not isinstance(state, dict) or state.get("phase") != "playing":
        raise GameError("This campaign has no accepted character to resume.")
    metadata = world.get("metadata")
    if not isinstance(metadata, dict) or (
        world_ref.seed is not None and str(metadata.get("seed")) != world_ref.seed):
        raise GameError("The saved world reference does not match this campaign.")
    player, campaign = state.get("player"), state.get("campaign")
    if not isinstance(player, dict) or not isinstance(campaign, dict):
        raise GameError("This campaign's character or location is unavailable.")
    name = player.get("name")
    if not isinstance(name, str) or not name.strip():
        raise GameError("This campaign's character name is invalid.")
    burgs = [burg for burg in world.get("pack", {}).get("burgs", []) if isinstance(burg, dict)]
    current = campaign.get("current_location") or campaign.get("starting_location") or {}
    current_id = current.get("burg_id") if isinstance(current, dict) else None
    place = next((burg for burg in burgs if burg.get("i") == current_id), None)
    location = (current.get("name") if isinstance(current, dict) else None) or (
        place.get("name") if place else None) or "Unknown settlement"
    inventory = []
    for item in player.get("inventory", []):
        if isinstance(item, str):
            inventory.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            quantity = item.get("quantity", 1)
            inventory.append(f"{quantity} × {item['name']}" if quantity != 1 else item["name"])
    quests = []
    for key, quest in state.get("quests", {}).items():
        if not isinstance(quest, dict) or quest.get("status") in ("completed", "failed", "abandoned"):
            continue
        progress = ""
        if isinstance(quest.get("objectives"), list):
            progress = f", {len(quest.get('completed_objectives', []))}/{len(quest['objectives'])} objectives"
        quests.append(f"{quest.get('title', key)} ({quest.get('status', 'available')}{progress})")
    npcs = []
    for key, npc in state.get("npcs", {}).items():
        if not isinstance(npc, dict):
            continue
        relation = npc.get("player_relationship")
        whereabouts = "here" if npc.get("location_burg_id") == current_id else "elsewhere"
        detail = f"{npc.get('role', 'local')}, {whereabouts}, {npc.get('status', 'active')}"
        if isinstance(relation, str):
            detail += f", {relation}"
        npcs.append(f"{npc.get('name', key)} — {detail}")
    nearby = []
    if place and isinstance(place.get("x"), (int, float)) and isinstance(place.get("y"), (int, float)):
        candidates = [burg for burg in burgs if burg.get("i") != current_id
                      and isinstance(burg.get("x"), (int, float))
                      and isinstance(burg.get("y"), (int, float))]
        candidates.sort(key=lambda burg: (burg["x"] - place["x"]) ** 2 + (burg["y"] - place["y"]) ** 2)
        nearby = [f"{burg.get('name', 'Settlement')} (ID {burg['i']})" for burg in candidates[:8]]
    history = []
    if introduction:
        history.append({"speaker": "Narrator", "text": introduction})
    if character_narration:
        history.append({"speaker": "Narrator", "text": character_narration})
    npc_names = {key: npc.get("name", key) for key, npc in state.get("npcs", {}).items()
                 if isinstance(npc, dict)}
    for event in state.get("history", []):
        if not isinstance(event, dict) or not isinstance(event.get("text"), str):
            continue
        kind = event.get("type")
        if kind == "player_action":
            history.append({"speaker": "You", "text": event["text"]})
        elif kind == "narration":
            history.append({"speaker": "Narrator", "text": event["text"]})
        elif kind == "dialogue":
            history.append({"speaker": str(npc_names.get(event.get("speaker_id"), event.get("speaker_id", "NPC"))),
                            "text": event["text"]})
    turn = campaign.get("current_turn")
    if type(turn) is not int or turn < 0:
        raise GameError("This campaign's turn counter is invalid.")
    return GameSession(world_ref, introduction, description, name, str(location),
                       inventory, quests, history, turn, character_narration,
                       npcs, nearby)


class WorldProvider(Protocol):
    def generate(self, export_root: Path, cancel: Event, progress: Progress) -> WorldRef: ...


class GameService(Protocol):
    def initialize_campaign(self, world: WorldRef, cancel: Event) -> CampaignIntro: ...
    def create_character(self, intro: CampaignIntro, description: str,
                         cancel: Event) -> GameSession | CharacterRevision: ...
    def perform_action(self, session: GameSession, action: str, cancel: Event) -> GameSession: ...


class AzgaarWorldProvider:
    """Thin adapter to the existing, fully validated Phase 1 function."""

    def generate(self, export_root: Path, cancel: Event, progress: Progress) -> WorldRef:
        from generate_world import generate_world
        from safe_storage import read_json

        original = generate_world(export_root=export_root, cancel_event=cancel, progress=progress)
        metadata = read_json(original.parent / "world.json")["metadata"]
        return WorldRef(name=metadata["mapName"], directory=str(original.parent),
                        seed=str(metadata["seed"]))


class SampleWorldProvider:
    """Offline demo path; no Azgaar download or Gemini key required."""

    def generate(self, export_root: Path, cancel: Event, progress: Progress) -> WorldRef:
        check_cancelled(cancel)
        progress("Preparing sample world...")
        sleep(0.15)
        check_cancelled(cancel)
        return WorldRef(name="Duskreach")


class MockGameService:
    def __init__(self, delay: float = 0.2):
        self.delay = delay

    def _pause(self, cancel: Event) -> None:
        check_cancelled(cancel)
        sleep(self.delay)
        check_cancelled(cancel)

    def initialize_campaign(self, world: WorldRef, cancel: Event) -> CampaignIntro:
        self._pause(cancel)
        return CampaignIntro(world, (
            f"In {world.name}, the roads have gone quiet. A lantern burns each night "
            "in the watchtower above the old trade road, though no keeper has lived "
            "there for years. At dusk, a letter bearing your name arrives with a "
            "single instruction: come before the light goes out.\n\n"
            "Who are you, and why would you answer?"))

    def create_character(self, intro: CampaignIntro, description: str, cancel: Event) -> GameSession:
        description = description.strip()
        if not 1 <= len(description) <= 4000:
            raise GameError("Describe your character in 1–4,000 characters.")
        self._pause(cancel)
        return GameSession(
            world=intro.world, introduction=intro.text,
            character_description=description, character_name="The Wayfarer",
            location="The Old Trade Road", inventory=["Traveler's pack", "A sealed letter"],
            quests=["Investigate the light in the watchtower"],
            history=[{"speaker": "Narrator", "text": intro.text},
                     {"speaker": "Narrator", "text": "You stand at the foot of the road. What do you do?"}],
        )

    def perform_action(self, session: GameSession, action: str, cancel: Event) -> GameSession:
        action = action.strip()
        if not 1 <= len(action) <= 2000:
            raise GameError("Enter an action in 1–2,000 characters.")
        self._pause(cancel)
        updated = deepcopy(session)
        updated.history.append({"speaker": "You", "text": action})
        if "look" in action.lower() or "inspect" in action.lower():
            narration = "The watchtower lantern glows beyond a row of dark pines. Fresh footprints cross the road."
        elif "letter" in action.lower():
            narration = "The seal bears a silver tower. Inside, the ink has blurred, but the warning is clear: trust no one at the gate."
        elif "tower" in action.lower() or "walk" in action.lower():
            updated.location = "Watchtower Gate"
            narration = "You reach the gate as the lantern flickers. Somewhere inside, a door closes softly."
        else:
            narration = "The evening answers with a cold wind. Your choice carries you a little farther into the mystery."
        updated.history.append({"speaker": "Narrator", "text": narration})
        return updated


class PhaseTwoCampaignService:
    """Connect the established campaign, character and turn services.

    The API key is deliberately supplied by a callable owned by the desktop
    credential service. It is never read from settings files or game saves by
    this adapter; the vault can use GEMINI_API_KEY if nothing was saved.
    """

    def __init__(self, api_key: Callable[[], str]):
        self._api_key = api_key

    def has_key(self) -> bool:
        return bool(self._api_key().strip())

    def initialize_campaign(self, world: WorldRef, cancel: Event, progress: Progress | None = None) -> CampaignIntro:
        check_cancelled(cancel)
        if not world.directory:
            raise GameError("This world has no saved directory for campaign initialization.")
        key = self._api_key().strip()
        if not key:
            raise GameError("Save a Gemini API key in Settings before starting a new game.")

        # Imported here so an offline demo can still launch if the optional
        # Gemini SDK has not yet been installed.
        from campaign_init import CampaignInitError, GeminiClient, initialize_campaign_from_directory

        client = None
        try:
            client = GeminiClient(api_key=key, progress=progress, cancel_event=cancel)
            check_cancelled(cancel)
            message = initialize_campaign_from_directory(
                Path(world.directory), client=client, max_attempts=2,
                progress=progress, cancel_event=cancel)
            check_cancelled(cancel)
        except CampaignInitError as error:
            # Do not surface a provider response or transport detail; either
            # may include information unsuitable for the status area.
            detail = str(error)
            if "GEMINI_API_KEY" in detail or "GOOGLE_API_KEY" in detail:
                raise GameError("Save a Gemini API key in Settings before starting a new game.") from error
            if detail == "Campaign initialization cancelled.":
                raise GameError("Operation cancelled.") from error
            if detail.startswith("Install the SDK:"):
                raise GameError("Gemini support is not installed. Run: python -m pip install -r requirements.txt") from error
            if detail.startswith("Gemini could not produce a valid campaign"):
                raise GameError("Gemini's campaign response did not pass validation after two proposals. Please retry.") from error
            if detail.startswith("Could not reach Gemini") or detail.startswith("Gemini API request failed"):
                raise GameError("The Gemini request failed or timed out. Check your connection and key, then retry.") from error
            raise GameError("Gemini could not initialize this campaign. Check your key and connection, then retry.") from error
        except ImportError as error:
            raise GameError("Gemini support is not installed. Run: python -m pip install -r requirements.txt") from error
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass  # Closing must not replace the operation's result.

        if not isinstance(message, str) or not message.strip():
            raise GameError("Gemini returned an empty campaign introduction. Please retry.")
        return CampaignIntro(world, message.strip())

    def create_character(self, intro: CampaignIntro, description: str, cancel: Event,
                         progress: Progress | None = None) -> GameSession | CharacterRevision:
        if not intro.world.directory:
            raise GameError("The production campaign directory is missing; character creation was not attempted.")
        from character_creation import ACCEPTED, NEEDS_REVISION, create_character

        key = self._api_key().strip()
        if not key:
            raise GameError("Save a Gemini API key in Settings to create a character.")
        result = create_character(description, Path(intro.world.directory), api_key=key,
                                  progress=progress, cancel_event=cancel)
        if result.status == NEEDS_REVISION:
            return CharacterRevision(result.questions, result.adjustments)
        if result.status != ACCEPTED:
            raise GameError(result.error or "Gemini could not create this character.")
        session = session_from_campaign(intro.world, intro.text, description,
                                        character_narration=result.narration)
        session.last_narration = result.narration
        return session

    def perform_action(self, session: GameSession, action: str, cancel: Event,
                       progress: Progress | None = None) -> GameSession:
        """Resolve an accepted campaign through Phase 3B without mock fallback."""
        directory = session.world.directory
        if not directory or not (Path(directory) / "gamestate.json").exists():
            raise GameError("The production campaign files are missing; no turn was submitted or saved.")
        from safe_storage import read_json
        state = read_json(Path(directory) / "gamestate.json")
        if state.get("phase") != "playing":
            raise GameError("This campaign is not ready for gameplay.")
        from campaign_init import CampaignInitError, GeminiClient
        from gameplay_turn import TurnError, process_turn

        key = self._api_key().strip()
        if not key:
            raise GameError("Save a Gemini API key in Settings to continue the campaign.")
        try:
            client = GeminiClient(api_key=key, progress=progress, cancel_event=cancel)
        except CampaignInitError as error:
            if str(error).startswith("Install the SDK:"):
                raise GameError("Gemini support is not installed. Run: python -m pip install -r requirements.txt") from error
            raise GameError("Gemini is unavailable for this campaign.") from error
        try:
            result = process_turn(directory, action, client=client, expected_turn=session.turn,
                                  cancel_event=cancel, progress=progress)
        except TurnError as error:
            raise GameError(str(error)) from error
        finally:
            try:
                client.close()
            except Exception:
                pass  # A committed turn stays successful if transport shutdown fails.
        updated = session_from_campaign(session.world, session.introduction,
                                        session.character_description, session.character_narration)
        updated.last_narration = result.narration
        updated.audio_cues = result.audio_cues
        return updated
