"""Headless Qt interaction tests; no browser, network, key, or OS vault needed."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from desktop.services import (AzgaarWorldProvider, CampaignIntro, GameError, GameSession, MockGameService,
                              PhaseTwoCampaignService, WorldRef)
from desktop.storage import ApiKeyVault, Preferences, SERVICE_NAME
from desktop.window import CampaignPromotionError, MainWindow
from safe_storage import read_json, write_json


class MemoryKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, user):
        return self.values.get((service, user))

    def set_password(self, service, user, value):
        self.values[(service, user)] = value

    def delete_password(self, service, user):
        del self.values[(service, user)]


class CountingProvider:
    def __init__(self, delay=0.1):
        self.calls = 0
        self.delay = delay

    def generate(self, export_root, cancel, progress):
        self.calls += 1
        progress("Building a test world...")
        end = time.monotonic() + self.delay
        while time.monotonic() < end:
            if cancel.is_set():
                raise ValueError("Cancelled")
            time.sleep(0.01)
        return WorldRef("Test Vale")


class DesktopTests(unittest.TestCase):
    def test_launcher_starts_without_api_key(self):
        environment = os.environ.copy()
        environment["QT_QPA_PLATFORM"] = "offscreen"
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parents[1] / "run_desktop.py")],
                                   cwd=Path(__file__).resolve().parents[1], env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(0.8)
            if process.poll() is not None:
                _, error = process.communicate(timeout=3)
                self.fail(f"Desktop launcher exited early: {error.decode(errors='replace')}")
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setOrganizationName("SOLARRPG")
        cls.app.setApplicationName("SOLARRPGTests")

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        settings = QSettings(str(self.root / "prefs.ini"), QSettings.Format.IniFormat)
        self.prefs = Preferences(settings=settings, base=self.root)
        self.keyring = MemoryKeyring()
        self.vault = ApiKeyVault(settings, backend=self.keyring)
        self.provider = CountingProvider(delay=0.15)
        self.window = MainWindow(preferences=self.prefs, vault=self.vault,
                                 world_provider=self.provider, sample_provider=self.provider,
                                 game_service=MockGameService(delay=0.1))
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        if self.window._thread is not None:
            self.window.cancel_operation()
            self.wait_idle()
        self.window.close()
        self.app.processEvents()
        self.temp.cleanup()

    def wait_until(self, condition, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if condition():
                return
            time.sleep(0.01)
        self.fail("Desktop operation did not complete")

    def wait_idle(self):
        self.wait_until(lambda: self.window._thread is None)

    def begin_game(self):
        self.window.menu.new_button.click()
        self.wait_until(lambda: self.window.stack.currentWidget() is self.window.creation)
        self.wait_idle()
        self.window.creation.description.setPlainText("A curious archivist who fears the dark.")
        self.window.creation.create_button.click()
        self.wait_until(lambda: self.window.stack.currentWidget() is self.window.game)
        self.wait_idle()

    def test_all_screens_mock_interactions_and_responsive_worker(self):
        window = self.window
        self.assertEqual(window.stack.count(), 4)
        self.assertIs(window.stack.currentWidget(), window.menu)
        window.menu.settings_button.click()
        self.assertIs(window.stack.currentWidget(), window.settings_screen)
        window.settings_screen.back_button.click()
        self.assertIs(window.stack.currentWidget(), window.menu)
        started = time.monotonic()
        window.menu.new_button.click()
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertIsNotNone(window._thread)
        self.assertFalse(window.menu.new_button.isEnabled())
        # The main thread continues processing Qt events while service sleeps.
        self.app.processEvents()
        self.assertTrue(window.spinner.isVisible())
        self.wait_until(lambda: window.stack.currentWidget() is window.creation)
        self.wait_idle()
        self.assertIn("Test Vale", window.creation.introduction.toPlainText())
        window.creation.description.setPlainText("An archivist")
        window.creation.create_button.click()
        self.wait_until(lambda: window.stack.currentWidget() is window.game)
        self.wait_idle()
        window.game.action.setPlainText("I look toward the tower")
        window.game.send_button.click()
        self.wait_until(lambda: "footprints" in window.game.history.toPlainText())
        self.wait_idle()
        self.assertEqual(self.provider.calls, 1)
        self.assertTrue(window.game.inventory.count())
        self.assertTrue(window.game.quests.count())
        self.assertIn("Test Vale", window.game.location.text())
        window.game.menu_button.click()
        self.assertIs(window.stack.currentWidget(), window.menu)

    def test_duplicate_generation_and_cancel(self):
        window = self.window
        window.menu.new_button.click()
        self.wait_until(lambda: self.provider.calls == 1)
        window.start_campaign(self.provider)
        self.assertEqual(self.provider.calls, 1)
        window.cancel_button.click()
        self.wait_idle()
        self.assertIs(window.stack.currentWidget(), window.menu)
        self.assertTrue(window.menu.new_button.isEnabled())
        window.menu.new_button.click()
        self.assertTrue(window.cancel_button.isEnabled())
        window.cancel_button.click()
        self.wait_idle()

    def test_error_state_allows_retry(self):
        window = self.window
        window.menu.new_button.click()
        self.wait_until(lambda: window.stack.currentWidget() is window.creation)
        self.wait_idle()
        window.creation.create_button.click()
        self.wait_idle()
        self.assertIs(window.stack.currentWidget(), window.creation)
        self.assertIn("Describe your character", window.message.text())
        self.assertTrue(window.creation.create_button.isEnabled())
        window.creation.description.setPlainText("A traveler searching for home")
        window.creation.create_button.click()
        self.wait_until(lambda: window.stack.currentWidget() is window.game)
        self.wait_idle()

    def test_gemini_key_save_clear_load_and_keys_never_enter_saves(self):
        window = self.window
        window.menu.settings_button.click()
        s = window.settings_screen
        s.gemini_key.setText("gemini-secret-value")
        s.elevenlabs_key.setText("voice-secret-value")
        s.audio_enabled.setChecked(True)
        s.volume.setValue(35)
        s.export_root.setText(str(self.root / "custom-worlds"))
        s.save_root.setText(str(self.root / "custom-saves"))
        s.save_button.click()
        self.assertFalse(self.keyring.values)
        self.assertEqual(self.vault.session["gemini"], "")
        self.assertEqual(self.prefs.audio_volume(), 35)
        self.assertEqual(self.prefs.save_root(), self.root / "custom-saves")
        self.assertNotIn("gemini-secret-value", (self.root / "prefs.ini").read_text())
        s.save_gemini_button.click()
        self.assertEqual(self.keyring.get_password(SERVICE_NAME, "gemini"), "gemini-secret-value")
        self.assertIn("saved securely", window.message.text())
        self.assertEqual(ApiKeyVault(self.prefs.settings, backend=self.keyring).session["gemini"],
                         "gemini-secret-value")
        window.menu.settings_button.click()
        self.assertEqual(s.gemini_key.text(), "gemini-secret-value")
        s.clear_gemini_button.click()
        self.assertFalse(self.keyring.values)
        self.assertEqual(s.gemini_key.text(), "")
        self.assertIn("cleared", window.message.text())
        self.begin_game()
        window.game.save_button.click()
        self.wait_until(lambda: any(self.prefs.save_root().glob("*.json")))
        self.wait_idle()
        saved = next(self.prefs.save_root().glob("*.json"))
        raw = saved.read_text(encoding="utf-8")
        self.assertNotIn("gemini-secret-value", raw)
        self.assertNotIn("voice-secret-value", raw)
        window.show_menu()
        window.load_game_from_path(saved)
        self.wait_until(lambda: window.stack.currentWidget() is window.game)
        self.wait_idle()
        self.assertIsInstance(window.session, GameSession)
        self.assertEqual(window.session.world.name, "Test Vale")

    def test_narration_is_plain_text_not_html(self):
        self.begin_game()
        payload = "<script>alert('unsafe')</script><b>not bold</b>"
        self.window.session.history.append({"speaker": "Narrator", "text": payload})
        self.window.session.character_description = payload
        self.window.session.world.name = payload
        self.window.game.show_session(self.window.session)
        self.assertIn(payload, self.window.game.history.toPlainText())
        self.assertIn(payload, self.window.game.character_details.text())
        self.assertIn(payload, self.window.game.location.text())
        self.assertEqual(self.window.game.character_details.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(self.window.game.location.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(self.window.message.textFormat(), Qt.TextFormat.PlainText)

    def test_failed_credential_manager_does_not_write_plaintext(self):
        class FailingKeyring(MemoryKeyring):
            def set_password(self, service, user, value):
                raise RuntimeError("backend unavailable")

        self.window.vault.backend = FailingKeyring()
        self.window.open_settings()
        screen = self.window.settings_screen
        screen.gemini_key.setText("secret-only-in-memory")
        screen.save_gemini_button.click()
        self.assertIs(self.window.stack.currentWidget(), screen)
        self.assertIn("Could not save", self.window.message.text())
        self.assertEqual(self.window.vault.session["gemini"], "")
        self.assertFalse(self.window.vault.is_saved("gemini"))
        preferences_file = self.root / "prefs.ini"
        self.assertNotIn("secret-only-in-memory", preferences_file.read_text() if preferences_file.exists() else "")

    def test_gemini_field_is_masked_with_required_placeholder(self):
        self.window.open_settings()
        field = self.window.settings_screen.gemini_key
        self.assertEqual(field.placeholderText(), "Enter your Gemini API Key...")
        self.assertEqual(field.echoMode(), field.EchoMode.Password)
        self.assertTrue(field.font().italic())
        field.setText("test-key")
        self.assertFalse(field.font().italic())

    def test_actual_phase_one_adapter_calls_callable_generator(self):
        world_dir = self.root / "world-a"
        world_dir.mkdir()
        write_json(world_dir / "world.json", {"metadata": {"mapName": "Actual Vale", "seed": 55}})
        with patch("generate_world.generate_world", return_value=world_dir / "azgaar_minimal.json") as generate:
            cancel = Event()
            world = AzgaarWorldProvider().generate(self.root, cancel, lambda message: None)
        generate.assert_called_once()
        self.assertEqual(world.directory, str(world_dir))
        self.assertEqual(world.name, "Actual Vale")
        self.assertEqual(world.seed, "55")

    def test_phase_two_adapter_initializes_directory_with_key_and_returns_intro(self):
        world_dir = self.root / "phase-two-world"
        world_dir.mkdir()
        service = PhaseTwoCampaignService(lambda: "saved-gemini-key")
        with patch("campaign_init.GeminiClient", return_value=object()) as client, \
             patch("campaign_init.initialize_campaign_from_directory", return_value="A real campaign opening.") as initialize:
            intro = service.initialize_campaign(WorldRef("Bright Vale", str(world_dir)), Event())
        client.assert_called_once()
        self.assertEqual(client.call_args.kwargs["api_key"], "saved-gemini-key")
        initialize.assert_called_once()
        self.assertEqual(initialize.call_args.args, (world_dir,))
        self.assertIs(initialize.call_args.kwargs["client"], client.return_value)
        self.assertEqual(initialize.call_args.kwargs["max_attempts"], 2)
        self.assertEqual(intro.world.name, "Bright Vale")
        self.assertEqual(intro.text, "A real campaign opening.")

    def test_settings_key_precedes_environment_fallback(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "environment-key"}):
            self.assertEqual(self.vault.gemini_key(), "environment-key")
            self.window.open_settings()
            self.window.settings_screen.gemini_key.setText("settings-key")
            self.window.settings_screen.save_gemini_button.click()
            self.assertEqual(self.vault.gemini_key(), "settings-key")
            self.assertEqual(ApiKeyVault(self.prefs.settings, backend=self.keyring).gemini_key(),
                             "settings-key")
            self.window.settings_screen.clear_gemini_button.click()
            self.assertEqual(self.vault.gemini_key(), "environment-key")

    def test_new_game_uses_settings_key_and_shows_gemini_introduction(self):
        world_dir = self.root / "generated-world"
        world_dir.mkdir()

        class DirectoryProvider:
            def generate(self, export_root, cancel, progress):
                progress("Generated test world.")
                return WorldRef("Bright Vale", str(world_dir))

        live_window = MainWindow(preferences=self.prefs, vault=self.vault,
                                 world_provider=DirectoryProvider())
        live_window.show()
        try:
            live_window.open_settings()
            live_window.settings_screen.gemini_key.setText("settings-test-key")
            live_window.settings_screen.save_gemini_button.click()
            live_window.settings_screen.back_button.click()

            def initialize(directory, **kwargs):
                kwargs["progress"]("Campaign response parsed in 0.0s.")
                return "An introduction from Gemini."

            with patch("campaign_init.GeminiClient", return_value=object()) as client, \
                 patch("campaign_init.initialize_campaign_from_directory", side_effect=initialize) as init:
                live_window.menu.new_button.click()
                self.wait_until(lambda: live_window.stack.currentWidget() is live_window.creation)
                self.wait_until(lambda: live_window._thread is None)
            self.assertEqual(live_window.creation.introduction.toPlainText(),
                             "An introduction from Gemini.")
            self.assertIn("Gemini wrote", live_window.creation.mode_note.text())
            self.assertEqual(client.call_args.kwargs["api_key"], "settings-test-key")
            self.assertEqual(init.call_args.args, (world_dir,))
            self.assertNotIn("settings-test-key", live_window.message.text())
        finally:
            live_window.close()

    def test_phase_two_adapter_requires_a_saved_gemini_key(self):
        service = PhaseTwoCampaignService(lambda: "")
        with self.assertRaisesRegex(GameError, "Save a Gemini API key"):
            service.initialize_campaign(WorldRef("Bright Vale", str(self.root)), Event())

    def test_workspace_promotion_and_safe_cleanup(self):
        pending = self.root / ".solar-pending-test"
        generated = pending / "World_unique"
        generated.mkdir(parents=True)
        (generated / "world.json").write_text("{}", encoding="utf-8")
        promoted = MainWindow._promote_workspace_world(
            WorldRef("World", str(generated)), pending, self.root)
        self.assertEqual(Path(promoted.directory), self.root / "World_unique")
        self.assertTrue(Path(promoted.directory).exists())
        self.assertFalse(pending.exists())

        unrelated = self.root / "do-not-delete"
        unrelated.mkdir()
        MainWindow._cleanup_workspace(unrelated, self.root)
        self.assertTrue(unrelated.exists())

    def test_promotion_failure_preserves_completed_workspace(self):
        pending = self.root / ".solar-pending-locked"
        generated = pending / "World_locked"
        generated.mkdir(parents=True)
        (generated / "world.json").write_text("{}", encoding="utf-8")
        with patch("desktop.window.os.replace", side_effect=OSError(5, "Access is denied")), \
             patch("desktop.window.time.sleep"):
            with self.assertRaisesRegex(CampaignPromotionError, "Recoverable files remain at"):
                MainWindow._promote_workspace_world(WorldRef("World", str(generated)), pending, self.root)
        self.assertTrue(generated.exists())

    def test_promotion_collision_preserves_completed_workspace(self):
        pending = self.root / ".solar-pending-collision"
        generated = pending / "World_same"
        generated.mkdir(parents=True)
        (self.root / "World_same").mkdir()
        with self.assertRaises(CampaignPromotionError):
            MainWindow._promote_workspace_world(WorldRef("World", str(generated)), pending, self.root)
        self.assertTrue(generated.exists())

    def test_audio_narration_is_queued_only_when_enabled(self):
        self.window.preferences.update(str(self.root / "exports"), str(self.root / "saves"), True, 70)
        self.window._show_intro(CampaignIntro(WorldRef("Test"), "An opening."))
        self.assertEqual(self.window._pending_narration, ("An opening.", "Test"))

    def test_reject_malformed_save_without_credential_fields(self):
        candidate = self.root / "invalid.json"
        write_json(candidate, {"schema_version": 1, "session": {"api_key": "must-not-load"}})
        with self.assertRaises(ValueError):
            self.window.save_store.load(candidate)


if __name__ == "__main__":
    unittest.main()
