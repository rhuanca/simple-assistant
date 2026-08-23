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
        self.assertEqual(
            agent.show_list.invoke({"scope": "personal"}), "🛒 My list — 1 item\n1. milk"
        )


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

    def test_remove_by_number(self):
        for item in ("pan", "leche", "huevos"):
            storage.add_item(item, owner_user_id=USER_ID)
        self.assertEqual(
            agent.remove_items_by_number.invoke({"numbers": [2], "scope": "personal"}),
            "✅ Removed from your list: leche",
        )
        self.assertEqual([i["item_text"] for i in storage.get_items(USER_ID)], ["pan", "huevos"])

    def test_remove_by_number_resolves_every_number_before_deleting(self):
        """Positions are resolved against one snapshot, so deleting 2 does not renumber 4
        out from under the same request."""
        for item in ("pan", "leche", "huevos", "queso"):
            storage.add_item(item, owner_user_id=USER_ID)
        self.assertEqual(
            agent.remove_items_by_number.invoke({"numbers": [2, 4], "scope": "personal"}),
            "✅ Removed from your list: leche, queso",
        )
        self.assertEqual([i["item_text"] for i in storage.get_items(USER_ID)], ["pan", "huevos"])

    def test_remove_by_number_reports_out_of_range(self):
        storage.add_item("pan", owner_user_id=USER_ID)
        reply = agent.remove_items_by_number.invoke({"numbers": [9], "scope": "personal"})

        self.assertEqual(reply, "⚠️ There is no item 9 — your list has 1 item.")
        self.assertEqual([i["item_text"] for i in storage.get_items(USER_ID)], ["pan"])

    def test_remove_by_number_ignores_a_repeated_number(self):
        for item in ("pan", "leche"):
            storage.add_item(item, owner_user_id=USER_ID)
        self.assertEqual(
            agent.remove_items_by_number.invoke({"numbers": [1, 1], "scope": "personal"}),
            "✅ Removed from your list: pan",
        )

    def test_remove_by_number_on_the_common_list(self):
        storage.add_item("mine", owner_user_id=USER_ID)
        storage.add_item("jabon", owner_user_id=None)
        self.assertEqual(
            agent.remove_items_by_number.invoke({"numbers": [1], "scope": "common"}),
            "✅ Removed from the common list: jabon",
        )
        self.assertEqual(storage.get_items(None), [])
        self.assertEqual(len(storage.get_items(USER_ID)), 1)

    def test_remove_by_number_numbers_only_the_acting_users_items(self):
        """Numbering comes from the same query show_list uses, so another person's items are
        not in it and cannot be reached by counting past the end of your own list."""
        storage.add_item("mine", owner_user_id=USER_ID)
        storage.add_item("theirs", owner_user_id=USER_ID + 1)

        reply = agent.remove_items_by_number.invoke({"numbers": [2], "scope": "personal"})
        self.assertIn("no item 2", reply)
        self.assertEqual([i["item_text"] for i in storage.get_items(USER_ID + 1)], ["theirs"])

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


class AppointmentToolTests(AgentTestCase):
    def _in_days(self, days, hour=15):
        from datetime import timedelta

        from bot import localtime

        moment = (localtime.now_local() + timedelta(days=days)).replace(
            hour=hour, minute=0, second=0, microsecond=0
        )
        return localtime.to_storage(moment), localtime.format_local(moment)

    def test_add_appointment_echoes_the_resolved_date(self):
        stored, shown = self._in_days(7)
        self.assertEqual(
            agent.add_appointment.invoke({"title": "doctor", "starts_at": stored}),
            f"📅 Saved: {shown} — doctor",
        )

    def test_add_appointment_rejects_an_unparseable_time(self):
        reply = agent.add_appointment.invoke({"title": "doctor", "starts_at": "next sunday"})
        self.assertIn("could not read", reply.lower())
        self.assertEqual(storage.get_upcoming_appointments(USER_ID, "0000"), [])

    def test_list_appointments_when_empty(self):
        self.assertEqual(agent.list_appointments.invoke({}), "📅 You have no upcoming appointments.")

    def test_list_appointments_is_numbered_and_soonest_first(self):
        later, later_shown = self._in_days(9)
        sooner, sooner_shown = self._in_days(2)
        storage.add_appointment("dentista", later, USER_ID)
        storage.add_appointment("doctor", sooner, USER_ID)

        self.assertEqual(
            agent.list_appointments.invoke({}),
            f"📅 Upcoming appointments — 2\n1. {sooner_shown} — doctor\n2. {later_shown} — dentista",
        )

    def test_list_appointments_hides_other_users(self):
        stored, _ = self._in_days(3)
        storage.add_appointment("secreto", stored, USER_ID + 1)
        self.assertEqual(agent.list_appointments.invoke({}), "📅 You have no upcoming appointments.")

    def test_cancel_appointment(self):
        stored, shown = self._in_days(4)
        storage.add_appointment("doctor", stored, USER_ID)

        self.assertEqual(
            agent.cancel_appointment.invoke({"title": "doctor"}),
            f"🗑️ Cancelled: {shown} — doctor",
        )
        self.assertEqual(storage.get_upcoming_appointments(USER_ID, "0000"), [])

    def test_cancel_appointment_with_no_match(self):
        reply = agent.cancel_appointment.invoke({"title": "doctor"})
        self.assertIn("No upcoming appointment matches", reply)

    def test_cancel_appointment_asks_when_several_match(self):
        first, _ = self._in_days(2)
        second, _ = self._in_days(5)
        storage.add_appointment("doctor Ruiz", first, USER_ID)
        storage.add_appointment("doctor Paz", second, USER_ID)

        reply = agent.cancel_appointment.invoke({"title": "doctor"})
        self.assertIn("which one", reply.lower())
        self.assertEqual(len(storage.get_upcoming_appointments(USER_ID, "0000")), 2)

    def test_cannot_cancel_another_users_appointment(self):
        stored, _ = self._in_days(3)
        storage.add_appointment("secreto", stored, USER_ID + 1)

        self.assertIn("No upcoming appointment matches", agent.cancel_appointment.invoke({"title": "secreto"}))
        self.assertEqual(len(storage.get_upcoming_appointments(USER_ID + 1, "0000")), 1)


class PromptContextTests(AgentTestCase):
    def test_prompt_tells_the_model_the_current_date(self):
        """Without this the model cannot resolve "next Sunday" at all."""
        prompt = agent._build_prompt("hola", "Renan", USER_ID)
        self.assertTrue(prompt.startswith("[Now: "))
        self.assertIn("America/La_Paz", prompt.splitlines()[0])


class ConversationMemoryTests(AgentTestCase):
    def test_history_is_replayed_and_capped(self):
        for i in range(5):
            agent._history[USER_ID] = (agent._history.get(USER_ID, []) + [(f"q{i}", f"a{i}")])[
                -agent._HISTORY_EXCHANGES :
            ]
        self.assertEqual(
            agent._history[USER_ID], [("q2", "a2"), ("q3", "a3"), ("q4", "a4")]
        )

    def test_clear_view_cache_also_clears_history(self):
        agent._history[USER_ID] = [("q", "a")]
        agent.clear_view_cache()
        self.assertEqual(len(agent._history), 0)


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

    def test_the_digest_never_carries_ids(self):
        """This one goes straight to Telegram without passing through _reply(), so ids here
        would reach the user for real."""
        storage.add_item("milk", owner_user_id=USER_ID)
        storage.add_item("soap", owner_user_id=None)
        self.assertNotIn("id=", agent.format_lists_for(USER_ID))


if __name__ == "__main__":
    unittest.main()
