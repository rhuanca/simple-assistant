import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from bot import agent, storage

USER_ID = 42


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_db = storage.DB_PATH
        storage.DB_PATH = Path(self._tmp.name) / "test.db"
        self.addCleanup(lambda: setattr(storage, "DB_PATH", self._orig_db))
        storage.init_db()

        token = agent._current_user_id.set(USER_ID)
        self.addCleanup(lambda: agent._current_user_id.reset(token))
        agent._view_cache.clear()


class ExtractTextTests(unittest.TestCase):
    """The bug behind the "OK" reply: an empty final model turn must still say something."""

    def test_model_text_wins(self):
        result = {"messages": [ToolMessage("🛒 My list is empty.", tool_call_id="1"),
                               AIMessage("Your list is empty.")]}
        self.assertEqual(agent._extract_text(result), "Your list is empty.")

    def test_empty_final_turn_falls_back_to_tool_output(self):
        result = {"messages": [HumanMessage("show me my list"),
                               ToolMessage("🛒 My list is empty.", tool_call_id="1"),
                               AIMessage("")]}
        self.assertEqual(agent._extract_text(result), "🛒 My list is empty.")

    def test_falls_back_to_the_last_tool_output(self):
        result = {"messages": [ToolMessage("Cache stale: id=1", tool_call_id="1"),
                               ToolMessage("🛒 My list — 1 item\n1. milk", tool_call_id="2"),
                               AIMessage("   ")]}
        self.assertEqual(agent._extract_text(result), "🛒 My list — 1 item\n1. milk")

    def test_content_blocks_are_joined_and_non_dict_blocks_ignored(self):
        message = AIMessage([{"type": "text", "text": "Here: "}, "raw", {"type": "thinking"},
                             {"type": "text", "text": "milk"}])
        self.assertEqual(agent._extract_text({"messages": [message]}), "Here: milk")

    def test_no_text_and_no_tool_output_still_replies(self):
        result = {"messages": [AIMessage("")]}
        self.assertEqual(agent._extract_text(result), "Done. / Listo.")

    def test_internal_ids_are_never_shown_to_the_user(self):
        """The model sometimes echoes the [Recently viewed list] block, ids and all."""
        leaked = AIMessage("Tu lista personal:\n1. jabon (id=4)\n2. leche (id=5)")
        self.assertEqual(
            agent._extract_text({"messages": [leaked]}),
            "Tu lista personal:\n1. jabon\n2. leche",
        )

    def test_ids_are_stripped_from_the_tool_fallback_too(self):
        result = {"messages": [ToolMessage("1. pan (id=7)", tool_call_id="1"), AIMessage("")]}
        self.assertEqual(agent._extract_text(result), "1. pan")


class ScopeTests(AgentTestCase):
    def test_common_scope_maps_to_the_shared_owner(self):
        self.assertIsNone(agent._owner_for_scope("common"))

    def test_personal_scope_maps_to_the_acting_user(self):
        self.assertEqual(agent._owner_for_scope("personal"), USER_ID)


class ShowListTests(AgentTestCase):
    def test_empty_personal_list(self):
        self.assertEqual(agent.show_list.invoke({"scope": "personal"}), "🛒 My list is empty.")

    def test_empty_common_list(self):
        self.assertEqual(agent.show_list.invoke({"scope": "common"}), "🏠 Common list is empty.")

    def test_numbered_rendering(self):
        for item in ("milk", "bread", "eggs"):
            storage.add_item(item, owner_user_id=USER_ID)
        self.assertEqual(
            agent.show_list.invoke({"scope": "personal"}),
            "🛒 My list — 3 items\n1. milk\n2. bread\n3. eggs",
        )

    def test_single_item_is_not_pluralised(self):
        storage.add_item("milk", owner_user_id=USER_ID)
        self.assertIn("1 item\n", agent.show_list.invoke({"scope": "personal"}))

    def test_numbers_match_the_cached_view(self):
        """Deleting by number depends on show_list and the cached view agreeing."""
        for item in ("milk", "bread", "eggs"):
            storage.add_item(item, owner_user_id=USER_ID)
        rendered = agent.show_list.invoke({"scope": "personal"}).splitlines()[1:]
        view = agent._get_cached_view(USER_ID)
        self.assertEqual(
            rendered,
            [f"{row['number']}. {row['text']}" for row in view["items"]],
        )

    def test_shows_only_the_acting_users_items(self):
        storage.add_item("milk", owner_user_id=USER_ID)
        storage.add_item("someone else's", owner_user_id=USER_ID + 1)
        self.assertEqual(agent.show_list.invoke({"scope": "personal"}), "🛒 My list — 1 item\n1. milk")


class MutationReplyTests(AgentTestCase):
    def test_add_items_names_the_list_and_the_items(self):
        self.assertEqual(
            agent.add_items.invoke({"items": ["milk", "eggs"], "scope": "personal"}),
            "✅ Added to your list: milk, eggs",
        )
        self.assertEqual(
            agent.add_items.invoke({"items": ["soap"], "scope": "common"}),
            "✅ Added to the common list: soap",
        )

    def test_remove_items_reports_removed_and_missing(self):
        storage.add_item("milk", owner_user_id=USER_ID)
        self.assertEqual(
            agent.remove_items.invoke({"items": ["milk", "soap"], "scope": "personal"}),
            "✅ Removed from your list: milk\n⚠️ Not found: soap",
        )

    def test_remove_items_by_id(self):
        item_id = storage.add_item("milk", owner_user_id=USER_ID)
        self.assertEqual(
            agent.remove_items_by_id.invoke({"ids": [item_id], "texts": ["milk"]}),
            "✅ Removed: milk",
        )

    def test_remove_items_by_id_cannot_touch_another_users_item(self):
        theirs = storage.add_item("secret", owner_user_id=USER_ID + 1)
        reply = agent.remove_items_by_id.invoke({"ids": [theirs], "texts": ["secret"]})

        self.assertNotIn("Removed", reply)
        self.assertEqual([i["item_text"] for i in storage.get_items(USER_ID + 1)], ["secret"])

    def test_remove_items_by_id_works_on_the_common_list(self):
        common = storage.add_item("soap", owner_user_id=None)
        self.assertEqual(
            agent.remove_items_by_id.invoke({"ids": [common], "texts": ["soap"]}),
            "✅ Removed: soap",
        )
        self.assertEqual(storage.get_items(None), [])

    def test_clear_list_reports_the_count(self):
        for item in ("milk", "bread"):
            storage.add_item(item, owner_user_id=USER_ID)
        self.assertEqual(
            agent.clear_list.invoke({"scope": "personal"}),
            "🗑️ Cleared 2 items from your list.",
        )

    def test_clear_empty_list_says_so(self):
        self.assertEqual(
            agent.clear_list.invoke({"scope": "personal"}),
            "Nothing to clear — your list was already empty.",
        )


class ModelConfigTests(unittest.TestCase):
    def test_thinking_is_disabled(self):
        """With thinking on, gemini-2.5-flash regularly returns 0 output tokens and no tool
        call, which is what made "muestrame mi lista" answer with nothing. See _get_agent."""
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            model = agent._build_model()
        self.assertEqual(model.thinking_budget, 0)


class AlertRenderingTests(AgentTestCase):
    def test_returns_none_when_both_lists_are_empty(self):
        self.assertIsNone(agent.format_lists_for(USER_ID))

    def test_renders_both_lists(self):
        storage.add_item("milk", owner_user_id=USER_ID)
        message = agent.format_lists_for(USER_ID)
        self.assertIn("🛒 My list — 1 item\n1. milk", message)
        self.assertIn("🏠 Common list is empty.", message)


if __name__ == "__main__":
    unittest.main()
