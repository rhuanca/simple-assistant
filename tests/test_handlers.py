import asyncio
import contextlib
import io
import unittest
from types import SimpleNamespace

from telegram.error import Conflict, TimedOut

from bot import handlers


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
