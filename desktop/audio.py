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
            print("ElevenLabs narration: API key missing; text-only mode.", flush=True)
            raise AudioError("ElevenLabs key is not configured; continuing in text-only mode.")
        try:
            from elevenlabs.client import ElevenLabs
        except ImportError as error:
            raise AudioError("Install ElevenLabs support with: python -m pip install -r requirements.txt") from error
        self._client = ElevenLabs(api_key=key)
        print("ElevenLabs narration: API key present; client ready.", flush=True)
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
            print(f"ElevenLabs narration: cache hit ({path.name}, mp3).", flush=True)
            return path
        print(f"ElevenLabs narration: cache miss; TTS request started ({kind}).", flush=True)
        client = self._sdk_client()
        try:
            stream = client.text_to_speech.convert(text=text, voice_id=voice_id or self.voice_id,
                                                    model_id=self.model, output_format="mp3_44100_128")
            result = self._write_stream(path, stream)
            print(f"ElevenLabs narration: TTS response saved ({result.name}, mp3).", flush=True)
            return result
        except Exception as error:
            diagnostic = _safe_elevenlabs_error(error, self.api_key())
            print(f"ElevenLabs narration: TTS failed type={type(error).__name__}; "
                  f"voice={voice_id or self.voice_id}; model={self.model}; {diagnostic}", flush=True)
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
    def __init__(self):
        self._player = None
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            self._output = QAudioOutput()
            self._player = QMediaPlayer()
            self._player.setAudioOutput(self._output)
            self._player.errorOccurred.connect(self._on_error)
            print("Qt Multimedia: narration playback available.", flush=True)
        except Exception:
            self._output = None
            print("Qt Multimedia unavailable. Install with: python -m pip install -r requirements.txt", flush=True)

    def _on_error(self, error) -> None:
        print(f"Qt Multimedia: playback failed ({error}).", flush=True)

    def play(self, path: Path, volume: int = 70, loop: bool = False) -> None:
        if self._player is None:
            print("Qt Multimedia: unable to play audio because playback is unavailable.", flush=True)
            return
        from PySide6.QtCore import QUrl
        self._output.setVolume(max(0, min(100, volume)) / 100)
        self._player.setLoops(-1 if loop else 1)
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.play()
        print(f"Qt Multimedia: playback started ({path.name}, mp3).", flush=True)

    def stop(self) -> None:
        if self._player is not None:
            self._player.stop()
            print("Qt Multimedia: playback stopped.", flush=True)
