from datetime import datetime, timezone
from html import escape
import json
import logging
import re
import time

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from .navigation import Navigation
from .store import ShopError
from .texts import INSTRUCTIONS, PRODUCT, STATUS, back_keys, home_keys, keyboard, order_keys

log = logging.getLogger(__name__)
BACK = back_keys()
CANCEL = keyboard((("← Назад", "cancel_input"), ("🏠 Головна", "home")))


def attachment(message, receipt=False):
    if message.photo:
        media = message.photo[-1]
        kind = "photo"
    elif message.document:
        media = message.document
        kind = "document"
        mime = media.mime_type or ""
        name = (media.file_name or "").lower()
        if mime != "application/pdf" or not name.endswith(".pdf"):
            raise ShopError("Надішліть фото через галерею або файл PDF.")
    elif not receipt and message.text:
        if len(message.text) > 3000:
            raise ShopError("Скоротіть повідомлення до 3000 символів.")
        return {"kind": "text", "text": message.text}
    else:
        raise ShopError("Надішліть фото або PDF квитанції." if receipt else "Надішліть текст, фото або PDF.")
    if (media.file_size or 0) > 10 * 1024 * 1024:
        raise ShopError("Максимальний розмір вкладення — 10 МБ.")
    if len(message.caption or "") > 700:
        raise ShopError("Скоротіть підпис до вкладення до 700 символів.")
    return {"kind": kind, "file_id": media.file_id, "unique_id": media.file_unique_id,
            "text": message.caption or ""}


def order_text(order, admin=False):
    created = datetime.fromtimestamp(order["created_at"], timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    text = (f"📦 <b>Замовлення №{order['id']}</b>\n"
            f"{escape(PRODUCT)}\n"
            f"Сума: <b>{order['price']} грн</b>\n"
            f"Статус: {STATUS[order['status']]}\n"
            f"Створено: {created}\n"
            f"Час видачі: {escape(order['delivery_time'])}")
    if admin:
        text += (f"\n\nПокупець: {escape(order['name'])}\n"
                 f"Telegram ID: <code>{order['user_id']}</code>")
        if order["username"]:
            text += f"\n@{escape(order['username'])}"
        if order["payment_ref"]:
            text += f"\nОперація зарахування: <code>{escape(order['payment_ref'])}</code>"
        if order["refund_ref"]:
            text += f"\nОперація повернення: <code>{escape(order['refund_ref'])}</code>"
    elif order["status"] in ("awaiting_payment", "rejected"):
        card = " ".join(order["card"][i:i+4] for i in range(0, len(order["card"]), 4))
        text += (f"\n\n💳 Картка: <code>{card}</code>\n"
                 f"Отримувач: {escape(order['recipient'])}\n"
                 f"Банк: {escape(order['bank']) or '—'}\n\n"
                 f"Перекажіть рівно {order['price']} грн і надішліть квитанцію кнопкою нижче.\n"
                 "Якщо вже переказали кошти, повторно не оплачуйте.\n"
                 "Квитанцію перевіряє адміністратор.")
    if order["rejection_reason"] and order["status"] == "rejected":
        text += f"\n\nПричина відхилення: {escape(order['rejection_reason'])}"
    if order["gift_url"] and order["status"] in ("issued", "activated"):
        text += f"\n\n🎁 Ваше посилання:\n{escape(order['gift_url'])}"
    return text


class UI:
    def __init__(self, bot, store):
        self.bot, self.store = bot, store
        self.navigation = Navigation(bot, store)
        self.router = Router(name="shop")
        self.router.message.register(self.message, F.chat.type == "private")
        self.router.callback_query.register(self.callback)
        self.last_action = {}

    async def send(self, uid, text, markup=None):
        return await self.navigation.render(uid, text, markup or BACK)

    def register(self, user, source="direct"):
        self.store.user(user.id, user.full_name, user.username, source)

    def throttle(self, uid):
        if uid in self.store.config.admin_ids:
            return False
        now = time.monotonic()
        previous = self.last_action.get(uid, 0)
        if len(self.last_action) > 10000:
            self.last_action = {k: v for k, v in self.last_action.items() if now - v < 60}
        if now - previous < 0.6:
            return True
        self.last_action[uid] = now
        return False

    async def home(self, uid):
        self.store.clear_session(uid)
        text = (f"✨ <b>{escape(PRODUCT)}</b>\n\n"
                "Активація подарунковим посиланням на вашому особистому Google-акаунті.\n"
                "<b>Передавати пароль або доступ до акаунта не потрібно.</b>\n\n"
                "У вартість входять:\n"
                "• підписка на 18 місяців;\n"
                "• подарункове посилання та інструкція;\n"
                "• допомога з активацією.\n\n"
                f"💳 Вартість: <b>{self.store.setting('price')} грн</b>\n"
                "Оплата переказом на картку з квитанцією.\n"
                "Після перевірки оплати адміністратор вручну видає посилання.\n"
                f"⏱ Час видачі: {escape(self.store.setting('delivery_time'))}")
        if not self.store.setting("sales_open"):
            text += "\n\n⏸ Продажі зараз призупинено. Підтримка працює."
        await self.send(uid, text, home_keys(uid in self.store.config.admin_ids, contact_url=self.store.config.admin_contact_url))

    async def show_order(self, uid, oid, admin=False):
        order = self.store.admin_order(uid, oid) if admin else self.store.own_order(uid, oid)
        parent = self.navigation.recalled(uid, "admin_orders" if admin else "buyer_orders",
                                          "a:list:all:0" if admin else "orders:0")
        await self.send(uid, order_text(order, admin), order_keys(order, admin,
                        contact_url=self.store.config.admin_contact_url, back_to=parent))

    async def list_orders(self, uid, page=0, admin=False, status="all"):
        orders = self.store.list_orders(uid, page, admin=admin, status=status)
        rows = [((f"№{o['id']} · {o['price']} грн · {STATUS[o['status']]}",
                  f"a:order:{o['id']}" if admin else f"order:{o['id']}"),) for o in orders[:8]]
        prefix = f"a:list:{status}" if admin else "orders"
        self.navigation.remember(uid, "admin_orders" if admin else "buyer_orders", f"{prefix}:{page}")
        nav = []
        if page > 0:
            nav.append(("← Назад", f"{prefix}:{page-1}"))
        if len(orders) > 8:
            nav.append(("Далі →", f"{prefix}:{page+1}"))
        if nav:
            rows.append(tuple(nav))
        rows.append((("← Назад", "admin" if admin else "home"),))
        await self.send(uid, "📦 <b>Замовлення</b>" if orders else "У цьому списку ще немає замовлень.", keyboard(*rows))

    async def admin_menu(self, uid):
        self.store.admin(uid)
        self.store.clear_session(uid)
        stats = self.store.stats(uid)
        await self.send(uid, f"⚙️ <b>Адмін-меню</b>\n\nНедоставлених повідомлень: {stats['pending']}\n"
                            f"Потребують повторної спроби: {stats['parked']}",
                        keyboard((("📎 Перевірити квитанції", "a:list:review:0"),),
                                 (("🎁 Очікують видачі", "a:list:paid:0"),),
                                 (("📦 Усі замовлення", "a:list:all:0"), ("🔎 Пошук", "a:search")),
                                 (("💬 Звернення", "a:tickets:0"), ("📊 Статистика", "a:stats")),
                                 (("⚙️ Налаштування", "a:settings"),),
                                 (("🔄 Повторити недоставлені", "a:retry_all"),),
                                 (("← Назад", "home"),)))

    async def settings(self, uid):
        self.store.admin(uid)
        opened = self.store.setting("sales_open")
        await self.send(uid, f"⚙️ <b>Налаштування</b>\nПродажі: {'відкрито' if opened else 'призупинено'}\n"
                            f"Ціна: {self.store.setting('price')} грн\n"
                            f"Час видачі: {escape(self.store.setting('delivery_time'))}",
                        keyboard((("⏸ Призупинити" if opened else "▶️ Відкрити продажі", f"a:sales:{0 if opened else 1}"),),
                                 (("💳 Змінити ціну", "a:setting:price"),),
                                 (("⏱ Змінити час видачі", "a:setting:delivery_time"),),
                                 (("← Назад", "admin"),)))

    async def prompt(self, uid, kind, data, text):
        self.store.set_session(uid, kind, data)
        await self.send(uid, text + "\n\nСкасувати введення: /cancel", back_keys(self.cancel_target(uid)))

    def cancel_target(self, uid):
        state = self.store.session(uid)
        if not state:
            return "home"
        kind, data = state
        if kind == "admin_setting":
            return "a:settings"
        if kind == "admin_reply":
            return f"a:ticket:{data['tid']}"
        if "oid" in data and data["oid"]:
            admin = kind.startswith("admin_") or kind in ("gift_confirm", "refund_confirm")
            return f"a:order:{data['oid']}" if admin else f"order:{data['oid']}"
        return "admin" if kind.startswith("admin_") else "home"

    async def show_contact(self, uid, oid=0):
        if oid:
            self.store.own_order(uid, oid)
        self.store.clear_session(uid)
        text = "💬 Напишіть адміністратору в особистий чат."
        if oid:
            text += f"\nВкажіть номер замовлення: №{oid}."
        await self.send(uid, text, keyboard(
            (("💬 Написати адміну", self.store.config.admin_contact_url),),
            (("← Назад", f"order:{oid}" if oid else "home"),)))

    async def callback(self, query: CallbackQuery):
        uid = query.from_user.id
        if not query.message or query.message.chat.type != "private" or query.message.chat.id != uid:
            await query.answer("Відкрийте особистий чат із ботом.", show_alert=True)
            return
        if self.throttle(uid):
            await query.answer("Зачекайте мить.")
            return
        await query.answer()
        self.register(query.from_user)
        try:
            await self.navigation.begin(uid, query.message)
            await self.handle_callback(uid, query.data or "")
        except ShopError as exc:
            await self.send(uid, escape(str(exc)), BACK)
        except (ValueError, IndexError):
            await self.send(uid, "Ця кнопка застаріла. Відкрийте меню через /start.", BACK)
        except Exception as exc:
            log.error("Callback failed: type=%s", type(exc).__name__)
            await self.send(uid, "Не вдалося виконати дію. Відкрийте замовлення та перевірте статус перед повторною спробою.", BACK)

    async def handle_callback(self, uid, data):
        s = self.store
        if data == "cancel_input":
            data = self.cancel_target(uid)
        parts = data.split(":")
        keeps_confirmation = data == "buy_confirm" or (parts[0] == "a" and len(parts) > 1 and parts[1] in ("send", "refund_confirm"))
        if not keeps_confirmation:
            s.clear_session(uid)
        if data.startswith("a:") or data == "admin":
            s.admin(uid)
            await self.admin_callback(uid, parts)
        elif data in ("home", "cancel_input"):
            await self.home(uid)
        elif data == "instructions":
            await self.send(uid, f"📖 <b>Як активувати підписку</b>\n\n{INSTRUCTIONS}", BACK)
        elif data == "terms":
            # An old message may still contain this removed button.
            await self.home(uid)
        elif data == "buy":
            existing = s.list_orders(uid)
            active = next((o for o in existing if o["status"] in ("awaiting_payment", "review", "rejected", "paid", "delivery_pending")), None)
            if active:
                await self.show_order(uid, active["id"])
                return
            if not s.setting("sales_open"):
                raise ShopError("Продажі тимчасово призупинено. Напишіть адміністратору.")
            s.set_session(uid, "purchase_confirm", {"price": s.setting("price"), "time": s.setting("delivery_time")})
            await self.send(uid, f"🛒 <b>{escape(PRODUCT)}</b>\nЦіна: <b>{s.setting('price')} грн</b>\n"
                                f"Час видачі: {escape(s.setting('delivery_time'))}\n\n"
                                "Після натискання кнопки покажемо реквізити для переказу.",
                            keyboard((("💳 Перейти до оплати", "buy_confirm"),),
                                     (("💬 Написати адміну", s.config.admin_contact_url), ("← Назад", "home"))))
        elif data == "buy_confirm":
            state = s.session(uid)
            if not state or state[0] != "purchase_confirm":
                raise ShopError("Відкрийте «Купити підписку» ще раз, щоб переглянути актуальну ціну.")
            if state[1] != {"price": s.setting("price"), "time": s.setting("delivery_time")}:
                raise ShopError("Ціна або час видачі змінилися. Натисніть «Купити підписку» ще раз.")
            order, _ = s.new_order(uid)
            s.clear_session(uid)
            await self.show_order(uid, order["id"])
        elif parts[0] == "orders":
            s.clear_session(uid)
            await self.list_orders(uid, max(0, int(parts[1])))
        elif parts[0] == "order":
            await self.show_order(uid, int(parts[1]))
        elif parts[0] == "receipt":
            oid = int(parts[1])
            order = s.own_order(uid, oid)
            if order["status"] not in ("awaiting_payment", "rejected"):
                raise ShopError("Квитанція вже на перевірці або замовлення опрацьоване.")
            await self.prompt(uid, "receipt", {"oid": oid}, f"📎 Надішліть фото або PDF квитанції до замовлення №{oid}.\nМаксимальний розмір — 10 МБ.")
        elif parts[0] == "cancel":
            oid = int(parts[1])
            s.own_order(uid, oid)
            await self.send(uid, f"Скасувати замовлення №{oid}?\nПідтверджуйте лише якщо ви ще не переказали кошти.",
                            keyboard((("Не оплачував — скасувати", f"cancel_yes:{oid}"),),
                                     (("← Назад", f"order:{oid}"),)))
        elif parts[0] == "cancel_yes":
            s.cancel_order(uid, int(parts[1]))
            s.clear_session(uid)
            await self.show_order(uid, int(parts[1]))
        elif parts[0] == "activate":
            s.activate(uid, int(parts[1]))
            await self.send(uid, "✅ Дякуємо! Ваше підтвердження активації збережено.", back_keys(f"order:{parts[1]}"))
        elif parts[0] == "support":
            await self.show_contact(uid, int(parts[1]))
        else:
            raise ShopError("Кнопка застаріла. Відкрийте меню через /start.")

    async def admin_callback(self, uid, parts):
        s = self.store
        s.admin(uid)
        if parts == ["admin"]:
            await self.admin_menu(uid)
            return
        action = parts[1]
        if action == "list":
            s.clear_session(uid)
            await self.list_orders(uid, max(0, int(parts[3])), True, parts[2])
        elif action == "order":
            await self.show_order(uid, int(parts[2]), True)
        elif action == "pay":
            oid = int(parts[2])
            order = s.admin_order(uid, oid)
            if order["status"] == "review":
                receipt_id = int(parts[3]) if len(parts) > 3 else order["receipt_id"]
                s.confirm_payment(uid, oid, receipt_id=receipt_id)
            elif order["status"] != "paid":
                raise ShopError("Статус замовлення змінився. Оновіть картку.")
            await self.prompt(uid, "admin_gift", {"oid": oid},
                              f"✅ Оплату замовлення №{oid} підтверджено.\n\n"
                              "🎁 Надішліть посилання для активації (HTTPS).\nДалі буде попередній перегляд перед відправленням покупцю.")
        elif action in ("reject", "gift", "message", "refund"):
            oid = int(parts[2])
            order = s.admin_order(uid, oid)
            expected = {"reject": ("review",), "gift": ("paid",),
                        "refund": ("paid", "issued", "activated")}
            if action in expected and order["status"] not in expected[action]:
                raise ShopError("Статус замовлення змінився. Оновіть картку.")
            prompts = {
                "reject": f"Вкажіть причину відхилення квитанції до замовлення №{oid}. Покупець отримає цей текст.",
                "gift": f"Надішліть подарункове HTTPS-посилання для замовлення №{oid}.\nДалі буде попередній перегляд.",
                "message": f"Надішліть текст, фото або PDF для покупця замовлення №{oid}.",
                "refund": f"Спочатку поверніть {order['price']} грн за замовленням №{oid} через свій банк.\n"
                          "Бот не переказує кошти. Після фактичного повернення надішліть ідентифікатор операції повернення.",
            }
            details = {"oid": oid}
            if action == "reject":
                receipt = s.receipts(uid, oid)[-1]
                details["receipt_id"] = receipt["id"]
                method = self.bot.send_photo if receipt["kind"] == "photo" else self.bot.send_document
                await self.navigation.extra(uid, method, **{receipt["kind"]: receipt["file_id"]},
                             caption=f"Поточна квитанція №{receipt['id']} · замовлення №{oid}", protect_content=True)
            await self.prompt(uid, f"admin_{action}", details, prompts[action])
        elif action == "send":
            oid = int(parts[2])
            s.issue_gift(uid, oid, parts[3] if len(parts) > 3 else None)
            await self.send(uid, f"Посилання до замовлення №{oid} додано до черги. Бот повідомить про доставку.",
                            back_keys(f"a:order:{oid}"))
        elif action == "refund_confirm":
            oid = int(parts[2])
            state = s.session(uid)
            if not state or state[0] != "refund_confirm" or state[1]["oid"] != oid:
                raise ShopError("Підтвердження застаріло. Відкрийте замовлення ще раз.")
            s.refund(uid, oid, state[1]["reference"])
            await self.show_order(uid, oid, True)
        elif action == "receipts":
            oid = int(parts[2])
            page = max(0, int(parts[3])) if len(parts) > 3 else 0
            receipts = s.receipts(uid, oid)
            batch = receipts[page*5:page*5+5]
            if not batch:
                await self.send(uid, "Квитанцій поки немає.")
            for r in batch:
                method = self.bot.send_photo if r["kind"] == "photo" else self.bot.send_document
                await self.navigation.extra(uid, method, **{r["kind"]: r["file_id"]},
                             caption=f"Квитанція №{r['id']} · замовлення №{oid}", protect_content=True)
            rows = []
            if page:
                rows.append((("← Попередні", f"a:receipts:{oid}:{page-1}"),))
            if len(receipts) > page*5+5:
                rows.append((("Наступні →", f"a:receipts:{oid}:{page+1}"),))
            rows.append((("← Назад", f"a:order:{oid}"),))
            await self.send(uid, f"Квитанцій у замовленні: {len(receipts)}", keyboard(*rows))
        elif action == "retry":
            s.retry_delivery(uid, int(parts[2]))
            await self.send(uid, "Повторну доставку заплановано.", back_keys(f"a:order:{parts[2]}"))
        elif action == "retry_all":
            s.retry_delivery(uid)
            await self.send(uid, "Недоставлені повідомлення повернено до черги.", back_keys("admin"))
        elif action == "search":
            await self.prompt(uid, "admin_search", {}, "Введіть номер замовлення, наприклад 1042.")
        elif action == "settings":
            s.clear_session(uid)
            await self.settings(uid)
        elif action == "sales":
            s.set_setting(uid, "sales_open", parts[2] == "1")
            await self.settings(uid)
        elif action == "setting":
            if parts[2] not in ("price", "delivery_time"):
                raise ShopError("Невідоме налаштування.")
            await self.prompt(uid, "admin_setting", {"key": parts[2]},
                              "Введіть нову ціну в гривнях цілим числом." if parts[2] == "price" else "Введіть час видачі, який бачитиме покупець.")
        elif action == "stats":
            stats = s.stats(uid)
            money = stats["money"]
            text = (f"📊 <b>Статистика за весь час</b>\nКористувачів: {stats['users']}\n"
                    f"Підтверджено оплат: {money['gross']} грн\nПовернення: {money['refunds']} грн\n"
                    f"Після повернень: {money['gross'] - money['refunds']} грн\n\n")
            text += "\n".join(f"{STATUS[r['status']]}: {r['n']}" for r in stats["statuses"])
            text += "\n\n<b>Джерела реклами</b> (перший запуск)\n"
            text += "\n".join(f"{escape(r['source'])}: {r['users']} корист., {r['payments']} оплат" for r in stats["sources"])
            await self.send(uid, text, back_keys("admin"))
        elif action == "tickets":
            page = max(0, int(parts[2]))
            tickets = s.tickets(uid, page)
            self.navigation.remember(uid, "tickets", f"a:tickets:{page}")
            rows = [((f"№{t['id']} · покупець {t['user_id']}", f"a:ticket:{t['id']}"),) for t in tickets[:8]]
            if page:
                rows.append((("← Назад", f"a:tickets:{page-1}"),))
            if len(tickets) > 8:
                rows.append((("Далі →", f"a:tickets:{page+1}"),))
            rows.append((("← Назад", "admin"),))
            await self.send(uid, "💬 Відкриті звернення" if tickets else "Відкритих звернень немає.", keyboard(*rows))
        elif action == "ticket":
            tid = int(parts[2])
            page = max(0, int(parts[3])) if len(parts) > 3 else 0
            ticket = s.ticket(uid, tid)
            messages = s.ticket_messages(uid, tid, page)
            heading = (f"💬 <b>Звернення №{tid}</b>\nПокупець: {escape(ticket['name'])} · ID {ticket['user_id']}\n"
                       f"Статус: {'відкрите' if ticket['status'] == 'open' else 'закрите'}")
            for item in reversed(messages[:5]):
                content = json.loads(item["body"])
                prefix = "Покупець:\n" if item["direction"] == "in" else "Адміністратор:\n"
                if content["kind"] == "text":
                    await self.navigation.extra(uid, self.bot.send_message,
                                                text=prefix + escape(content["text"]), parse_mode="HTML")
                else:
                    method = self.bot.send_photo if content["kind"] == "photo" else self.bot.send_document
                    await self.navigation.extra(uid, method, **{content["kind"]: content["file_id"]},
                                 caption=prefix + content.get("text", ""), parse_mode=None, protect_content=True)
            rows = []
            if ticket["status"] == "open":
                rows.append((("💬 Відповісти", f"a:reply:{tid}"), ("✅ Закрити", f"a:close:{tid}")))
            if ticket["order_id"]:
                rows.append((("📦 Замовлення", f"a:order:{ticket['order_id']}"),))
            if page:
                rows.append((("Новіші повідомлення", f"a:ticket:{tid}:{page-1}"),))
            if len(messages) > 5:
                rows.append((("Старіші повідомлення", f"a:ticket:{tid}:{page+1}"),))
            rows.append((("← Назад", self.navigation.recalled(uid, "tickets", "a:tickets:0")),))
            await self.send(uid, heading, keyboard(*rows))
        elif action == "reply":
            tid = int(parts[2])
            ticket = s.ticket(uid, tid)
            if ticket["status"] != "open":
                raise ShopError("Звернення вже закрито.")
            await self.prompt(uid, "admin_reply", {"tid": tid},
                              f"Надішліть відповідь для звернення №{tid}: текст, фото або PDF.")
        elif action == "close":
            s.close_ticket(uid, int(parts[2]))
            await self.send(uid, "Звернення закрито.", back_keys(self.navigation.recalled(uid, "tickets", "a:tickets:0")))
        else:
            raise ShopError("Невідома дія адміністратора.")

    async def message(self, message: Message):
        if not message.from_user:
            return
        uid = message.from_user.id
        text = (message.text or "").strip()
        command = text.split(maxsplit=1)[0].split("@")[0] if text.startswith("/") else ""
        source = "direct"
        if command == "/start":
            payload = text.split(maxsplit=1)
            if len(payload) == 2 and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", payload[1]):
                source = payload[1]
        self.register(message.from_user, source)
        if command and self.throttle(uid):
            return
        try:
            await self.navigation.begin(uid)
            if command in ("/start", "/menu"):
                self.store.resume_recipient(uid)
                await self.home(uid)
            elif command == "/cancel":
                await self.handle_callback(uid, self.cancel_target(uid))
            elif command == "/admin":
                await self.admin_menu(uid)
            elif command == "/orders":
                self.store.clear_session(uid)
                await self.list_orders(uid)
            elif command in ("/support", "/paysupport"):
                await self.show_contact(uid)
            elif command == "/terms":
                await self.home(uid)
            elif command == "/id":
                await self.send(uid, f"Ваш Telegram ID: <code>{uid}</code>")
            elif command == "/help":
                await self.send(uid, "/start — головна\n/orders — замовлення\n/support — допомога\n"
                                "/paysupport — питання оплати\n/cancel — скасувати введення\n/id — ваш ID", BACK)
            elif command:
                await self.send(uid, "Невідома команда. Скористайтеся /help.", BACK)
            else:
                await self.input(message)
        except ShopError as exc:
            await self.send(uid, escape(str(exc)), CANCEL if self.store.session(uid) else BACK)
        except ValueError:
            await self.send(uid, "Введіть коректне ціле число.", CANCEL)
        except Exception as exc:
            log.error("Message handling failed: type=%s", type(exc).__name__)
            await self.send(uid, "Не вдалося виконати дію. Перевірте статус у «Мої замовлення» або зверніться до підтримки.", BACK)

    async def input(self, message):
        uid = message.from_user.id
        s = self.store
        state = s.session(uid)
        if not state:
            await self.send(uid, "Оберіть дію в меню. Для квитанції відкрийте замовлення → «Надіслати квитанцію».",
                            home_keys(uid in s.config.admin_ids, contact_url=s.config.admin_contact_url))
            return
        kind, data = state
        text = (message.text or "").strip()
        if kind.startswith("admin_") or kind in ("gift_confirm", "refund_confirm"):
            s.admin(uid)
        if kind == "receipt":
            body = attachment(message, receipt=True)
            s.add_receipt(uid, data["oid"], message.message_id, body["file_id"], body["unique_id"], body["kind"])
            await self.show_order(uid, data["oid"])
        elif kind == "support":
            # Redirect a session opened before direct contact was introduced.
            await self.show_contact(uid, data["oid"])
        elif kind == "admin_reject":
            s.reject_receipt(uid, data["oid"], text, data["receipt_id"])
            await self.show_order(uid, data["oid"], True)
        elif kind in ("admin_gift", "admin_pay"):
            oid = data["oid"]
            if kind == "admin_pay":
                # Resume input opened by the old verification button before deployment.
                s.validate_gift(text)
                if s.admin_order(uid, oid)["status"] == "review":
                    s.confirm_payment(uid, oid, receipt_id=data["receipt_id"])
                s.set_session(uid, "admin_gift", {"oid": oid})
            url = s.prepare_gift(uid, oid, text)
            order = s.admin_order(uid, oid)
            await self.send(uid, f"🎁 <b>Перевірте перед відправленням</b>\nЗамовлення №{oid}\n"
                                f"Покупець: {escape(order['name'])} · ID {order['user_id']}\n"
                                f"Оплата: {order['price']} грн — підтверджено\n\n{escape(url)}",
                            keyboard((("✅ Надіслати покупцю", f"a:send:{oid}:{s.session(uid)[1]['nonce']}"),),
                                     (("← Назад", f"a:gift:{oid}"), ("🏠 Головна", "home"))))
        elif kind == "admin_refund":
            ref = s.reference(text)
            oid = data["oid"]
            s.set_session(uid, "refund_confirm", {"oid": oid, "reference": ref})
            await self.send(uid, f"Підтвердити, що ви вже повернули кошти за замовленням №{oid} через банк?\n"
                                f"Операція повернення: <code>{escape(ref)}</code>",
                            keyboard((("Так, переказ виконано", f"a:refund_confirm:{oid}"),),
                                     (("← Назад", f"a:refund:{oid}"), ("🏠 Головна", "home"))))
        elif kind == "admin_reply":
            s.reply_support(uid, data["tid"], message.message_id, attachment(message))
            await self.send(uid, "Відповідь додано до черги доставки.", back_keys(f"a:ticket:{data['tid']}"))
        elif kind == "admin_message":
            s.contact_buyer(uid, data["oid"], message.message_id, attachment(message))
            await self.send(uid, "Повідомлення додано до черги доставки.", back_keys(f"a:order:{data['oid']}"))
        elif kind == "admin_search":
            oid = int(text.lstrip("#№ "))
            await self.show_order(uid, oid, True)
            s.clear_session(uid)
        elif kind == "admin_setting":
            value = int(text) if data["key"] == "price" else text
            s.set_setting(uid, data["key"], value)
            s.clear_session(uid)
            await self.settings(uid)
        else:
            await self.send(uid, "Завершіть дію кнопкою підтвердження або натисніть /cancel.", CANCEL)
