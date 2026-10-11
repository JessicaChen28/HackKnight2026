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
        self.last_kwargs = kwargs
        return [b"ID3", b" audio"]


class FakeClient:
    def __init__(self):
        self.text_to_speech = FakeTTS()
        self.text_to_sound_effects = FakeTTS()
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
            effect = service.play_sound_effect("rain on a tin roof")
            self.assertEqual(effect.read_bytes(), b"ID3 audio")
            self.assertEqual(effect.suffix, ".mp3")
            service.voice_id = "anotherVoiceId12345"
            service.model = "another_tts_model"
            self.assertEqual(service.play_sound_effect("rain on a tin roof"), effect)
            self.assertEqual(client.text_to_sound_effects.calls, 1)
            self.assertEqual(client.text_to_sound_effects.last_kwargs["model_id"], "eleven_text_to_sound_v2")

    def test_failed_sfx_is_not_retried_and_tts_still_works(self):
        with TemporaryDirectory() as root:
            client = FakeClient()
            calls = []

            def broken(**kwargs):
                calls.append(kwargs)
                raise AttributeError("sound method failed")

            client.text_to_sound_effects.convert = broken
            service = ElevenLabsAudioService("test-key", Path(root), client=client)
            with self.assertRaises(AudioError):
                service.play_sound_effect("warning bell")
            with self.assertRaisesRegex(AudioError, "failed earlier"):
                service.play_sound_effect("warning bell")
            self.assertEqual(len(calls), 1)
            self.assertEqual(service.speak_narration("continue story").read_bytes(), b"ID3 audio")
            self.assertFalse(list(Path(root).glob("*.part")))

    def test_sfx_api_error_is_distinguished_from_local_error(self):
        class ApiErrorLike(Exception):
            status_code = 429
            body = {"detail": {"status": "quota_exceeded", "message": "quota exhausted"}}

        with TemporaryDirectory() as root:
            client = FakeClient()
            calls = []

            def reject(**kwargs):
                calls.append(kwargs)
                raise ApiErrorLike("provider error")

            client.text_to_sound_effects.convert = reject
            service = ElevenLabsAudioService("test-key", Path(root), client=client)
            with patch("builtins.print") as printed:
                with self.assertRaises(AudioError):
                    service.play_sound_effect("wind")
            output = " ".join(str(call) for call in printed.call_args_list)
            self.assertIn("sound effect API failed", output)
            self.assertIn("quota/rate limit", output)
            self.assertNotIn("test-key", output)
            with self.assertRaises(AudioError):
                service.play_sound_effect("wind")
            self.assertEqual(len(calls), 1)

    def test_partial_sfx_response_does_not_enter_cache(self):
        with TemporaryDirectory() as root:
            client = FakeClient()

            def broken(**kwargs):
                yield b"ID3"
                raise RuntimeError("stream interrupted")

            client.text_to_sound_effects.convert = broken
            service = ElevenLabsAudioService("test-key", Path(root), client=client)
            with self.assertRaises(AudioError):
                service.play_sound_effect("wind")
            self.assertFalse(list(Path(root).glob("*.mp3")))
            self.assertFalse(list(Path(root).glob("*.part")))


if __name__ == "__main__":
    unittest.main()
