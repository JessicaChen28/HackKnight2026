"""ElevenLabs generation, caching, and Qt playback helpers.

The SDK is imported lazily so the desktop app remains usable in text-only mode.
Generated files are cache artifacts and never part of authoritative game saves.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Callable


DEFAULT_VOICE = "Rachel"
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
DEFAULT_MODEL = "eleven_multilingual_v2"


class AudioError(RuntimeError):
    pass


def _safe_elevenlabs_error(error, api_key: str = "") -> str:
    """Return a short diagnostic without credentials or response payloads."""
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    body = getattr(error, "body", None)
    api_code = api_detail = ""
    if isinstance(body, dict):
        api_code = str(body.get("code") or body.get("error_code") or "")
        api_detail = str(body.get("detail") or body.get("message") or "")
        if isinstance(body.get("detail"), dict):
            detail = body["detail"]
            api_code = str(detail.get("status") or detail.get("code") or api_code)
            api_detail = str(detail.get("message") or detail.get("detail") or "")
    message = str(getattr(error, "message", "") or api_detail or "No provider message")
    message = message.splitlines()[0]
    if api_key:
        message = message.replace(api_key, "[credential removed]")
    message = re.sub(r"(?i)Bearer\s+\S+", "Bearer [credential removed]", message)
    message = re.sub(r"(?i)(?:api[_-]?key|authorization|key)=\S+", "[credential removed]", message)
    message = re.sub(r"AIza[A-Za-z0-9_-]{12,}", "[credential removed]", message)
    message = re.sub(r"[\x00-\x1f\x7f]", " ", message).strip()[:240]
    lower = f"{status} {api_code} {message}".lower()
    if status in (401, 403) or any(word in lower for word in ("unauthorized", "authentication", "invalid api key", "permission")):
        category = "authentication/permissions"
    elif status == 429 or any(word in lower for word in ("quota", "rate limit", "too many")):
        category = "quota/rate limit"
    elif status == 400 or any(word in lower for word in ("invalid", "parameter", "voice", "model")):
        category = "invalid parameters"
    else:
        category = "unknown/provider failure"
    return (f"status={status or 'unknown'} category={category} "
            f"api_code={api_code or 'unknown'} detail={message or 'none'}")


class ElevenLabsAudioService:
    def __init__(self, api_key: Callable[[], str] | str | None = None,
                 cache_dir: Path | None = None, voice_id: str = DEFAULT_VOICE_ID,
                 model: str = DEFAULT_MODEL, client=None):
        self._key = api_key
        self.cache_dir = Path(cache_dir or (Path.home() / ".solar-rpg" / "audio-cache"))
        self.voice_id, self.model, self._client = voice_id, model, client
        self._resolved_voices: dict[str, str] = {}

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
            print("ElevenLabs narration: API key missing; text-only mode.", flush=True)
            raise AudioError("ElevenLabs key is not configured; continuing in text-only mode.")
        try:
            from elevenlabs.client import ElevenLabs
        except ImportError as error:
            raise AudioError("Install ElevenLabs support with: python -m pip install -r requirements.txt") from error
        self._client = ElevenLabs(api_key=key)
        print("ElevenLabs narration: API key present; client ready.", flush=True)
        return self._client

    def _resolve_voice_id(self, requested: str | None, client) -> str:
        configured = (requested or self.voice_id or os.environ.get("ELEVENLABS_VOICE_ID", "")).strip()
        if not configured:
            raise AudioError("No ElevenLabs voice ID is configured.")
        # Names are accepted as a convenience, but are resolved before any
        # request. A display name is never passed as voice_id.
        if configured in self._resolved_voices:
            return self._resolved_voices[configured]
        looks_like_id = bool(re.fullmatch(r"[A-Za-z0-9_-]{15,}", configured))
        voices_api = getattr(client, "voices", None)
        if not looks_like_id:
            if voices_api is None or not hasattr(voices_api, "get_all"):
                raise AudioError(f"ElevenLabs voice name '{configured}' could not be resolved.")
            try:
                voices = voices_api.get_all().voices
            except Exception as error:
                raise AudioError("Could not retrieve accessible ElevenLabs voices.") from error
            match = next((voice for voice in voices
                          if str(getattr(voice, "name", "")).casefold() == configured.casefold()), None)
            resolved = str(getattr(match, "voice_id", "")) if match else ""
            if not resolved:
                raise AudioError(f"ElevenLabs voice name '{configured}' was not found in this account.")
            self._resolved_voices[configured] = resolved
            print(f"ElevenLabs narration: resolved voice name to account voice ID ({resolved}).", flush=True)
            return resolved
        return configured

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
        client = self._sdk_client()
        resolved_voice_id = self._resolve_voice_id(voice_id, client)
        path = self._path(kind, text, resolved_voice_id)
        if path.exists() and path.stat().st_size:
            print(f"ElevenLabs narration: cache hit ({path.name}, mp3).", flush=True)
            return path
        print(f"ElevenLabs narration: cache miss; TTS request started ({kind}).", flush=True)
        try:
            stream = client.text_to_speech.convert(text=text, voice_id=resolved_voice_id,
                                                    model_id=self.model, output_format="mp3_44100_128")
            result = self._write_stream(path, stream)
            print(f"ElevenLabs narration: TTS response saved ({result.name}, mp3).", flush=True)
            return result
        except Exception as error:
            diagnostic = _safe_elevenlabs_error(error, self.api_key())
            print(f"ElevenLabs narration: TTS failed type={type(error).__name__}; "
                  f"voice={resolved_voice_id}; model={self.model}; {diagnostic}", flush=True)
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
            diagnostic = _safe_elevenlabs_error(error, self.api_key())
            print(f"ElevenLabs sound effect failed type={type(error).__name__}; {diagnostic}", flush=True)
            raise AudioError("ElevenLabs sound generation failed.") from error

    def set_ambience(self, description: str) -> Path:
        path = self.play_sound_effect(description)
        return path


class AudioPlayback:
    """Qt multimedia playback, kept optional for headless tests."""
    def __init__(self, parent=None):
        self._player = None
        self._output = None
        self._source = None
        self._playlist: list[Path] = []
        self._playlist_volume = 70
        try:
            from PySide6.QtCore import QThread
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            owner = parent if parent is not None else None
            self._output = QAudioOutput(owner)
            self._player = QMediaPlayer(owner)
            self._player.setAudioOutput(self._output)
            self._player.errorOccurred.connect(self._on_error)
            self._player.mediaStatusChanged.connect(self._on_media_status)
            self._qt_thread = QThread.currentThread()
            print("Qt Multimedia: narration playback available on the GUI thread.", flush=True)
        except Exception as error:
            print("Qt Multimedia unavailable. Install with: python -m pip install -r requirements.txt", flush=True)
            print(f"Qt Multimedia initialization failed ({type(error).__name__}).", flush=True)

    def _on_error(self, error) -> None:
        print(f"Qt Multimedia: playback failed ({error}).", flush=True)
        self._advance_playlist()

    def _on_media_status(self, status) -> None:
        if self._player is None:
            return
        from PySide6.QtCore import QTimer
        from PySide6.QtMultimedia import QMediaPlayer
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            QTimer.singleShot(0, self._advance_playlist)

    def _advance_playlist(self) -> None:
        if self._playlist:
            path = self._playlist.pop(0)
            self.play(path, self._playlist_volume)

    def play_sequence(self, paths: list[Path], volume: int = 70) -> None:
        """Play generated narration and sound cues in order on the GUI thread."""
        self._playlist = [Path(path) for path in paths]
        self._playlist_volume = volume
        self._advance_playlist()

    def play(self, path: Path, volume: int = 70, loop: bool = False) -> None:
        if self._player is None:
            print("Qt Multimedia: unable to play audio because playback is unavailable.", flush=True)
            return
        from PySide6.QtCore import QThread, QUrl
        if self._player.thread() is not QThread.currentThread():
            print("Qt Multimedia: refusing playback from a non-GUI thread.", flush=True)
            return
        self._output.setVolume(max(0, min(100, volume)) / 100)
        self._player.setLoops(-1 if loop else 1)
        self._source = QUrl.fromLocalFile(str(path))
        self._player.setSource(self._source)
        self._player.play()
        print(f"Qt Multimedia: playback started ({path.name}, mp3).", flush=True)

    def stop(self) -> None:
        self._playlist.clear()
        if self._player is not None:
            self._player.stop()
            print("Qt Multimedia: playback stopped.", flush=True)
