"""Play an existing local audio file without Gemini or ElevenLabs.

Usage: python play_cached_audio.py path/to/file.mp3
Set SOLAR_RPG_DEVELOPMENT=1 for faulthandler and Qt diagnostics.
"""
import faulthandler
import os
import sys
from pathlib import Path

if os.environ.get("SOLAR_RPG_DEVELOPMENT") == "1":
    faulthandler.enable(all_threads=True)

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from desktop.audio import AudioPlayback


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python play_cached_audio.py path/to/file.mp3", file=sys.stderr)
        return 2
    path = Path(sys.argv[1]).expanduser().resolve()
    if not path.is_file():
        print(f"Audio file not found: {path}", file=sys.stderr)
        return 2
    app = QApplication(sys.argv)
    playback = AudioPlayback()
    playback.play(path)
    # Keep both playback objects alive until the event loop exits.
    app.aboutToQuit.connect(playback.stop)
    QTimer.singleShot(max(10_000, 1_000), lambda: None)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
