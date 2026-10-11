"""User paths, opt-in OS credential storage, and small JSON game saves."""

from dataclasses import asdict
import os
from pathlib import Path
from uuid import uuid4

import keyring
from keyring.errors import PasswordDeleteError
from PySide6.QtCore import QSettings, QStandardPaths

from safe_storage import prepare_root, read_json, safe_name, write_json
from desktop.services import GameSession, WorldRef, session_from_campaign


SERVICE_NAME = "SOLARRPG.FantasyRPG"


class Preferences:
    def __init__(self, settings: QSettings | None = None, base: Path | None = None):
        self.settings = settings or QSettings("SOLARRPG", "SOLARRPG")
        default = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        self.base = Path(base) if base is not None else Path(default or Path.home() / ".hackknight2026")

    def export_root(self) -> Path:
        return Path(self.settings.value("paths/exports", str(self.base / "worlds")))

    def save_root(self) -> Path:
        return Path(self.settings.value("paths/saves", str(self.base / "saves")))

    def audio_enabled(self) -> bool:
        return self.settings.value("audio/enabled", False, type=bool)

    def audio_volume(self) -> int:
        return int(self.settings.value("audio/volume", 70))

    def audio_voice_id(self) -> str:
        return str(self.settings.value("audio/voice_id", os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")))

    def update(self, export_root: str, save_root: str, audio_enabled: bool, volume: int, voice_id: str = "") -> None:
        if not export_root.strip() or not save_root.strip():
            raise ValueError("World export and save folders cannot be empty.")
        self.settings.setValue("paths/exports", str(Path(export_root).expanduser().absolute()))
        self.settings.setValue("paths/saves", str(Path(save_root).expanduser().absolute()))
        self.settings.setValue("audio/enabled", audio_enabled)
        self.settings.setValue("audio/volume", volume)
        if voice_id.strip():
            self.settings.setValue("audio/voice_id", voice_id.strip())
        self.settings.sync()


class ApiKeyVault:
    PROVIDERS = ("gemini", "elevenlabs")

    def __init__(self, settings: QSettings, backend=keyring):
        self.settings = settings
        self.backend = backend
        self.session: dict[str, str] = {provider: "" for provider in self.PROVIDERS}
        self.warnings: list[str] = []
        for provider in self.PROVIDERS:
            self.load_key(provider)

    def _check_provider(self, provider: str) -> None:
        if provider not in self.PROVIDERS:
            raise ValueError("Unknown API key provider")

    def load_key(self, provider: str) -> str:
        """Read a key from the OS credential manager into session memory only."""
        self._check_provider(provider)
        try:
            self.session[provider] = self.backend.get_password(SERVICE_NAME, provider) or ""
        except Exception:
            self.session[provider] = ""
            self.warnings.append(f"Could not read saved {provider} key from the OS credential manager.")
        return self.session[provider]

    def gemini_key(self) -> str:
        """Prefer the credential saved in Settings; use the environment if absent."""
        return self.session["gemini"].strip() or os.environ.get("GEMINI_API_KEY", "").strip()

    def save_key(self, provider: str, value: str) -> None:
        """Store a nonempty key in the OS credential manager."""
        self._check_provider(provider)
        value = value.strip()
        if not value:
            raise ValueError(f"Enter a {provider} API key before saving.")
        self.backend.set_password(SERVICE_NAME, provider, value)
        self.session[provider] = value
        self.settings.setValue(f"keys/remember_{provider}", True)
        self.settings.sync()

    def clear_key(self, provider: str) -> None:
        """Delete a persisted key and remove the session copy."""
        self._check_provider(provider)
        try:
            self.backend.delete_password(SERVICE_NAME, provider)
        except PasswordDeleteError:
            pass  # The credential manager already has no record.
        self.session[provider] = ""
        self.settings.setValue(f"keys/remember_{provider}", False)
        self.settings.sync()

    def is_saved(self, provider: str) -> bool:
        return self.settings.value(f"keys/remember_{provider}", False, type=bool)

    def set_key(self, provider: str, value: str, remember: bool) -> None:
        self._check_provider(provider)
        value = value.strip()
        self.session[provider] = value
        if remember:
            self.save_key(provider, value)
        else:
            # Session-only use deliberately does not touch a newly typed value.
            if self.is_saved(provider):
                self.clear_key(provider)
            else:
                self.settings.setValue(f"keys/remember_{provider}", False)
                self.settings.sync()


class GameSaveStore:
    def __init__(self, preferences: Preferences):
        self.preferences = preferences

    def save(self, session: GameSession, root: Path | None = None) -> Path:
        root = prepare_root(root if root is not None else self.preferences.save_root())
        destination = root / f"{safe_name(session.world.name)}_{uuid4().hex}.json"
        directory = session.world.directory
        if directory:
            if not (Path(directory) / "gamestate.json").is_file():
                raise ValueError("Campaign files are missing; the game was not saved.")
            # The world directory holds authoritative state. A save is a small
            # pointer plus the two narration texts absent from Phase 2/3A JSON.
            session_from_campaign(session.world, session.introduction,
                                  session.character_description, session.character_narration)
            write_json(destination, {"schema_version": 2, "world": asdict(session.world),
                                     "introduction": session.introduction,
                                     "character_description": session.character_description,
                                     "character_narration": session.character_narration})
        else:
            write_json(destination, {"schema_version": 1, "session": asdict(session)})
        return destination

    def load(self, path: Path) -> GameSession:
        data = read_json(path, 2 * 1024 * 1024)
        if not isinstance(data, dict):
            raise ValueError("This is not a supported game save.")
        if data.get("schema_version") == 2:
            world = data.get("world")
            if not isinstance(world, dict) or not isinstance(world.get("name"), str) or not world["name"]:
                raise ValueError("Game save has an invalid world reference.")
            if not isinstance(world.get("directory"), str) or not world["directory"]:
                raise ValueError("Game save has no campaign directory.")
            if world.get("seed") is not None and not isinstance(world["seed"], str):
                raise ValueError("Game save has an invalid world seed.")
            for key in ("introduction", "character_description", "character_narration"):
                if not isinstance(data.get(key), str) or len(data[key]) > 100_000:
                    raise ValueError(f"Game save has invalid {key}.")
            return session_from_campaign(
                WorldRef(world["name"], world["directory"], world.get("seed")),
                data["introduction"], data["character_description"], data["character_narration"])
        if data.get("schema_version") != 1:
            raise ValueError("This is not a supported game save.")
        item = data.get("session")
        if not isinstance(item, dict) or not isinstance(item.get("world"), dict):
            raise ValueError("Game save is missing its world information.")
        world = item["world"]
        if not isinstance(world.get("name"), str) or not world["name"]:
            raise ValueError("Game save has an invalid world name.")
        if any(world.get(key) is not None and not isinstance(world[key], str) for key in ("directory", "seed")):
            raise ValueError("Game save has invalid world references.")
        required_text = ("introduction", "character_description", "character_name", "location")
        if any(not isinstance(item.get(key), str) or len(item[key]) > 100_000 for key in required_text):
            raise ValueError("Game save has invalid character or story text.")
        for key in ("inventory", "quests"):
            values = item.get(key)
            if not isinstance(values, list) or len(values) > 300 or any(not isinstance(v, str) or len(v) > 2000 for v in values):
                raise ValueError(f"Game save has invalid {key}.")
        history = item.get("history")
        if not isinstance(history, list) or len(history) > 300 or any(
            not isinstance(entry, dict) or set(entry) != {"speaker", "text"}
            or not all(isinstance(entry[k], str) and len(entry[k]) <= 100_000 for k in ("speaker", "text"))
            for entry in history
        ):
            raise ValueError("Game save has invalid narration history.")
        turn = item.get("turn", 0)
        if type(turn) is not int or turn < 0:
            raise ValueError("Game save has an invalid turn counter.")
        session = GameSession(WorldRef(world["name"], world.get("directory"), world.get("seed")),
                           *(item[key] for key in required_text),
                           inventory=item["inventory"], quests=item["quests"], history=history, turn=turn)
        if session.world.directory and (Path(session.world.directory) / "gamestate.json").is_file():
            character_narration = item.get("character_narration", "")
            if not isinstance(character_narration, str):
                raise ValueError("Game save has invalid character narration.")
            return session_from_campaign(session.world, session.introduction,
                                         session.character_description,
                                         character_narration)
        return session
