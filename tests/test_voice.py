import contextlib
import io
import os
import unittest
from unittest import mock

from bot import handlers, voice


class ConfiguredTests(unittest.TestCase):
    def test_not_configured_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(voice.is_configured())

    def test_configured_with_key(self):
        with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "dummy-elevenlabs-key"}):
            self.assertTrue(voice.is_configured())


class ClientTests(unittest.TestCase):
    """The ElevenLabs key goes only to ElevenLabs, and no other credential rides along.

    This is the guard from the global secrets rule: if someone ever wires the Telegram
    token or the Gemini key into this client, these tests go red."""

    def setUp(self):
        self._orig = voice._client
        voice._client = None
        self.addCleanup(lambda: setattr(voice, "_client", self._orig))

    def test_client_gets_only_the_elevenlabs_key(self):
        env = {
            "ELEVENLABS_API_KEY": "dummy-elevenlabs-key",
            "TELEGRAM_BOT_TOKEN": "real-telegram-token",
            "GEMINI_API_KEY": "real-gemini-key",
        }
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(voice, "ElevenLabs") as client_cls:
                voice._get_client()
        client_cls.assert_called_once_with(api_key="dummy-elevenlabs-key")
        for value in client_cls.call_args.kwargs.values():
            self.assertNotIn("real-telegram-token", str(value))
            self.assertNotIn("real-gemini-key", str(value))

    def test_client_is_built_once(self):
        with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "dummy-elevenlabs-key"}):
            with mock.patch.object(voice, "ElevenLabs") as client_cls:
                first = voice._get_client()
                second = voice._get_client()
        self.assertIs(first, second)
        client_cls.assert_called_once()


class TranscribeTests(unittest.TestCase):
    def _transcribe(self, text: str, env: dict | None = None, audio: bytes = b"OggS") -> tuple:
        client = mock.Mock()
        client.speech_to_text.convert.return_value = mock.Mock(text=text)
        with mock.patch.dict(os.environ, env or {}):
            with mock.patch.object(voice, "_get_client", return_value=client):
                result = voice.transcribe(audio)
        return result, client.speech_to_text.convert

    def test_sends_the_audio_with_the_default_model_in_spanish(self):
        result, convert = self._transcribe("agrega leche", audio=b"opus-bytes")
        self.assertEqual(result, "agrega leche")
        kwargs = convert.call_args.kwargs
        self.assertEqual(kwargs["model_id"], voice.STT_MODEL_DEFAULT)
        self.assertEqual(kwargs["language_code"], "es")
        self.assertEqual(kwargs["file"].read(), b"opus-bytes")

    def test_model_is_overridable_from_the_environment(self):
        _, convert = self._transcribe("hi", env={"ELEVENLABS_STT_MODEL": "scribe_v1"})
        self.assertEqual(convert.call_args.kwargs["model_id"], "scribe_v1")

    def test_whitespace_is_stripped(self):
        result, _ = self._transcribe("  add milk \n")
        self.assertEqual(result, "add milk")

    def test_silence_comes_back_empty(self):
        result, _ = self._transcribe("   ")
        self.assertEqual(result, "")


class SynthesizeTests(unittest.TestCase):
    def _synthesize(self, env: dict | None = None) -> tuple:
        client = mock.Mock()
        client.text_to_speech.convert.return_value = iter([b"mp3-", b"bytes"])
        with mock.patch.dict(os.environ, env or {}):
            with mock.patch.object(voice, "_get_client", return_value=client):
                result = voice.synthesize("Recordatorio de citas")
        return result, client.text_to_speech.convert

    def test_joins_the_chunks_with_default_voice_and_model(self):
        result, convert = self._synthesize()
        self.assertEqual(result, b"mp3-bytes")
        convert.assert_called_once_with(
            text="Recordatorio de citas",
            voice_id=voice.TTS_VOICE_DEFAULT,
            model_id=voice.TTS_MODEL_DEFAULT,
            output_format="mp3_44100_128",
        )

    def test_voice_and_model_are_overridable_from_the_environment(self):
        _, convert = self._synthesize(
            env={"ELEVENLABS_VOICE_ID": "custom-voice", "ELEVENLABS_TTS_MODEL": "eleven_multilingual_v2"}
        )
        self.assertEqual(convert.call_args.kwargs["voice_id"], "custom-voice")
        self.assertEqual(convert.call_args.kwargs["model_id"], "eleven_multilingual_v2")


class SpeakableTests(unittest.TestCase):
    def test_strips_emoji_from_a_reply(self):
        self.assertEqual(
            voice.speakable("✅ Agregado a tu lista: leche"), "Agregado a tu lista: leche"
        )

    def test_multiline_replies_keep_their_lines(self):
        self.assertEqual(
            voice.speakable("🛒 Mi lista — 2 artículos\n1. leche\n2. pan"),
            "Mi lista — 2 artículos\n1. leche\n2. pan",
        )


class SendVoiceNoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_the_synthesized_audio(self):
        bot = mock.Mock(send_voice=mock.AsyncMock())
        with mock.patch.object(voice, "synthesize", return_value=b"mp3-bytes"):
            await voice.send_voice_note(bot, 5, "hola")
        bot.send_voice.assert_awaited_once_with(5, b"mp3-bytes")

    async def test_a_failure_is_logged_never_raised(self):
        bot = mock.Mock(send_voice=mock.AsyncMock(side_effect=RuntimeError("upload died")))
        out = io.StringIO()
        with mock.patch.object(voice, "synthesize", return_value=b"mp3-bytes"):
            with contextlib.redirect_stdout(out):
                await voice.send_voice_note(bot, 5, "hola")
        self.assertIn("Failed to send voice note", out.getvalue())


class TranscriptEchoTests(unittest.TestCase):
    def test_reply_prefix_shows_what_was_heard(self):
        self.assertEqual(
            handlers.format_transcript("agrega leche y huevos"),
            "🎤 «agrega leche y huevos»\n\n",
        )


if __name__ == "__main__":
    unittest.main()
