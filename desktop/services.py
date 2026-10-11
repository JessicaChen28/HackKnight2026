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
