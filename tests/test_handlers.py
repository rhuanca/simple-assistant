import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from telegram.error import Conflict, TimedOut

from bot import handlers, storage


class FakeChat:
    def __init__(self):
        self.actions = []

    async def send_action(self, action):
        self.actions.append(action)


class WithTypingTests(unittest.IsolatedAsyncioTestCase):
    """Telegram shows a chat action for ~5 seconds only; slow work must refresh it or the
    dots die mid-wait and the chat looks dead."""

    async def test_returns_the_result_and_shows_typing(self):
        chat = FakeChat()

        async def quick():
            return 42

        self.assertEqual(await handlers._with_typing(chat, quick()), 42)
        self.assertEqual(chat.actions, ["typing"])

    async def test_keeps_the_indicator_alive_while_the_work_runs(self):
        chat = FakeChat()

        async def slow():
            await asyncio.sleep(0.05)
            return "ok"

        result = await handlers._with_typing(chat, slow(), refresh=0.01)
        self.assertEqual(result, "ok")
        self.assertGreaterEqual(len(chat.actions), 3)

    async def test_exceptions_from_the_work_propagate(self):
        chat = FakeChat()

        async def boom():
            raise RuntimeError("agent failed")

        with self.assertRaises(RuntimeError):
            await handlers._with_typing(chat, boom())


class _ReplyChat:
    def __init__(self, chat_id):
        self.id = chat_id
        self.actions = []

    async def send_action(self, action):
        self.actions.append(action)


class _ReplyMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class _VoiceBot:
    def __init__(self):
        self.voices = []

    async def send_voice(self, chat_id, audio):
        self.voices.append((chat_id, audio))


class VoiceReplyTests(unittest.IsolatedAsyncioTestCase):
    """The voice_replies setting: every agent reply also goes out as a voice note."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_db = storage.DB_PATH
        storage.DB_PATH = Path(self._tmp.name) / "test.db"
        self.addCleanup(lambda: setattr(storage, "DB_PATH", self._orig_db))
        storage.init_db()

    async def _reply(self, flag=None, configured=True):
        if flag is not None:
            storage.set_setting("voice_replies", flag)
        update = SimpleNamespace(
            effective_chat=_ReplyChat(100),
            effective_user=SimpleNamespace(id=1, first_name="Renan"),
            message=_ReplyMessage(),
        )
        context = SimpleNamespace(bot=_VoiceBot())

        async def fake_run(text, user="", user_id=None):
            return "✅ Agregado a tu lista: leche"

        with mock.patch.object(handlers, "run", fake_run):
            with mock.patch.object(handlers.voice, "is_configured", return_value=configured):
                with mock.patch.object(
                    handlers.voice, "synthesize", return_value=b"mp3-bytes"
                ) as synthesize:
                    await handlers._run_and_reply(update, context, "agrega leche")
        return update, context, synthesize

    async def test_flag_on_speaks_the_reply_without_emoji(self):
        update, context, synthesize = await self._reply(flag="true")

        self.assertEqual(update.message.replies, ["✅ Agregado a tu lista: leche"])
        self.assertEqual(context.bot.voices, [(100, b"mp3-bytes")])
        synthesize.assert_called_once_with("Agregado a tu lista: leche")

    async def test_off_by_default(self):
        _, context, synthesize = await self._reply()
        self.assertEqual(context.bot.voices, [])
        synthesize.assert_not_called()

    async def test_flag_without_a_speech_server_stays_text_only(self):
        update, context, synthesize = await self._reply(flag="true", configured=False)
        self.assertEqual(update.message.replies, ["✅ Agregado a tu lista: leche"])
        self.assertEqual(context.bot.voices, [])
        synthesize.assert_not_called()


class _StatusMessage:
    """The placeholder the voice handler sends and then edits in place."""

    def __init__(self):
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)


class _VoiceUserMessage:
    def __init__(self):
        self.replies = []
        self.status = _StatusMessage()
        file = SimpleNamespace(
            download_as_bytearray=mock.AsyncMock(return_value=bytearray(b"OggS"))
        )
        self.voice = SimpleNamespace(get_file=mock.AsyncMock(return_value=file))

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return self.status


class VoiceStagedStatusTests(unittest.IsolatedAsyncioTestCase):
    """A voice note gets one placeholder message that morphs: Escuchando → transcript +
    Pensando → the answer. The user always sees progress in the chat body."""

    CHAT_ID = 100

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_db = storage.DB_PATH
        storage.DB_PATH = Path(self._tmp.name) / "test.db"
        self.addCleanup(lambda: setattr(storage, "DB_PATH", self._orig_db))
        storage.init_db()
        storage.allow_chat(self.CHAT_ID)

    async def _handle(self, transcribe):
        update = SimpleNamespace(
            effective_chat=_ReplyChat(self.CHAT_ID),
            effective_user=SimpleNamespace(id=1, first_name="Renan"),
            message=_VoiceUserMessage(),
        )
        context = SimpleNamespace(bot=_VoiceBot())

        async def fake_run(text, user="", user_id=None):
            return "✅ Agregado a tu lista: leche"

        with mock.patch.object(handlers, "run", fake_run):
            with mock.patch.object(handlers.voice, "is_configured", return_value=True):
                with mock.patch.object(handlers.voice, "transcribe", transcribe):
                    await handlers.handle_voice(update, context)
        return update

    async def test_placeholder_morphs_through_the_stages_into_the_answer(self):
        update = await self._handle(mock.Mock(return_value="agrega leche"))

        self.assertEqual(update.message.replies, [handlers.VOICE_LISTENING])
        self.assertEqual(update.message.status.edits, [
            "🎤 «agrega leche»\n\n" + handlers.VOICE_THINKING,
            "🎤 «agrega leche»\n\n✅ Agregado a tu lista: leche",
        ])

    async def test_transcription_failure_morphs_into_the_error(self):
        update = await self._handle(mock.Mock(side_effect=RuntimeError("stt down")))

        self.assertEqual(update.message.replies, [handlers.VOICE_LISTENING])
        self.assertEqual(update.message.status.edits, [handlers.VOICE_FAILED])

    async def test_silence_morphs_into_not_understood(self):
        update = await self._handle(mock.Mock(return_value=""))
        self.assertEqual(update.message.status.edits, [handlers.VOICE_NOT_UNDERSTOOD])


class ErrorHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def _handle(self, error) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await handlers.on_error(None, SimpleNamespace(error=error))
        return out.getvalue()

    async def test_conflict_collapses_to_one_pointed_line(self):
        logged = await self._handle(Conflict("terminated by other getUpdates request"))
        self.assertIn("another instance is polling", logged)
        self.assertNotIn("Traceback", logged)
        self.assertEqual(len(logged.strip().splitlines()), 1)

    async def test_timeouts_collapse_to_one_line(self):
        logged = await self._handle(TimedOut())
        self.assertIn("network hiccup", logged)
        self.assertEqual(len(logged.strip().splitlines()), 1)

    async def test_unexpected_errors_keep_the_full_traceback(self):
        try:
            raise RuntimeError("something real broke")
        except RuntimeError as exc:
            error = exc
        logged = await self._handle(error)
        self.assertIn("something real broke", logged)


if __name__ == "__main__":
    unittest.main()
