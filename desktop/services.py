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
class GameSession:
    world: WorldRef
    introduction: str
    character_description: str
    character_name: str
    location: str
    inventory: list[str] = field(default_factory=list)
    quests: list[str] = field(default_factory=list)
    history: list[dict[str, str]] = field(default_factory=list)


class WorldProvider(Protocol):
    def generate(self, export_root: Path, cancel: Event, progress: Progress) -> WorldRef: ...


class GameService(Protocol):
    def initialize_campaign(self, world: WorldRef, cancel: Event) -> CampaignIntro: ...
    def create_character(self, intro: CampaignIntro, description: str, cancel: Event) -> GameSession: ...
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
    """Use Phase 2A for the opening, while later gameplay remains a mock.

    The API key is deliberately supplied by a callable owned by the desktop
    credential service. It is never read from settings files or game saves by
    this adapter; the vault can use GEMINI_API_KEY if nothing was saved.
    """

    def __init__(self, api_key: Callable[[], str], placeholder: GameService | None = None):
        self._api_key = api_key
        self._placeholder = placeholder or MockGameService()

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

    def create_character(self, intro: CampaignIntro, description: str, cancel: Event) -> GameSession:
        """Placeholder until the Phase 2 character-creation service is ready."""
        return self._placeholder.create_character(intro, description, cancel)

    def perform_action(self, session: GameSession, action: str, cancel: Event) -> GameSession:
        """Placeholder until the Phase 2 gameplay service is ready."""
        return self._placeholder.perform_action(session, action, cancel)
