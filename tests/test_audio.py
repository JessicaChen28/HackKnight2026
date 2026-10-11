import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from desktop.audio import AudioError, ElevenLabsAudioService, _safe_elevenlabs_error


class FakeTTS:
    def __init__(self):
        self.calls = 0
        self.last_voice = None
    def convert(self, **kwargs):
        self.calls += 1
        self.last_voice = kwargs.get("voice_id")
        return [b"ID3", b" audio"]


class FakeClient:
    def __init__(self):
        self.text_to_speech = FakeTTS()
        self.sound_generation = FakeTTS()
        self.voices = type("Voices", (), {"get_all": lambda self: type("Result", (), {
            "voices": [type("Voice", (), {"name": "Rachel", "voice_id": "validVoiceId123456"})()]
        })()})()


class AudioTests(unittest.TestCase):
    def test_api_error_is_sanitized_and_classified(self):
        class ApiErrorLike(Exception):
            status_code = 401
            body = {"detail": {"status": "invalid_api_key", "message": "bad key"}}
        result = _safe_elevenlabs_error(ApiErrorLike(), "secret-key")
        self.assertIn("status=401", result)
        self.assertIn("authentication/permissions", result)
        self.assertIn("invalid_api_key", result)
        self.assertNotIn("secret-key", result)

    def test_invalid_parameter_error_is_classified(self):
        class ApiErrorLike(Exception):
            status_code = 400
            body = {"code": "voice_not_found", "detail": "voice is invalid"}
        self.assertIn("invalid parameters", _safe_elevenlabs_error(ApiErrorLike()))

    def test_tts_cache_avoids_duplicate_requests(self):
        with TemporaryDirectory() as root:
            client = FakeClient()
            service = ElevenLabsAudioService("test-key", Path(root), client=client)
            first = service.speak_narration("Welcome to SOLAR RPG")
            second = service.speak_narration("Welcome to SOLAR RPG")
            self.assertEqual(first, second)
            self.assertEqual(client.text_to_speech.calls, 1)
            self.assertEqual(first.read_bytes(), b"ID3 audio")

    def test_voice_name_resolves_and_id_is_sent(self):
        with TemporaryDirectory() as root:
            client = FakeClient()
            service = ElevenLabsAudioService("test-key", Path(root), voice_id="Rachel", client=client)
            service.speak_narration("hello")
            self.assertEqual(client.text_to_speech.last_voice, "validVoiceId123456")

    def test_unknown_voice_name_fails_before_tts(self):
        with TemporaryDirectory() as root:
            client = FakeClient()
            service = ElevenLabsAudioService("test-key", Path(root), voice_id="Deleted Voice", client=client)
            with self.assertRaisesRegex(AudioError, "was not found"):
                service.speak_narration("hello")

    def test_missing_key_is_non_secret_error(self):
        with TemporaryDirectory() as root:
            service = ElevenLabsAudioService("", Path(root), client=None)
            with patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}, clear=False):
                with self.assertRaisesRegex(AudioError, "text-only"):
                    service.speak_narration("hello")

    def test_sfx_is_cached_separately(self):
        with TemporaryDirectory() as root:
            client = FakeClient()
            service = ElevenLabsAudioService("test-key", Path(root), client=client)
            service.play_sound_effect("rain on a tin roof")
            service.play_sound_effect("rain on a tin roof")
            self.assertEqual(client.sound_generation.calls, 1)


if __name__ == "__main__":
    unittest.main()
