import os
import unittest
from unittest import mock

from bot import handlers, voice


class ConfiguredTests(unittest.TestCase):
    def test_not_configured_without_url(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(voice.is_configured())

    def test_configured_with_url(self):
        with mock.patch.dict(os.environ, {"SPEECH_API_URL": "http://example.local:8000/v1"}):
            self.assertTrue(voice.is_configured())


class ClientTests(unittest.TestCase):
    """The speech server is the only thing the audio may reach, and it gets no credential.

    This is the guard from the global secrets rule: if someone ever wires the Telegram
    token or the Gemini key into this client, these tests go red."""

    def setUp(self):
        self._orig = voice._client
        voice._client = None
        self.addCleanup(lambda: setattr(voice, "_client", self._orig))

    def test_client_points_at_the_configured_server_with_a_dummy_key(self):
        env = {
            "SPEECH_API_URL": "http://example.local:8000/v1",
            "TELEGRAM_BOT_TOKEN": "real-telegram-token",
            "GEMINI_API_KEY": "real-gemini-key",
        }
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(voice, "OpenAI") as openai_cls:
                voice._get_client()
        openai_cls.assert_called_once_with(
            base_url="http://example.local:8000/v1", api_key="not-needed"
        )
        _, kwargs = openai_cls.call_args
        for value in kwargs.values():
            self.assertNotIn("real-telegram-token", str(value))
            self.assertNotIn("real-gemini-key", str(value))

    def test_client_is_built_once(self):
        with mock.patch.dict(os.environ, {"SPEECH_API_URL": "http://example.local:8000/v1"}):
            with mock.patch.object(voice, "OpenAI") as openai_cls:
                first = voice._get_client()
                second = voice._get_client()
        self.assertIs(first, second)
        openai_cls.assert_called_once()


class TranscribeTests(unittest.TestCase):
    def _transcribe(self, text: str, env: dict | None = None, audio: bytes = b"OggS") -> tuple:
        client = mock.Mock()
        client.audio.transcriptions.create.return_value = mock.Mock(text=text)
        with mock.patch.dict(os.environ, env or {}):
            with mock.patch.object(voice, "_get_client", return_value=client):
                result = voice.transcribe(audio)
        return result, client.audio.transcriptions.create

    def test_sends_the_audio_and_default_model(self):
        result, create = self._transcribe("agrega leche", audio=b"opus-bytes")
        self.assertEqual(result, "agrega leche")
        create.assert_called_once_with(
            model=voice.STT_MODEL_DEFAULT, file=("voice.ogg", b"opus-bytes"), language="es"
        )

    def test_model_is_overridable_from_the_environment(self):
        _, create = self._transcribe(
            "hi", env={"SPEECH_STT_MODEL": "Systran/faster-whisper-small"}
        )
        self.assertEqual(create.call_args.kwargs["model"], "Systran/faster-whisper-small")

    def test_whitespace_is_stripped(self):
        result, _ = self._transcribe("  add milk \n")
        self.assertEqual(result, "add milk")

    def test_silence_comes_back_empty(self):
        result, _ = self._transcribe("   ")
        self.assertEqual(result, "")


class SynthesizeTests(unittest.TestCase):
    def _synthesize(self, env: dict | None = None) -> tuple:
        client = mock.Mock()
        client.audio.speech.create.return_value = mock.Mock(content=b"mp3-bytes")
        with mock.patch.dict(os.environ, env or {}):
            with mock.patch.object(voice, "_get_client", return_value=client):
                result = voice.synthesize("Recordatorio de citas")
        return result, client.audio.speech.create

    def test_sends_the_text_with_the_default_spanish_voice(self):
        result, create = self._synthesize()
        self.assertEqual(result, b"mp3-bytes")
        create.assert_called_once_with(
            model=voice.TTS_MODEL_DEFAULT,
            voice=voice.TTS_VOICE_DEFAULT,
            input="Recordatorio de citas",
            response_format="mp3",
        )

    def test_model_and_voice_are_overridable_from_the_environment(self):
        _, create = self._synthesize(
            env={"SPEECH_TTS_MODEL": "other-model", "SPEECH_TTS_VOICE": "em_alex"}
        )
        self.assertEqual(create.call_args.kwargs["model"], "other-model")
        self.assertEqual(create.call_args.kwargs["voice"], "em_alex")


class TranscriptEchoTests(unittest.TestCase):
    def test_reply_prefix_shows_what_was_heard(self):
        self.assertEqual(
            handlers.format_transcript("agrega leche y huevos"),
            "🎤 «agrega leche y huevos»\n\n",
        )


if __name__ == "__main__":
    unittest.main()
