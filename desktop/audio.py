"""ElevenLabs generation, caching, and Qt playback helpers.

The SDK is imported lazily so the desktop app remains usable in text-only mode.
Generated files are cache artifacts and never part of authoritative game saves.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable


DEFAULT_VOICE = "Rachel"
DEFAULT_MODEL = "eleven_multilingual_v2"


class AudioError(RuntimeError):
    pass


class ElevenLabsAudioService:
    def __init__(self, api_key: Callable[[], str] | str | None = None,
                 cache_dir: Path | None = None, voice_id: str = DEFAULT_VOICE,
                 model: str = DEFAULT_MODEL, client=None):
        self._key = api_key
        self.cache_dir = Path(cache_dir or (Path.home() / ".solar-rpg" / "audio-cache"))
        self.voice_id, self.model, self._client = voice_id, model, client

    def api_key(self) -> str:
        value = self._key() if callable(self._key) else self._key
        return (value or os.environ.get("ELEVENLABS_API_KEY", "")).strip()

    def _path(self, kind: str, text: str, voice: str | None = None) -> Path:
        token = hashlib.sha256(f"{kind}\0{voice or self.voice_id}\0{self.model}\0{text}".encode()).hexdigest()
        return self.cache_dir / f"{kind}-{token}.mp3"

    def _sdk_client(self):
        if self._client is not None:
            return self._client
        key = self.api_key()
        if not key:
            raise AudioError("ElevenLabs key is not configured; continuing in text-only mode.")
        try:
            from elevenlabs.client import ElevenLabs
        except ImportError as error:
            raise AudioError("Install ElevenLabs support with: python -m pip install -r requirements.txt") from error
        self._client = ElevenLabs(api_key=key)
        return self._client

    @staticmethod
    def _write_stream(path: Path, stream) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as output:
            if isinstance(stream, (bytes, bytearray)):
                output.write(stream)
            else:
                for chunk in stream:
                    if chunk:
                        output.write(chunk)
        return path

    def speak_narration(self, text: str, voice_id: str | None = None) -> Path:
        return self._tts(text, voice_id=voice_id, kind="narration")

    def speak_dialogue(self, speaker_id: str, text: str, voice_id: str | None = None) -> Path:
        # Speaker ID participates in the cache key so future stable mappings are safe.
        return self._tts(f"[{speaker_id}] {text}", voice_id=voice_id, kind="dialogue")

    def _tts(self, text: str, voice_id: str | None, kind: str) -> Path:
        if not text.strip():
            raise AudioError("Cannot generate audio for empty text.")
        path = self._path(kind, text, voice_id)
        if path.exists() and path.stat().st_size:
            return path
        client = self._sdk_client()
        try:
            stream = client.text_to_speech.convert(text=text, voice_id=voice_id or self.voice_id,
                                                    model_id=self.model, output_format="mp3_44100_128")
            return self._write_stream(path, stream)
        except Exception as error:
            raise AudioError("ElevenLabs narration generation failed.") from error

    def play_sound_effect(self, description: str) -> Path:
        if not description.strip():
            raise AudioError("Cannot generate an empty sound effect.")
        path = self._path("sfx", description, "")
        if path.exists() and path.stat().st_size:
            return path
        try:
            stream = self._sdk_client().sound_generation.convert(text=description, output_format="mp3_44100_128")
            return self._write_stream(path, stream)
        except Exception as error:
            raise AudioError("ElevenLabs sound generation failed.") from error

    def set_ambience(self, description: str) -> Path:
        path = self.play_sound_effect(description)
        return path


class AudioPlayback:
    """Qt multimedia playback, kept optional for headless tests."""
    def __init__(self):
        self._player = None
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            self._output = QAudioOutput()
            self._player = QMediaPlayer()
            self._player.setAudioOutput(self._output)
        except Exception:
            self._output = None

    def play(self, path: Path, volume: int = 70, loop: bool = False) -> None:
        if self._player is None:
            return
        from PySide6.QtCore import QUrl
        self._output.setVolume(max(0, min(100, volume)) / 100)
        self._player.setLoops(-1 if loop else 1)
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.play()

    def stop(self) -> None:
        if self._player is not None:
            self._player.stop()
