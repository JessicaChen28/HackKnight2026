"""Offline vertical-slice tests for character, turn, world, desktop and save handoff."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from desktop.audio import AudioError
from desktop.services import CampaignIntro, GameError, PhaseTwoCampaignService, WorldRef
from desktop.storage import ApiKeyVault, GameSaveStore, Preferences
from desktop.window import MainWindow
from safe_storage import read_json, write_json
from tests.test_character_creation import good as good_character
from tests.test_desktop import MemoryKeyring
from tests.test_world_interactions import campaign as world_campaign


class FakeClient:
    def __init__(self, *responses, delay=0):
        self.responses = list(responses)
        self.delay = delay

    def generate(self, system, turns):
        if self.delay:
            time.sleep(self.delay)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response)

    def close(self):
        pass


def turn(*operations, inventory_add=None, narration="The road opens ahead."):
    return {
        "narration": narration,
        "dialogue": [],
        "player_changes": {"inventory_add": inventory_add or []},
        "operations": list(operations),
        "audio_cues": {"sfx": ["A wooden gate creaks"], "ambience": None},
        "status": "continue",
    }


class PhaseThreeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / "world"
        self.directory.mkdir()
        world, notes, state, coastlines = world_campaign()
        state["phase"] = "character_creation"
        state["player"] = None
        state["campaign"]["current_turn"] = 0
        for name, value in (
            ("world.json", world), ("notes.json", notes),
            ("gamestate.json", state), ("coastlines.json", coastlines),
        ):
            write_json(self.directory / name, value)
        self.ref = WorldRef("Test", str(self.directory), "123")
        self.intro = CampaignIntro(self.ref, "A letter arrives at Harbor.")
        self.service = PhaseTwoCampaignService(lambda: "offline-test-key")

    def tearDown(self):
        self.temp.cleanup()

    def character(self):
        with patch("character_creation.GeminiClient", return_value=FakeClient(good_character())):
            return self.service.create_character(self.intro, "Kaelen seeks the lost ledger.", Event())

    def action(self, session, response):
        with patch("campaign_init.GeminiClient", return_value=FakeClient(response)):
            return self.service.perform_action(session, "I continue the journey.", Event())

    def test_character_travel_quest_npc_and_old_save_resume_latest_turn(self):
        session = self.character()
        self.assertEqual(session.character_name, "Kaelen")
        self.assertEqual(session.turn, 0)
        self.assertEqual(read_json(self.directory / "gamestate.json")["phase"], "playing")
        self.assertIn("Vera", session.npcs[0])

        session = self.action(session, turn(
            {"type": "quest_activate", "quest_id": "ledger"},
            {"type": "travel", "destination_burg_id": 2, "mode": "walk", "duration_days": 0.5},
            inventory_add=[{"name": "Weathered compass", "quantity": 1}],
        ))
        state = read_json(self.directory / "gamestate.json")
        self.assertEqual(session.turn, 1)
        self.assertEqual(state["campaign"]["current_location"]["burg_id"], 2)
        self.assertEqual(state["quests"]["ledger"]["status"], "active")
        self.assertIn("Weathered compass", session.inventory)
        self.assertEqual(len(state["world_operation_receipts"]), 2)
        self.assertEqual(session.location, "Hill")
        self.assertEqual(session.audio_cues["sfx"], ["A wooden gate creaks"])

        store = GameSaveStore(type("Prefs", (), {"save_root": lambda _: self.root / "saves"})())
        saved = store.save(session)
        self.assertEqual(read_json(saved)["schema_version"], 2)
        session = self.action(session, turn(
            {"type": "npc_relationship_update", "npc_id": "vera",
             "target_id": "player", "relation": "friendly"},
            {"type": "quest_progress", "quest_id": "ledger", "objective_index": 0},
            narration="Vera trusts you with the first clue.",
        ))
        resumed = store.load(saved)
        latest = read_json(self.directory / "gamestate.json")
        self.assertEqual(resumed.turn, 2)
        self.assertEqual(resumed.location, "Hill")
        self.assertEqual(latest["npcs"]["vera"]["player_relationship"], "friendly")
        self.assertEqual(latest["quests"]["ledger"]["completed_objectives"], [0])
        self.assertIn("Vera trusts you", resumed.history[-1]["text"])

    def test_failed_api_request_does_not_change_campaign(self):
        session = self.character()
        before = {name: (self.directory / name).read_bytes()
                  for name in ("world.json", "notes.json", "gamestate.json")}
        with patch("campaign_init.GeminiClient", return_value=FakeClient(TimeoutError("timed out"))):
            with self.assertRaisesRegex(GameError, "timed out"):
                self.service.perform_action(session, "Ask Vera", Event())
        self.assertEqual(before, {name: (self.directory / name).read_bytes() for name in before})

    def test_qt_character_turn_audio_failure_and_reload(self):
        settings = QSettings(str(self.root / "prefs.ini"), QSettings.Format.IniFormat)
        preferences = Preferences(settings=settings, base=self.root)
        preferences.update(str(self.root / "exports"), str(self.root / "saves"), False, 70)
        vault = ApiKeyVault(settings, backend=MemoryKeyring())
        class DirectoryProvider:
            def generate(inner, export_root, cancel, progress):
                progress("Using prepared campaign world.")
                return self.ref
        window = MainWindow(preferences=preferences, vault=vault, game_service=self.service,
                            world_provider=DirectoryProvider())
        window.show()
        try:
            with patch("campaign_init.GeminiClient", return_value=FakeClient()), \
                 patch("campaign_init.initialize_campaign_from_directory",
                       return_value=self.intro.text):
                window.menu.new_button.click()
                self._wait(lambda: window.stack.currentWidget() is window.creation
                           and window._thread is None)
            self.assertEqual(window.creation.introduction.toPlainText(), self.intro.text)
            preferences.update(str(self.root / "exports"), str(self.root / "saves"), True, 70)
            audio_paths = Mock()
            window.audio_service = Mock(
                api_key=lambda: "offline-audio-key",
                speak_narration=lambda text: self.root / "cached-narration.mp3",
                play_sound_effect=lambda text: self.root / "cached-effect.mp3",
            )
            window.audio_playback.play_sequence = audio_paths
            window.creation.description.setPlainText("Kaelen seeks the lost ledger.")
            with patch("character_creation.GeminiClient", return_value=FakeClient(good_character(), delay=0.1)):
                started = time.monotonic()
                window.creation.create_button.click()
                self.assertLess(time.monotonic() - started, 0.05)
                self._wait(lambda: window.session is not None and window._thread is None)
            self.assertEqual(window.session.character_name, "Kaelen")
            self._wait(lambda: audio_paths.call_count >= 1)
            self.assertTrue(window.game.send_button.isEnabled())

            window.game.action.setPlainText("I travel to Hill and ask about the ledger.")
            response = turn({"type": "quest_activate", "quest_id": "ledger"},
                            {"type": "travel", "destination_burg_id": 2,
                             "mode": "walk", "duration_days": 0.5})
            with patch("campaign_init.GeminiClient", return_value=FakeClient(response, delay=0.1)):
                window.game.send_button.click()
                self._wait(lambda: window.session is not None and window.session.turn == 1
                           and window._thread is None)
            self._wait(lambda: audio_paths.call_count >= 2)
            self.assertEqual(len(audio_paths.call_args.args[0]), 2)
            self.assertIn("Hill", window.game.location.text())
            self.assertIn("ledger", window.game.quests.item(0).text().lower())

            window.audio_service.speak_narration = Mock(side_effect=AudioError("offline audio failure"))
            with patch("campaign_init.GeminiClient",
                       return_value=FakeClient(turn({"type": "quest_progress",
                                                    "quest_id": "ledger", "objective_index": 0}))):
                window.game.action.setPlainText("I find the first clue.")
                window.game.send_button.click()
                self._wait(lambda: window.session is not None and window.session.turn == 2
                           and window._thread is None)
            self.assertEqual(read_json(self.directory / "gamestate.json")["campaign"]["current_turn"], 2)
            window.game.save_button.click()
            self._wait(lambda: window._thread is None and any(preferences.save_root().glob("*.json")))
            saved = next(preferences.save_root().glob("*.json"))
        finally:
            window.close()
            self.app.processEvents()

        reopened = MainWindow(preferences=preferences, vault=vault, game_service=self.service)
        reopened.show()
        try:
            reopened.load_game_from_path(saved)
            self._wait(lambda: reopened.session is not None and reopened._thread is None)
            self.assertEqual(reopened.session.turn, 2)
            self.assertEqual(reopened.session.location, "Hill")
            self.assertIs(reopened._active_game_service, self.service)
            with patch("campaign_init.GeminiClient",
                       return_value=FakeClient(turn(
                           {"type": "quest_progress", "quest_id": "ledger", "objective_index": 1},
                           {"type": "quest_complete", "quest_id": "ledger"},
                           narration="The ledger is recovered.",
                       ))):
                reopened.game.action.setPlainText("I return the ledger and complete the task.")
                reopened.game.send_button.click()
                self._wait(lambda: reopened.session is not None and reopened.session.turn == 3
                           and reopened._thread is None)
            self.assertEqual(read_json(self.directory / "gamestate.json")["quests"]["ledger"]["status"],
                             "completed")
        finally:
            reopened.close()
            self.app.processEvents()

    def _wait(self, predicate, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Qt integration operation did not finish")


if __name__ == "__main__":
    unittest.main()
