"""Subprocess-only Qt lifecycle stress check; no network or paid API calls."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEvent, QSettings
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from desktop.audio import AudioError

from desktop.services import GameSession, MockGameService, WorldRef
from desktop.storage import ApiKeyVault, Preferences
from desktop.window import MainWindow


class InMemoryKeyring:
    def get_password(self, service, user):
        return None


class TurnService(MockGameService):
    def __init__(self):
        super().__init__(delay=0.008)
        self.calls = 0

    def perform_action(self, session, action, cancel):
        result = super().perform_action(session, action, cancel)
        self.calls += 1
        result.turn = session.turn + 1
        result.last_narration = "The bell rings beside the coast."
        result.audio_cues = {"sfx": ["A short warning bell"]}
        return result


class LocalAudio:
    def __init__(self, path):
        self.path = path
        self.narrations = 0
        self.effects = 0

    def api_key(self):
        return "mock-present"

    def speak_narration(self, text):
        self.narrations += 1
        return self.path

    def play_sound_effect(self, description):
        self.effects += 1
        if self.effects in (7, 14):
            raise AudioError("mock SFX failure")
        return self.path


def wait_for(app, predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("Qt task did not finish")


def main():
    app = QApplication([])
    with TemporaryDirectory() as temp:
        root = Path(temp)
        wav = root / "cached-audio.wav"
        with wave.open(str(wav), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"\0\0" * 2400)
        settings = QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat)
        prefs = Preferences(settings=settings, base=root)
        prefs.update(str(root / "worlds"), str(root / "saves"), True, 70)
        service = TurnService()
        window = MainWindow(preferences=prefs, vault=ApiKeyVault(settings, backend=InMemoryKeyring()),
                            game_service=service)
        audio = LocalAudio(wav)
        window.audio_service = audio
        window.show()
        session = GameSession(WorldRef("Test World"), "Introduction", "An explorer", "Explorer", "Harbor")
        window._show_session(session)
        for turn in range(1, 21):
            window.game.action.setPlainText(f"I look around turn {turn}")
            window.take_action()
            active = window._thread
            assert active is not None
            window.take_action()  # must not enqueue a duplicate
            assert window._thread is active
            if turn == 5:
                window.show_menu()  # navigation is blocked while busy
                assert window.stack.currentWidget() is window.game
            wait_for(app, lambda: window.session.turn == turn and window._thread is None)
            assert service.calls == turn
        assert audio.narrations == 20 and audio.effects == 20
        wait_for(app, lambda: not window._retired_tasks)
        assert window.session.turn == 20
        window.audio_playback.stop()
        window.audio_playback._player.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not isValid(window.audio_playback._player)
        window.audio_playback.stop()
        window.audio_playback.play_sequence([wav])  # deleted Qt object is ignored safely
        # Closing during a running task must wait for its cancellation/finish.
        window.game.action.setPlainText("One more action")
        window.take_action()
        window.close()
        wait_for(app, lambda: window._thread is None)
        app.processEvents()
        assert not window.isVisible()
        print("20 mocked gameplay turns, narration/SFX events, and shutdown: PASSED")


if __name__ == "__main__":
    sys.exit(main())
