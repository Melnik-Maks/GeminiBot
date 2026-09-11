import asyncio
from datetime import datetime, timezone
import unittest

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import (AnswerCallbackQuery, DeleteMessage, EditMessageReplyMarkup,
                             EditMessageText, SendDocument, SendMessage, SendPhoto)
from aiogram.types import Chat, Message, Update

from gemini_bot.delivery import deliver_batch
from gemini_bot.store import Store
from gemini_bot.ui import UI, attachment
from tests.test_store import ADMIN, BUYER, OTHER, configuration


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail_gift = None
        self.messages = {}
        self.edit_error = None
        self.delete_error = None

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, DeleteMessage):
            if self.delete_error:
                raise TelegramBadRequest(method=method, message=self.delete_error)
            self.messages.pop((int(method.chat_id), method.message_id), None)
            return True
        if isinstance(method, (EditMessageText, EditMessageReplyMarkup)):
            key = (int(method.chat_id), method.message_id)
            if isinstance(method, EditMessageText) and self.edit_error:
                raise TelegramBadRequest(method=method, message=self.edit_error)
            if key not in self.messages:
                raise TelegramBadRequest(method=method, message="message to edit not found")
            changes = {"reply_markup": method.reply_markup}
            if isinstance(method, EditMessageText):
                changes["text"] = method.text
            result = self.messages[key].model_copy(update=changes)
            if result == self.messages[key]:
                raise TelegramBadRequest(method=method, message="message is not modified")
            self.messages[key] = result
            return result
        if self.fail_gift and isinstance(method, SendMessage) and "example.com/gift" in method.text:
            raise self.fail_gift
        if isinstance(method, (SendMessage, SendPhoto, SendDocument)):
            result = Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                             chat=Chat(id=int(method.chat_id), type="private"), text=getattr(method, "text", None),
                             caption=getattr(method, "caption", None), reply_markup=method.reply_markup)
            self.messages[(int(method.chat_id), result.message_id)] = result
            return result
        raise AssertionError(type(method).__name__)

    async def stream_content(self, *args, **kwargs):
        yield b""


class BotFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(configuration())
        self.session = FakeSession()
        self.bot = Bot(configuration().token, session=self.session)
        self.ui = UI(self.bot, self.store)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(self.ui.router)
        self.counter = 0

    async def asyncTearDown(self):
        self.store.close()
        await self.bot.session.close()

    async def event(self, uid, text=None, callback=None, photo=False, document=None, group=False, clicked=None):
        self.counter += 1
        self.ui.last_action.clear()
        user = {"id": uid, "is_bot": False, "first_name": f"User {uid}"}
        message = {"message_id": self.counter, "date": int(datetime.now(timezone.utc).timestamp()),
                   "chat": {"id": uid if not group else -100123, "type": "private" if not group else "supergroup"},
                   "from": user}
        if text is not None:
            message["text"] = text
        if photo:
            message["photo"] = [{"file_id": f"photo-{self.counter}", "file_unique_id": f"unique-{self.counter}",
                                 "width": 100, "height": 100, "file_size": 1000}]
        if document:
            message["document"] = document
        if callback:
            if clicked is None:
                screen_id = self.store.screen(uid).get("message_id")
                clicked = self.session.messages.get((uid, screen_id))
            if clicked is None:
                clicked = await self.bot.send_message(uid, "Поточне повідомлення")
            message = clicked.model_dump(mode="json", by_alias=True, exclude_none=True)
            raw = {"update_id": self.counter, "callback_query": {
                "id": str(self.counter), "from": user, "chat_instance": "test",
                "message": message, "data": callback}}
        else:
            raw = {"update_id": self.counter, "message": message}
        await self.dispatcher.feed_update(self.bot, Update.model_validate(raw))

    async def drain(self):
        for _ in range(40):
            if not await deliver_batch(self.bot, self.store):
                return
        self.fail("Outbox failed to drain")

    def panel(self, uid):
        return self.session.messages[(uid, self.store.screen(uid)["message_id"])]

    async def test_menu_navigation_edits_one_message_and_goes_back(self):
        await self.event(BUYER, "/start")
        message_id = self.panel(BUYER).message_id
        await self.event(BUYER, callback="instructions")
        self.assertEqual(self.panel(BUYER).message_id, message_id)
        back = next(b for row in self.panel(BUYER).reply_markup.inline_keyboard for b in row if b.text == "← Назад")
        await self.event(BUYER, callback=back.callback_data)
        self.assertIn("Вартість", self.panel(BUYER).text)
        self.assertEqual(self.panel(BUYER).message_id, message_id)
        self.assertEqual(len([m for m in self.session.calls if isinstance(m, SendMessage) and m.chat_id == BUYER]), 1)

    async def test_start_and_menu_send_visible_reply_then_buttons_edit_it(self):
        await self.event(BUYER, "/start")
        for command in ("/start", "/menu"):
            previous = self.panel(BUYER).message_id
            before = len(self.session.calls)
            await self.event(BUYER, command)
            current = self.panel(BUYER).message_id
            self.assertNotEqual(current, previous)
            self.assertNotIn((BUYER, previous), self.session.messages)
            calls = self.session.calls[before:]
            self.assertEqual(len([m for m in calls if isinstance(m, SendMessage) and m.chat_id == BUYER]), 1)
            self.assertLess(next(i for i, m in enumerate(calls) if isinstance(m, SendMessage)),
                            next(i for i, m in enumerate(calls) if isinstance(m, DeleteMessage)))
            await self.event(BUYER, callback="instructions")
            self.assertEqual(self.panel(BUYER).message_id, current)

    async def test_receipt_back_clears_input_without_cancelling_order(self):
        await self.event(BUYER, "/start")
        await self.event(BUYER, callback="buy")
        await self.event(BUYER, callback="buy_confirm")
        oid = self.store.list_orders(BUYER)[0]["id"]
        message_id = self.panel(BUYER).message_id
        await self.event(BUYER, callback=f"receipt:{oid}")
        back = self.panel(BUYER).reply_markup.inline_keyboard[0][0]
        self.assertEqual(back.callback_data, f"order:{oid}")
        await self.event(BUYER, callback=back.callback_data)
        self.assertIsNone(self.store.session(BUYER))
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "awaiting_payment")
        self.assertEqual(self.panel(BUYER).message_id, message_id)
        await self.event(BUYER, callback=f"receipt:{oid}")
        await self.event(BUYER, "/cancel")
        self.assertIn(f"Замовлення №{oid}", self.panel(BUYER).text)
        self.assertIsNone(self.store.session(BUYER))

    async def test_admin_order_back_preserves_list_filter_and_page(self):
        oid = await self.new_review()
        await self.event(ADMIN, callback="a:list:review:2")
        await self.event(ADMIN, callback=f"a:order:{oid}")
        back = next(b for row in self.panel(ADMIN).reply_markup.inline_keyboard for b in row if b.text == "← Назад")
        self.assertEqual(back.callback_data, "a:list:review:2")

    async def test_gift_preview_back_does_not_issue_gift(self):
        oid = await self.new_review()
        await self.event(ADMIN, callback=f"a:pay:{oid}")
        await self.event(ADMIN, "https://example.com/gift/preview")
        confirm = self.panel(ADMIN).reply_markup.inline_keyboard[0][0].callback_data
        back = next(b for row in self.panel(ADMIN).reply_markup.inline_keyboard for b in row if b.text == "← Назад")
        await self.event(ADMIN, callback=back.callback_data)
        self.assertEqual(self.store.session(ADMIN)[0], "admin_gift")
        await self.event(ADMIN, callback=confirm)
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "paid")
        self.assertIsNone(self.store.own_order(BUYER, oid)["gift_url"])

    async def test_media_callback_replaces_message_and_preserves_receipt_record(self):
        oid = await self.new_review()
        receipt = await self.bot.send_photo(ADMIN, "receipt-file", caption="Квитанція")
        before = len(self.session.calls)
        await self.event(ADMIN, callback=f"a:pay:{oid}", clicked=receipt)
        self.assertNotIn((ADMIN, receipt.message_id), self.session.messages)
        self.assertIsNotNone(self.panel(ADMIN).text)
        self.assertEqual(self.store.session(ADMIN)[0], "admin_gift")
        self.assertEqual(len(self.store.receipts(ADMIN, oid)), 1)
        self.assertFalse(any(isinstance(m, (SendPhoto, SendDocument)) for m in self.session.calls[before:]))
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "paid")

    async def test_verify_payment_immediately_requests_gift_and_repeated_click_is_safe(self):
        oid = await self.new_review()
        await self.event(ADMIN, callback=f"a:order:{oid}")
        panel = self.panel(ADMIN)
        pay = next(b.callback_data for row in panel.reply_markup.inline_keyboard for b in row
                   if b.text == "✅ Перевірив зарахування")
        self.assertEqual(pay, f"a:pay:{oid}:{self.store.receipts(ADMIN, oid)[-1]['id']}")
        before = len(self.session.calls)
        await self.event(ADMIN, callback=pay)
        order = self.store.admin_order(ADMIN, oid)
        self.assertEqual(order["status"], "paid")
        self.assertIsNone(order["payment_ref"])
        self.assertEqual(self.store.session(ADMIN), ("admin_gift", {"oid": oid}))
        self.assertIn("Надішліть посилання для активації", self.panel(ADMIN).text)
        self.assertEqual(self.panel(ADMIN).message_id, panel.message_id)
        self.assertFalse(any(isinstance(m, (SendPhoto, SendDocument)) for m in self.session.calls[before:]))
        queued = self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        await self.event(ADMIN, callback=pay)
        self.assertEqual(queued, self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
        back = self.panel(ADMIN).reply_markup.inline_keyboard[0][0].callback_data
        await self.event(ADMIN, callback=back)
        self.assertIsNone(self.store.session(ADMIN))
        self.assertEqual(self.store.admin_order(ADMIN, oid)["status"], "paid")
        await self.event(ADMIN, callback=f"a:gift:{oid}")
        self.assertEqual(self.store.session(ADMIN)[0], "admin_gift")

    async def test_stale_receipt_verification_cannot_confirm_new_receipt(self):
        oid = await self.new_review()
        rid = self.store.receipts(ADMIN, oid)[-1]["id"]
        self.store.reject_receipt(ADMIN, oid, "Unreadable")
        self.store.add_receipt(BUYER, oid, 123, "new-file", "new-unique", "photo")
        await self.event(ADMIN, callback=f"a:pay:{oid}:{rid}")
        self.assertEqual(self.store.admin_order(ADMIN, oid)["status"], "review")
        self.assertIsNone(self.store.session(ADMIN))
        self.assertIn("нову квитанцію", self.panel(ADMIN).text)

    async def test_legacy_payment_input_accepts_gift_without_recording_it_as_bank_reference(self):
        oid = await self.new_review()
        rid = self.store.receipts(ADMIN, oid)[-1]["id"]
        self.store.set_session(ADMIN, "admin_pay", {"oid": oid, "receipt_id": rid})
        await self.event(ADMIN, "https://example.com/gift/legacy")
        self.assertEqual(self.store.admin_order(ADMIN, oid)["status"], "paid")
        self.assertIsNone(self.store.admin_order(ADMIN, oid)["payment_ref"])
        self.assertEqual(self.store.session(ADMIN)[0], "gift_confirm")
        self.assertIn("https://example.com/gift/legacy", self.panel(ADMIN).text)

    async def test_legacy_payment_input_requires_valid_gift_and_current_receipt(self):
        oid = await self.new_review()
        rid = self.store.receipts(ADMIN, oid)[-1]["id"]
        self.store.set_session(ADMIN, "admin_pay", {"oid": oid, "receipt_id": rid})
        await self.event(ADMIN, "BANK-1234")
        self.assertEqual(self.store.admin_order(ADMIN, oid)["status"], "review")
        self.store.reject_receipt(ADMIN, oid, "Unreadable")
        self.store.add_receipt(BUYER, oid, 123, "new-file", "new-unique", "photo")
        self.store.set_session(ADMIN, "admin_pay", {"oid": oid, "receipt_id": rid})
        await self.event(ADMIN, "https://example.com/gift/legacy")
        self.assertEqual(self.store.admin_order(ADMIN, oid)["status"], "review")
        self.assertIn("нову квитанцію", self.panel(ADMIN).text)

    async def test_buyer_cannot_verify_payment_with_new_button(self):
        oid = await self.new_review()
        rid = self.store.receipts(ADMIN, oid)[-1]["id"]
        await self.event(BUYER, callback=f"a:pay:{oid}:{rid}")
        self.assertEqual(self.store.admin_order(ADMIN, oid)["status"], "review")
        self.assertIsNone(self.store.session(BUYER))

    async def test_receipt_view_attachments_are_removed_on_back(self):
        oid = await self.new_review()
        await self.event(ADMIN, callback=f"a:receipts:{oid}")
        attachments = self.store.screen(ADMIN)["extras"]
        self.assertEqual(len(attachments), 1)
        await self.event(ADMIN, callback=f"a:order:{oid}")
        self.assertTrue(all((ADMIN, mid) not in self.session.messages for mid in attachments))
        self.assertEqual(self.store.screen(ADMIN)["extras"], [])
        self.assertEqual(len(self.store.receipts(ADMIN, oid)), 1)

    async def test_uneditable_menu_is_replaced_after_successful_send(self):
        await self.event(BUYER, "/start")
        previous = self.panel(BUYER).message_id
        start = len(self.session.calls)
        self.session.edit_error = "message can't be edited"
        await self.event(BUYER, callback="instructions")
        self.assertNotEqual(self.panel(BUYER).message_id, previous)
        self.assertNotIn((BUYER, previous), self.session.messages)
        calls = self.session.calls[start:]
        self.assertLess(next(i for i, m in enumerate(calls) if isinstance(m, SendMessage)),
                        next(i for i, m in enumerate(calls) if isinstance(m, DeleteMessage)))

    async def test_unmodified_menu_does_not_create_duplicate(self):
        await self.event(BUYER, "/start")
        previous = self.panel(BUYER).message_id
        await self.event(BUYER, callback="home")
        self.assertEqual(self.panel(BUYER).message_id, previous)
        self.assertEqual(len([m for m in self.session.calls if isinstance(m, SendMessage) and m.chat_id == BUYER]), 1)

    async def test_old_undeletable_menu_has_buttons_removed(self):
        await self.event(BUYER, "/start")
        previous = self.panel(BUYER).message_id
        self.session.edit_error = "message can't be edited"
        self.session.delete_error = "message can't be deleted"
        await self.event(BUYER, callback="instructions")
        self.assertIsNone(self.session.messages[(BUYER, previous)].reply_markup)
        self.assertIsNotNone(self.panel(BUYER).reply_markup)

    async def test_recreated_ui_uses_persisted_menu_for_text_input(self):
        await self.event(ADMIN, "/admin")
        await self.event(ADMIN, callback="a:setting:price")
        message_id = self.panel(ADMIN).message_id
        self.ui = UI(self.bot, self.store)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(self.ui.router)
        await self.event(ADMIN, "250")
        self.assertEqual(self.panel(ADMIN).message_id, message_id)
        self.assertEqual(self.store.setting("price"), 250)

    async def new_review(self):
        await self.event(BUYER, "/start ad_campaign_a")
        await self.event(BUYER, callback="buy")
        await self.event(BUYER, callback="buy_confirm")
        oid = self.store.list_orders(BUYER)[0]["id"]
        await self.event(BUYER, callback=f"receipt:{oid}")
        await self.event(BUYER, photo=True)
        return oid

    async def ready_gift(self):
        oid = await self.new_review()
        await self.event(ADMIN, callback=f"a:pay:{oid}")
        await self.event(ADMIN, "https://example.com/gift/123")
        await self.event(ADMIN, callback=f"a:send:{oid}:{self.store.session(ADMIN)[1]['nonce']}")
        return oid

    async def test_full_purchase_and_admin_contact_through_dispatcher(self):
        oid = await self.new_review()
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "review")
        self.assertEqual(self.store.own_order(BUYER, oid)["source"], "ad_campaign_a")
        await self.event(ADMIN, callback=f"a:pay:{oid}")
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "paid")
        await self.event(ADMIN, "https://example.com/gift/123")
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "paid")
        await self.event(ADMIN, callback=f"a:send:{oid}:{self.store.session(ADMIN)[1]['nonce']}")
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "delivery_pending")
        await self.drain()
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "issued")
        gifts = [m for m in self.session.calls if isinstance(m, SendMessage) and m.chat_id == BUYER and "https://example.com/gift/123" in m.text]
        self.assertEqual(len(gifts), 1)
        await self.event(BUYER, callback=f"activate:{oid}")
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "activated")
        await self.event(BUYER, callback=f"support:{oid}")
        contact = self.session.calls[-1].reply_markup.inline_keyboard[0][0]
        self.assertEqual(contact.url, "https://t.me/shop_admin")
        self.assertIsNone(contact.callback_data)
        self.assertIsNone(self.store.session(BUYER))
        self.assertEqual(self.store.tickets(ADMIN), [])
        # Admins can still send an order-specific message and read its history.
        await self.event(ADMIN, callback=f"a:message:{oid}")
        await self.event(ADMIN, "Відкрийте налаштування підписки.")
        await self.drain()
        tid = self.store.tickets(ADMIN)[0]["id"]
        self.assertEqual(len(self.store.ticket_messages(ADMIN, tid)), 1)
        await self.event(ADMIN, callback=f"a:ticket:{tid}")
        await self.event(ADMIN, callback="a:stats")
        await self.event(BUYER, callback=f"order:{oid}")

    async def test_contact_buttons_open_chat_without_callback(self):
        await self.event(BUYER, "/start")
        home = self.session.calls[-1]
        self.assertIn("1–5 хв", home.text)
        buttons = [b for row in home.reply_markup.inline_keyboard for b in row]
        contact = next(b for b in buttons if "Написати адміну" in b.text)
        self.assertEqual(contact.url, "https://t.me/shop_admin")
        self.assertIsNone(contact.callback_data)
        self.assertFalse(any(b.callback_data == "terms" for b in buttons))
        await self.event(BUYER, callback="buy")
        self.assertNotIn("Умови", self.session.calls[-1].text)
        await self.event(BUYER, callback="buy_confirm")
        order = self.session.calls[-1]
        self.assertIn("1–5 хв", order.text)
        help_button = next(b for row in order.reply_markup.inline_keyboard for b in row if "допомога" in b.text)
        self.assertEqual(help_button.url, "https://t.me/shop_admin")
        self.assertIsNone(help_button.callback_data)

    async def test_first_start_notifies_admin_once_with_ad_source(self):
        await self.event(BUYER, "/start instagram_campaign")
        await self.drain()
        await self.event(BUYER, "/start another_campaign")
        await self.event(BUYER, callback="home")
        await self.drain()
        notifications = [m for m in self.session.calls if isinstance(m, SendMessage)
                         and m.chat_id == ADMIN and "Новий користувач у боті" in m.text]
        self.assertEqual(len(notifications), 1)
        self.assertIn("instagram_campaign", notifications[0].text)
        self.assertIn(f"<code>{BUYER}</code>", notifications[0].text)

    async def test_existing_user_start_does_not_send_join_notification(self):
        # Emulate a user present in the database before this feature was added.
        self.store.db.execute("INSERT INTO users VALUES (?,?,?,?,?)", (BUYER, None, "Existing", "direct", 1))
        await self.event(BUYER, "/start")
        await self.drain()
        self.assertFalse(any(isinstance(m, SendMessage) and m.chat_id == ADMIN for m in self.session.calls))

    async def test_support_commands_and_legacy_sessions_show_contact(self):
        for command in ("/support", "/paysupport"):
            await self.event(BUYER, command)
            self.assertIsNone(self.store.session(BUYER))
            self.assertEqual(self.session.calls[-1].reply_markup.inline_keyboard[0][0].url,
                             "https://t.me/shop_admin")
        self.store.set_session(BUYER, "support", {"oid": 0})
        await self.event(BUYER, "Запитання")
        self.assertIsNone(self.store.session(BUYER))
        self.assertEqual(self.store.tickets(ADMIN), [])

    async def test_delivered_gift_has_direct_contact_button(self):
        await self.ready_gift()
        await self.drain()
        gift = next(m for m in self.session.calls if isinstance(m, SendMessage)
                    and m.chat_id == BUYER and "example.com/gift" in m.text)
        help_button = next(b for row in gift.reply_markup.inline_keyboard for b in row if "допомога" in b.text)
        self.assertEqual(help_button.url, "https://t.me/shop_admin")
        self.assertIsNone(help_button.callback_data)

    async def test_unauthorized_callbacks_and_foreign_orders_do_not_leak_gift(self):
        oid = await self.ready_gift()
        await self.drain()
        before = len(self.session.calls)
        for callback in (f"order:{oid}", f"a:order:{oid}", f"a:receipts:{oid}",
                         f"a:pay:{oid}", f"a:send:{oid}", "a:settings", "admin"):
            await self.event(OTHER, callback=callback)
        replies = [m.text for m in self.session.calls[before:] if isinstance(m, (SendMessage, EditMessageText))]
        self.assertTrue(replies)
        self.assertFalse(any("example.com/gift" in text for text in replies))
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "issued")

    async def test_rejected_pdf_can_be_resubmitted_to_same_order(self):
        oid = await self.new_review()
        await self.event(ADMIN, callback=f"a:reject:{oid}")
        await self.event(ADMIN, "Не видно суму.")
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "rejected")
        await self.event(BUYER, callback=f"receipt:{oid}")
        await self.event(BUYER, document={"file_id": "pdf-file", "file_unique_id": "pdf-unique",
                                         "file_name": "receipt.pdf", "mime_type": "application/pdf", "file_size": 100})
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "review")
        self.assertEqual(len(self.store.receipts(ADMIN, oid)), 2)
        await self.drain()
        self.assertTrue(any(isinstance(m, SendDocument) for m in self.session.calls))

    async def test_blocked_recipient_can_resume_delivery(self):
        oid = await self.ready_gift()
        self.session.fail_gift = TelegramForbiddenError(method=SendMessage(chat_id=BUYER, text="test"), message="blocked")
        await self.drain()
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "delivery_pending")
        self.assertEqual(self.store.stats(ADMIN)["parked"], 1)
        self.session.fail_gift = None
        await self.event(BUYER, "/start")
        await self.drain()
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "issued")

    async def test_retry_after_preserves_pending_delivery(self):
        oid = await self.ready_gift()
        self.session.fail_gift = TelegramRetryAfter(method=SendMessage(chat_id=BUYER, text="test"), message="rate limit", retry_after=5)
        await self.drain()
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "delivery_pending")
        row = self.store.db.execute("SELECT * FROM outbox WHERE purpose='gift'").fetchone()
        self.assertEqual(row["attempts"], 1)
        self.assertFalse(row["parked"])
        self.session.fail_gift = None
        self.store.retry_delivery(ADMIN, oid)
        await self.drain()
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "issued")

    async def test_changed_price_requires_new_consent(self):
        await self.event(BUYER, "/start")
        await self.event(BUYER, callback="buy")
        self.store.set_setting(ADMIN, "price", 250)
        await self.event(BUYER, callback="buy_confirm")
        self.assertEqual(self.store.list_orders(BUYER), [])
        await self.event(BUYER, callback="buy")
        await self.event(BUYER, callback="buy_confirm")
        self.assertEqual(self.store.list_orders(BUYER)[0]["price"], 250)

    async def test_admin_settings_and_refund_confirmation(self):
        oid = await self.ready_gift()
        await self.drain()
        await self.event(ADMIN, callback="a:setting:price")
        await self.event(ADMIN, "250")
        self.assertEqual(self.store.setting("price"), 250)
        await self.event(ADMIN, callback=f"a:refund:{oid}")
        await self.event(ADMIN, "REFUND-123")
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "issued")
        await self.event(ADMIN, callback=f"a:refund_confirm:{oid}")
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "refunded")

    async def test_group_messages_are_ignored(self):
        await self.event(BUYER, "/start", group=True)
        self.assertEqual(self.session.calls, [])

    async def test_invalid_file_keeps_receipt_prompt(self):
        await self.event(BUYER, "/start")
        await self.event(BUYER, callback="buy")
        await self.event(BUYER, callback="buy_confirm")
        oid = self.store.list_orders(BUYER)[0]["id"]
        await self.event(BUYER, callback=f"receipt:{oid}")
        await self.event(BUYER, document={"file_id": "exe", "file_unique_id": "exe",
                                         "file_name": "receipt.exe", "mime_type": "application/octet-stream"})
        self.assertEqual(self.store.session(BUYER)[0], "receipt")
        self.assertEqual(self.store.own_order(BUYER, oid)["status"], "awaiting_payment")


if __name__ == "__main__":
    unittest.main()
