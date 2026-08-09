import tempfile
import unittest
from pathlib import Path

from bot import storage


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Point storage at a throwaway DB (functions read the module global at call time).
        self._orig_db = storage.DB_PATH
        storage.DB_PATH = Path(self._tmp.name) / "test.db"
        self.addCleanup(lambda: setattr(storage, "DB_PATH", self._orig_db))
        storage.init_db()


class ItemOwnershipTests(StorageTestCase):
    def test_personal_and_common_are_isolated(self):
        storage.add_item("milk", owner_user_id=1)
        storage.add_item("bread", owner_user_id=2)
        storage.add_item("soap", owner_user_id=None)  # common

        self.assertEqual([i["item_text"] for i in storage.get_items(1)], ["milk"])
        self.assertEqual([i["item_text"] for i in storage.get_items(2)], ["bread"])
        self.assertEqual([i["item_text"] for i in storage.get_items(None)], ["soap"])

    def test_remove_respects_owner(self):
        storage.add_item("milk", owner_user_id=1)
        storage.add_item("milk", owner_user_id=None)

        # Removing from user 1's list must not touch the common list.
        self.assertTrue(storage.remove_item("milk", owner_user_id=1))
        self.assertEqual(storage.get_items(1), [])
        self.assertEqual([i["item_text"] for i in storage.get_items(None)], ["milk"])

    def test_remove_missing_returns_false(self):
        self.assertFalse(storage.remove_item("ghost", owner_user_id=1))

    def test_clear_respects_owner(self):
        storage.add_item("a", owner_user_id=1)
        storage.add_item("b", owner_user_id=1)
        storage.add_item("c", owner_user_id=None)

        self.assertEqual(storage.clear_list(owner_user_id=1), 2)
        self.assertEqual(storage.get_items(1), [])
        self.assertEqual(len(storage.get_items(None)), 1)


class SettingsTests(StorageTestCase):
    def test_defaults_returned_when_unset(self):
        self.assertEqual(storage.get_setting("alert_enabled"), "true")
        self.assertEqual(storage.get_setting("alert_interval_days"), "3")

    def test_set_and_get_roundtrip(self):
        storage.set_setting("alert_interval_days", "5")
        self.assertEqual(storage.get_setting("alert_interval_days"), "5")

    def test_unknown_key_uses_provided_default(self):
        self.assertEqual(storage.get_setting("nope", "fallback"), "fallback")


class AdminHelperTests(StorageTestCase):
    def _add_user(self, uid, chat_id, username, admin=False):
        storage.upsert_user(uid, chat_id, username=username, first_name=username.title())
        storage.allow_chat(chat_id)
        if admin:
            storage.promote_to_admin(uid)

    def test_is_admin_and_roles(self):
        self._add_user(1, 100, "renan", admin=True)
        self._add_user(2, 200, "alice")
        self.assertTrue(storage.is_admin(1))
        self.assertFalse(storage.is_admin(2))
        self.assertFalse(storage.is_admin(999))

    def test_find_user_by_username_is_case_insensitive(self):
        self._add_user(2, 200, "Alice")
        found = storage.find_user_by_username("@alice")
        self.assertIsNotNone(found)
        self.assertEqual(found["telegram_user_id"], 2)
        self.assertIsNone(storage.find_user_by_username("bob"))

    def test_set_role(self):
        self._add_user(2, 200, "alice")
        self.assertTrue(storage.set_role(2, storage.ADMIN_USER_ROLE))
        self.assertTrue(storage.is_admin(2))
        self.assertFalse(storage.set_role(999, storage.ADMIN_USER_ROLE))

    def test_get_all_users(self):
        self._add_user(1, 100, "renan", admin=True)
        self._add_user(2, 200, "alice")
        self.assertEqual({u["telegram_user_id"] for u in storage.get_all_users()}, {1, 2})

    def test_revoke_user_removes_user_and_chat(self):
        self._add_user(2, 200, "alice")
        self.assertTrue(storage.is_chat_allowed(200))
        self.assertTrue(storage.revoke_user(2))
        self.assertIsNone(storage.find_user_by_username("alice"))
        self.assertFalse(storage.is_chat_allowed(200))
        self.assertFalse(storage.revoke_user(2))  # already gone


if __name__ == "__main__":
    unittest.main()
