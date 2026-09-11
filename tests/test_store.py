from dataclasses import replace
from pathlib import Path
import sqlite3
import json
import tempfile
import unittest
from unittest.mock import patch

from gemini_bot.backup import backup
from gemini_bot.config import Config
from gemini_bot.store import ShopError, Store

ADMIN, BUYER, OTHER = 100, 200, 300


def configuration(database=":memory:"):
    return Config(token="123456:" + "a" * 35, admin_ids=(ADMIN,), card="0000000000000000",
                  recipient="Test recipient", database=database, sales_open=True, admin_username="shop_admin")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(configuration())
        self.store.user(BUYER, "Buyer <tag>", source="ad_a")
        self.store.user(OTHER, "Other")
        # Treat fixture registrations as already notified, so the remaining
        # tests can inspect only the messages produced by their own actions.
        self.store.db.execute("UPDATE outbox SET sent_at=1")

    def tearDown(self):
        self.store.close()

    def order(self, uid=BUYER):
        return self.store.new_order(uid)[0]["id"]

    def reviewed(self, uid=BUYER):
        oid = self.order(uid)
        self.store.add_receipt(uid, oid, oid, f"file-{oid}", f"unique-{oid}", "photo")
        return oid

    def paid(self, uid=BUYER):
        oid = self.reviewed(uid)
        self.store.confirm_payment(ADMIN, oid, f"BANK-{oid}")
        return oid

    def test_order_is_reused_and_price_is_snapshotted(self):
        oid = self.order()
        self.store.set_setting(ADMIN, "price", 250)
        self.store.set_setting(ADMIN, "delivery_time", "Within one hour")
        order, created = self.store.new_order(BUYER)
        self.assertFalse(created)
        self.assertEqual((order["id"], order["price"]), (oid, 200))
        self.assertEqual(self.store.new_order(OTHER)[0]["price"], 250)

    def test_default_delivery_and_direct_contact(self):
        self.assertEqual(self.store.setting("delivery_time"), "1–5 хв")
        self.assertEqual(self.store.config.admin_contact_url, "https://t.me/shop_admin")
        self.assertEqual(replace(self.store.config, admin_username="").admin_contact_url,
                         f"tg://user?id={ADMIN}")

    def test_new_user_notification_contains_profile_and_first_source(self):
        self.assertTrue(self.store.user(400, "Олена <test>", "olena_test", "ad_campaign"))
        self.assertFalse(self.store.user(400, "Олена", "olena_new", "other_campaign"))
        rows = self.store.db.execute("SELECT * FROM outbox WHERE sent_at IS NULL").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["chat_id"], ADMIN)
        body = json.loads(rows[0]["payload"])
        self.assertIn("Олена &lt;test&gt;", body["text"])
        self.assertIn("@olena_test", body["text"])
        self.assertIn("<code>400</code>", body["text"])
        self.assertIn("ad_campaign", body["text"])
        self.assertNotIn("other_campaign", body["text"])
        self.assertEqual(body["reply_markup"]["inline_keyboard"][0][0]["url"],
                         "https://t.me/olena_test")
        user = self.store.db.execute("SELECT * FROM users WHERE id=400").fetchone()
        self.assertEqual((user["username"], user["source"]), ("olena_new", "ad_campaign"))

    def test_new_user_without_username_and_multiple_admins(self):
        self.store.config = replace(self.store.config, admin_ids=(ADMIN, 101))
        self.store.user(400, "Олена")
        rows = self.store.db.execute("SELECT * FROM outbox WHERE sent_at IS NULL").fetchall()
        self.assertEqual({row["chat_id"] for row in rows}, {ADMIN, 101})
        for row in rows:
            body = json.loads(row["payload"])
            self.assertIn("Username: не вказано", body["text"])
            self.assertIn("Прямий перехід", body["text"])
            self.assertEqual(body["reply_markup"]["inline_keyboard"][0][0]["url"], "tg://user?id=400")

    def test_admin_registration_does_not_create_join_notification(self):
        self.store.user(ADMIN, "Admin")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL").fetchone()[0], 0)

    def test_registration_rolls_back_if_notification_cannot_be_saved(self):
        with patch.object(self.store, "notify_admins", side_effect=RuntimeError("DB failure")):
            with self.assertRaises(RuntimeError):
                self.store.user(400, "New buyer")
        self.assertIsNone(self.store.db.execute("SELECT id FROM users WHERE id=400").fetchone())
        self.assertTrue(self.store.user(400, "New buyer"))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL").fetchone()[0], 1)

    def test_join_notification_survives_restart_without_repeating(self):
        with tempfile.TemporaryDirectory() as folder:
            config = configuration(str(Path(folder) / "shop.sqlite3"))
            store = Store(config)
            store.user(BUYER, "Buyer", source="first_ad")
            store.close()
            store = Store(config)
            try:
                self.assertFalse(store.user(BUYER, "Buyer", source="second_ad"))
                queued = store.due_messages()
                self.assertEqual(len(queued), 1)
                self.assertIn("first_ad", json.loads(queued[0]["payload"])["text"])
                store.delivered(queued[0]["id"])
                store.user(BUYER, "Buyer")
                self.assertEqual(store.due_messages(), [])
            finally:
                store.close()

    def test_existing_database_migrates_without_losing_orders(self):
        previous = "Час видачі уточнюйте в адміністратора перед оплатою."
        with tempfile.TemporaryDirectory() as folder:
            config = configuration(str(Path(folder) / "shop.sqlite3"))
            store = Store(config)
            store.user(BUYER, "Buyer")
            oid = store.new_order(BUYER)[0]["id"]
            store.set_setting(ADMIN, "delivery_time", previous)
            store.db.execute("UPDATE orders SET delivery_time=?", (previous,))
            store.queue("legacy", BUYER, previous, {"inline_keyboard": [[
                {"text": "Help", "callback_data": f"support:{oid}"},
                {"text": "Terms", "callback_data": "terms"}]]})
            store.db.execute("PRAGMA user_version=0")
            store.close()
            store = Store(config)
            try:
                self.assertEqual(store.own_order(BUYER, oid)["delivery_time"], "1–5 хв")
                self.assertEqual(store.setting("delivery_time"), "1–5 хв")
                body = json.loads(store.db.execute("SELECT payload FROM outbox WHERE event_key='legacy'").fetchone()[0])
                self.assertEqual(body["text"], "1–5 хв")
                self.assertEqual(body["reply_markup"]["inline_keyboard"], [[
                    {"text": "Help", "url": "https://t.me/shop_admin"}]])
                store.set_setting(ADMIN, "delivery_time", "10 хв")
                store.migrate_contact_and_delivery()
                self.assertEqual(store.setting("delivery_time"), "10 хв")
            finally:
                store.close()

    def test_pause_blocks_new_sales_but_keeps_existing(self):
        oid = self.order()
        self.store.set_setting(ADMIN, "sales_open", False)
        self.assertEqual(self.order(), oid)
        with self.assertRaises(ShopError):
            self.order(OTHER)
        self.store.add_receipt(BUYER, oid, 1, "file", "unique", "photo")

    def test_receipt_does_not_confirm_payment(self):
        oid = self.reviewed()
        order = self.store.own_order(BUYER, oid)
        self.assertEqual(order["status"], "review")
        self.assertIsNone(order["paid_at"])
        with self.assertRaises(ShopError):
            self.store.prepare_gift(ADMIN, oid, "https://example.com/gift/1")

    def test_payment_requires_receipt(self):
        oid = self.order()
        with self.assertRaises(ShopError):
            self.store.confirm_payment(ADMIN, oid, "BANK-123")

    def test_receipt_replay_and_rejection_resubmission(self):
        oid = self.reviewed()
        self.assertFalse(self.store.add_receipt(BUYER, oid, oid, "file", "unique", "photo"))
        self.store.reject_receipt(ADMIN, oid, "Unreadable")
        self.store.add_receipt(BUYER, oid, 50, "file2", "unique2", "document")
        self.assertEqual(len(self.store.receipts(ADMIN, oid)), 2)
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "review")

    def test_reused_receipt_is_rejected_across_buyers(self):
        oid = self.reviewed()
        other = self.order(OTHER)
        with self.assertRaises(ShopError):
            self.store.add_receipt(OTHER, other, 1, "other-file", f"unique-{oid}", "photo")
        self.assertEqual(self.store.own_order(OTHER, other)["status"], "awaiting_payment")

    def test_payment_reference_is_unique_and_normalized(self):
        self.paid()
        other = self.reviewed(OTHER)
        with self.assertRaises(ShopError):
            self.store.confirm_payment(ADMIN, other, "  bank-1 ")
        self.assertEqual(self.store.own_order(OTHER, other)["status"], "review")

    def test_payment_can_be_confirmed_without_bank_reference_for_multiple_orders(self):
        for uid in (BUYER, OTHER):
            oid = self.reviewed(uid)
            self.store.confirm_payment(ADMIN, oid)
            order = self.store.admin_order(ADMIN, oid)
            self.assertEqual(order["status"], "paid")
            self.assertIsNone(order["payment_ref"])
            self.assertEqual(order["paid_by"], ADMIN)
            self.assertIsNotNone(order["paid_at"])

    def test_payment_cannot_be_confirmed_twice(self):
        oid = self.paid()
        before = self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        with self.assertRaises(ShopError):
            self.store.confirm_payment(ADMIN, oid, "bank-new")
        self.assertEqual(before, self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    def test_payment_notifies_buyer_and_other_admins_but_not_confirming_admin(self):
        self.store.config = replace(self.store.config, admin_ids=(ADMIN, 400))
        oid = self.reviewed()
        self.store.confirm_payment(ADMIN, oid)
        recipients = [row[0] for row in self.store.db.execute(
            "SELECT chat_id FROM outbox WHERE event_key=? OR event_key LIKE ?",
            (f"paid:{oid}", f"paid:{oid}:admin:%"))]
        self.assertCountEqual(recipients, [BUYER, 400])

    def test_all_admin_actions_reject_buyer(self):
        oid = self.reviewed()
        operations = [
            lambda: self.store.confirm_payment(BUYER, oid, "bank-ref"),
            lambda: self.store.reject_receipt(BUYER, oid, "reason"),
            lambda: self.store.prepare_gift(BUYER, oid, "https://example.com/gift"),
            lambda: self.store.issue_gift(BUYER, oid),
            lambda: self.store.refund(BUYER, oid, "refund-ref"),
            lambda: self.store.admin_order(BUYER, oid),
            lambda: self.store.receipts(BUYER, oid),
            lambda: self.store.set_setting(BUYER, "sales_open", False),
            lambda: self.store.stats(BUYER),
            lambda: self.store.retry_delivery(BUYER),
            lambda: self.store.list_orders(BUYER, admin=True),
        ]
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(ShopError):
                operation()

    def test_ownership_checked_for_all_buyer_actions(self):
        oid = self.order()
        operations = [
            lambda: self.store.own_order(OTHER, oid),
            lambda: self.store.cancel_order(OTHER, oid),
            lambda: self.store.activate(OTHER, oid),
            lambda: self.store.add_receipt(OTHER, oid, 2, "file", "uid", "photo"),
            lambda: self.store.submit_support(OTHER, oid, 3, {"kind": "text", "text": "Hi"}),
        ]
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(ShopError):
                operation()

    def test_gift_requires_preview_and_one_delivery_record(self):
        oid = self.paid()
        with self.assertRaises(ShopError):
            self.store.issue_gift(ADMIN, oid, (self.store.session(ADMIN) or ("", {}))[1].get("nonce"))
        self.store.prepare_gift(ADMIN, oid, "https://example.com/gift/1")
        self.store.issue_gift(ADMIN, oid, (self.store.session(ADMIN) or ("", {}))[1].get("nonce"))
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "delivery_pending")
        with self.assertRaises(ShopError):
            self.store.issue_gift(ADMIN, oid, (self.store.session(ADMIN) or ("", {}))[1].get("nonce"))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox WHERE purpose='gift'").fetchone()[0], 1)
        row = self.store.db.execute("SELECT id FROM outbox WHERE purpose='gift'").fetchone()
        self.store.delivered(row[0])
        self.store.delivered(row[0])
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "issued")
        self.store.activate(BUYER, oid)
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "activated")

    def test_same_gift_cannot_be_assigned_twice_even_with_two_previews(self):
        one = self.paid()
        two = self.paid(OTHER)
        self.store.prepare_gift(ADMIN, one, "https://EXAMPLE.com/gift/same")
        self.store.issue_gift(ADMIN, one, self.store.session(ADMIN)[1]["nonce"])
        with self.assertRaises(ShopError):
            self.store.prepare_gift(ADMIN, two, "https://example.com:443/gift/same")

    def test_refund_invalidates_old_gift_preview(self):
        oid = self.paid()
        self.store.prepare_gift(ADMIN, oid, "https://example.com/gift/old")
        self.store.refund(ADMIN, oid, "refund-123")
        with self.assertRaises(ShopError):
            self.store.issue_gift(ADMIN, oid, (self.store.session(ADMIN) or ("", {}))[1].get("nonce"))
        stats = self.store.stats(ADMIN)
        self.assertEqual(stats["money"], {"gross": 200, "refunds": 200})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox WHERE purpose='gift'").fetchone()[0], 0)

    def test_gift_validation(self):
        for url in ("http://example.com/gift", "https://localhost/gift", "https://127.0.0.1/gift",
                    "https://example.com@evil.test/gift", "javascript:alert(1)", "https://example.com:bad/gift",
                    "https://example.com:8443/gift", "https://example.com/a\nb"):
            with self.subTest(url=url), self.assertRaises(ShopError):
                self.store.validate_gift(url)
        store = Store(replace(configuration(), allowed_hosts=("one.google.com",)))
        try:
            self.assertEqual(store.validate_gift("https://one.google.com/gift?token=abc"), "https://one.google.com/gift?token=abc")
            with self.assertRaises(ShopError):
                store.validate_gift("https://one.google.com.evil.test/gift")
        finally:
            store.close()

    def test_sessions_and_queue_survive_restart_and_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "shop.sqlite3")
            store = Store(configuration(path))
            store.user(BUYER, "Buyer")
            oid = store.new_order(BUYER)[0]["id"]
            store.set_session(BUYER, "receipt", {"oid": oid})
            store.save_screen(BUYER, {"message_id": 123, "extras": [], "buyer_orders": "orders:2"})
            store.queue("test", BUYER, "Hello")
            target = backup(path, Path(folder) / "backup.sqlite3")
            store.close()
            store = Store(configuration(path))
            try:
                self.assertEqual(store.session(BUYER), ("receipt", {"oid": oid}))
                self.assertEqual(store.screen(BUYER)["message_id"], 123)
                self.assertEqual(store.screen(BUYER)["buyer_orders"], "orders:2")
                self.assertEqual(len([r for r in store.due_messages() if r["event_key"] == "test"]), 1)
                self.assertEqual(store.own_order(BUYER, oid)["price"], 200)
                copy = sqlite3.connect(target)
                try:
                    self.assertEqual(copy.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
                finally:
                    copy.close()
            finally:
                store.close()

    def test_outbox_failure_does_not_mark_gift_issued(self):
        oid = self.paid()
        self.store.prepare_gift(ADMIN, oid, "https://example.com/gift/fail")
        self.store.issue_gift(ADMIN, oid, (self.store.session(ADMIN) or ("", {}))[1].get("nonce"))
        row = self.store.db.execute("SELECT id FROM outbox WHERE purpose='gift'").fetchone()
        self.store.delivery_error(row[0], "TelegramForbiddenError", permanent=True)
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "delivery_pending")
        self.assertEqual(self.store.stats(ADMIN)["parked"], 1)
        self.store.resume_recipient(BUYER)
        self.assertEqual(self.store.stats(ADMIN)["parked"], 0)

    def test_outbox_preserves_order_and_does_not_block_other_buyers(self):
        self.store.queue("first", BUYER, "one")
        self.store.queue("second", BUYER, "two")
        self.store.queue("other", OTHER, "three")
        rows = self.store.due_messages()
        self.assertEqual([r["event_key"] for r in rows], ["first", "other"])
        self.store.delivery_error(rows[0]["id"], "NetworkError", delay=100)
        self.assertEqual([r["event_key"] for r in self.store.due_messages()], ["other"])

    def test_support_is_persistent_deduplicated_and_admin_only(self):
        oid = self.order()
        body = {"kind": "text", "text": "Hello <b>world</b>"}
        tid = self.store.submit_support(BUYER, oid, 99, body)
        self.assertEqual(self.store.submit_support(BUYER, oid, 99, body), tid)
        with self.assertRaises(ShopError):
            self.store.reply_support(OTHER, tid, 100, body)
        self.store.reply_support(ADMIN, tid, 100, body)
        self.assertEqual(len(self.store.ticket_messages(ADMIN, tid)), 2)
        self.store.close_ticket(ADMIN, tid)
        with self.assertRaises(ShopError):
            self.store.reply_support(ADMIN, tid, 101, body)

    def test_source_is_first_touch_and_stats_count_orders(self):
        self.store.user(BUYER, "New name", source="different_ad")
        oid = self.paid()
        self.assertEqual(self.store.own_order(BUYER, oid)["source"], "ad_a")
        sources = {r["source"]: r for r in self.store.stats(ADMIN)["sources"]}
        self.assertEqual(sources["ad_a"]["payments"], 1)

    def test_session_expiry_prevents_old_confirmation(self):
        oid = self.paid()
        self.store.prepare_gift(ADMIN, oid, "https://example.com/gift/expired")
        self.store.db.execute("UPDATE sessions SET created_at=0")
        with self.assertRaises(ShopError):
            self.store.issue_gift(ADMIN, oid, (self.store.session(ADMIN) or ("", {}))[1].get("nonce"))

    def test_old_gift_preview_cannot_send_new_link(self):
        oid = self.paid()
        self.store.prepare_gift(ADMIN, oid, "https://example.com/first")
        nonce = self.store.session(ADMIN)[1]["nonce"]
        self.store.prepare_gift(ADMIN, oid, "https://example.com/second")
        with self.assertRaises(ShopError):
            self.store.issue_gift(ADMIN, oid, nonce)
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "paid")

    def test_changed_receipt_invalidates_admin_confirmation(self):
        oid = self.reviewed()
        old = self.store.receipts(ADMIN, oid)[-1]["id"]
        self.store.reject_receipt(ADMIN, oid, "Unreadable")
        self.store.add_receipt(BUYER, oid, 123, "newfile", "newunique", "photo")
        with self.assertRaises(ShopError):
            self.store.confirm_payment(ADMIN, oid, "BANK-REF", old)
        with self.assertRaises(ShopError):
            self.store.reject_receipt(ADMIN, oid, "Wrong", old)
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "review")

    def test_two_admins_cannot_issue_same_gift_after_both_preview(self):
        self.store.config = replace(self.store.config, admin_ids=(ADMIN, 101))
        one, two = self.paid(), self.paid(OTHER)
        self.store.prepare_gift(ADMIN, one, "https://example.com/same")
        self.store.prepare_gift(101, two, "https://example.com/same")
        self.store.issue_gift(ADMIN, one, self.store.session(ADMIN)[1]["nonce"])
        with self.assertRaises(ShopError):
            self.store.issue_gift(101, two, self.store.session(101)[1]["nonce"])
        self.assertEqual(self.store.own_order(OTHER, two)["status"], "paid")


if __name__ == "__main__":
    unittest.main()
