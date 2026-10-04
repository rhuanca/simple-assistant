import asyncio
import unittest

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


if __name__ == "__main__":
    unittest.main()
