from contextlib import contextmanager
from html import escape
import ipaddress
import json
import secrets
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit, urlunsplit

from .texts import gift_message, keyboard, order_keys


class ShopError(Exception):
    """Expected validation or permissions error, safe to display."""


class Store:
    def __init__(self, config):
        self.config = config
        if config.database != ":memory:":
            Path(config.database).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(config.database, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys=ON;
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, username TEXT, name TEXT NOT NULL,
                source TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                price INTEGER NOT NULL, card TEXT NOT NULL, recipient TEXT NOT NULL,
                bank TEXT NOT NULL, delivery_time TEXT NOT NULL, terms TEXT NOT NULL,
                source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'awaiting_payment',
                payment_ref TEXT UNIQUE, paid_by INTEGER, paid_at REAL,
                rejection_reason TEXT, gift_url TEXT UNIQUE, issued_at REAL,
                activated_at REAL, refund_ref TEXT UNIQUE, refunded_at REAL,
                created_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_order ON orders(user_id)
                WHERE status IN ('awaiting_payment','review','rejected','paid','delivery_pending');
            CREATE INDEX IF NOT EXISTS orders_status ON orders(status, created_at);
            CREATE TABLE IF NOT EXISTS receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL REFERENCES orders(id),
                user_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                file_id TEXT NOT NULL, unique_id TEXT NOT NULL, kind TEXT NOT NULL,
                created_at REAL NOT NULL, UNIQUE(user_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS receipt_fingerprint ON receipts(unique_id);
            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
                data TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ui_state (
                user_id INTEGER PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                order_id INTEGER REFERENCES orders(id), status TEXT NOT NULL DEFAULT 'open',
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
                sender_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                direction TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL,
                UNIQUE(sender_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, actor INTEGER NOT NULL,
                order_id INTEGER, action TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
                chat_id INTEGER NOT NULL, method TEXT NOT NULL, payload TEXT NOT NULL,
                order_id INTEGER, purpose TEXT NOT NULL DEFAULT 'notification',
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                sent_at REAL, last_error TEXT, parked INTEGER NOT NULL DEFAULT 0
            );
        """)
        defaults = {"price": config.price, "delivery_time": config.delivery_time,
                    "sales_open": config.sales_open}
        for key, value in defaults.items():
            self.db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, json.dumps(value)))
        self.migrate_contact_and_delivery()

    def migrate_contact_and_delivery(self):
        """Upgrade existing shops once without resetting later admin changes."""
        if self.db.execute("PRAGMA user_version").fetchone()[0] >= 1:
            return
        previous = "Час видачі уточнюйте в адміністратора перед оплатою."
        with self.transaction():
            if self.setting("delivery_time") == previous:
                self.db.execute("UPDATE settings SET value=? WHERE key='delivery_time'",
                                (json.dumps(self.config.delivery_time),))
            self.db.execute("""UPDATE orders SET delivery_time=? WHERE delivery_time=?
                AND status IN ('awaiting_payment','review','rejected','paid','delivery_pending')""",
                            (self.config.delivery_time, previous))
            for row in self.db.execute("SELECT id,payload FROM outbox WHERE sent_at IS NULL").fetchall():
                body = json.loads(row["payload"])
                for field in ("text", "caption"):
                    if field in body:
                        body[field] = body[field].replace(previous, self.config.delivery_time)
                if "reply_markup" in body:
                    rows = []
                    for buttons in body["reply_markup"]["inline_keyboard"]:
                        updated = []
                        for button in buttons:
                            action = button.get("callback_data", "")
                            if action == "terms":
                                continue
                            if action.startswith("support:"):
                                button.pop("callback_data")
                                button["url"] = self.config.admin_contact_url
                            updated.append(button)
                        if updated:
                            rows.append(updated)
                    body["reply_markup"]["inline_keyboard"] = rows
                self.db.execute("UPDATE outbox SET payload=? WHERE id=?",
                                (json.dumps(body, ensure_ascii=False), row["id"]))
            self.db.execute("DELETE FROM sessions WHERE kind='purchase_confirm'")
            self.db.execute("PRAGMA user_version=1")

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def admin(self, actor):
        if actor not in self.config.admin_ids:
            raise ShopError("Дія доступна лише адміністратору.")

    def user(self, uid, name, username=None, source="direct"):
        # Registration and its notification must commit together. Repeated
        # updates (including after a restart) only refresh the user's profile.
        with self.transaction():
            created = self.db.execute(
                "INSERT INTO users VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (uid, username, name, source, time.time()),
            ).rowcount == 1
            if not created:
                self.db.execute("UPDATE users SET username=?,name=? WHERE id=?", (username, name, uid))
                return False
            if uid not in self.config.admin_ids:
                handle = f"@{escape(username)}" if username else "не вказано"
                origin = "Прямий перехід" if source == "direct" else escape(source)
                profile = f"https://t.me/{username}" if username else f"tg://user?id={uid}"
                self.notify_admins(
                    f"new_user:{uid}",
                    f"👤 <b>Новий користувач у боті!</b>\n\n"
                    f"Ім’я: {escape(name)}\n"
                    f"Username: {handle}\n"
                    f"Telegram ID: <code>{uid}</code>\n"
                    f"Джерело: {origin}",
                    keyboard((("👤 Відкрити профіль", profile),)),
                )
            return True

    def setting(self, key):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0])

    def set_setting(self, actor, key, value):
        self.admin(actor)
        if key == "price":
            if type(value) is not int or not 1 <= value <= 100000:
                raise ShopError("Ціна має бути цілим числом від 1 до 100000 грн.")
        elif key == "delivery_time":
            if not isinstance(value, str) or not 3 <= len(value) <= 300:
                raise ShopError("Вкажіть час видачі текстом: від 3 до 300 символів.")
        elif key == "sales_open":
            if type(value) is not bool:
                raise ShopError("Невірне значення налаштування.")
        else:
            raise ShopError("Невідоме налаштування.")
        self.db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(value), key))

    def session(self, uid):
        row = self.db.execute("SELECT * FROM sessions WHERE user_id=?", (uid,)).fetchone()
        if not row:
            return None
        if time.time() - row["created_at"] > 3600:
            self.clear_session(uid)
            return None
        return row["kind"], json.loads(row["data"])

    def set_session(self, uid, kind, data):
        self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)",
                        (uid, kind, json.dumps(data), time.time()))

    def clear_session(self, uid):
        self.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))

    def screen(self, uid):
        row = self.db.execute("SELECT data FROM ui_state WHERE user_id=?", (uid,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_screen(self, uid, data):
        self.db.execute("INSERT OR REPLACE INTO ui_state VALUES (?,?)", (uid, json.dumps(data)))

    def _order(self, oid):
        row = self.db.execute("SELECT o.*,u.name,u.username FROM orders o JOIN users u ON u.id=o.user_id WHERE o.id=?", (oid,)).fetchone()
        if not row:
            raise ShopError("Замовлення не знайдено.")
        return dict(row)

    def own_order(self, uid, oid):
        order = self._order(oid)
        if order["user_id"] != uid:
            raise ShopError("Замовлення недоступне.")
        return order

    def admin_order(self, actor, oid):
        self.admin(actor)
        return self._order(oid)

    def audit(self, actor, oid, action):
        self.db.execute("INSERT INTO audit(actor,order_id,action,created_at) VALUES (?,?,?,?)",
                        (actor, oid, action, time.time()))

    def queue(self, key, chat_id, text=None, markup=None, *, method="send_message", payload=None, oid=None, purpose="notification"):
        body = dict(payload or {})
        if text is not None:
            body.update(text=text, parse_mode="HTML", link_preview_options={"is_disabled": True})
        if markup is not None:
            body["reply_markup"] = markup
        self.db.execute("INSERT OR IGNORE INTO outbox(event_key,chat_id,method,payload,order_id,purpose) VALUES (?,?,?,?,?,?)",
                        (key, chat_id, method, json.dumps(body, ensure_ascii=False), oid, purpose))

    def notify_admins(self, key, text, markup=None, oid=None):
        for admin in self.config.admin_ids:
            self.queue(f"{key}:admin:{admin}", admin, text, markup, oid=oid)

    def new_order(self, uid):
        with self.transaction():
            active = self.db.execute("SELECT id FROM orders WHERE user_id=? AND status IN ('awaiting_payment','review','rejected','paid','delivery_pending')", (uid,)).fetchone()
            if active:
                return self.own_order(uid, active[0]), False
            if not self.setting("sales_open"):
                raise ShopError("Продажі тимчасово призупинено. Ви можете написати адміністратору.")
            user = self.db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            cur = self.db.execute("""INSERT INTO orders(user_id,price,card,recipient,bank,delivery_time,terms,source,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (uid, self.setting("price"), self.config.card,
                self.config.recipient, self.config.bank, self.setting("delivery_time"), "",
                user["source"], time.time()))
            self.audit(uid, cur.lastrowid, "order_created")
            return self._order(cur.lastrowid), True

    def cancel_order(self, uid, oid):
        with self.transaction():
            order = self.own_order(uid, oid)
            if order["status"] != "awaiting_payment":
                raise ShopError("Це замовлення не можна скасувати самостійно. Напишіть адміністратору.")
            self.db.execute("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
            self.audit(uid, oid, "cancelled_by_buyer")

    def add_receipt(self, uid, oid, message_id, file_id, unique_id, kind):
        if kind not in ("photo", "document"):
            raise ShopError("Надішліть фото або PDF квитанції.")
        with self.transaction():
            order = self.own_order(uid, oid)
            old = self.db.execute("SELECT id FROM receipts WHERE user_id=? AND message_id=?", (uid, message_id)).fetchone()
            if old:
                return False
            if order["status"] not in ("awaiting_payment", "rejected"):
                raise ShopError("Квитанція вже на перевірці або оплату підтверджено. Для уточнень напишіть адміністратору.")
            if self.db.execute("SELECT id FROM receipts WHERE unique_id=? AND order_id<>?", (unique_id, oid)).fetchone():
                raise ShopError("Цей файл уже додано до іншого замовлення. Напишіть адміністратору.")
            rid = self.db.execute("INSERT INTO receipts(order_id,user_id,message_id,file_id,unique_id,kind,created_at) VALUES (?,?,?,?,?,?,?)",
                                 (oid, uid, message_id, file_id, unique_id, kind, time.time())).lastrowid
            self.db.execute("UPDATE orders SET status='review',rejection_reason=NULL WHERE id=?", (oid,))
            order = self._order(oid)
            caption = (f"📎 <b>Квитанція до замовлення №{oid}</b>\n"
                       f"Покупець: {escape(order['name'])} (ID {uid})\n"
                       f"Сума: {order['price']} грн\n"
                       "Перевірте фактичне зарахування в банку перед підтвердженням.")
            for admin in self.config.admin_ids:
                self.queue(f"receipt:{rid}:{admin}", admin, method=f"send_{kind}",
                           payload={kind: file_id, "caption": caption, "parse_mode": "HTML", "protect_content": True},
                           markup=order_keys(order, True), oid=oid)
            self.queue(f"receipt:{rid}:buyer", uid,
                       f"✅ Квитанцію до замовлення №{oid} отримано.\nАдміністратор перевірить оплату.\n"
                       f"Час видачі: {escape(order['delivery_time'])}", order_keys(order, contact_url=self.config.admin_contact_url), oid=oid)
            self.clear_session(uid)
            self.audit(uid, oid, "receipt_submitted")
            return True

    def receipts(self, actor, oid):
        self.admin_order(actor, oid)
        return [dict(r) for r in self.db.execute("SELECT * FROM receipts WHERE order_id=? ORDER BY id", (oid,))]

    @staticmethod
    def reference(value):
        value = " ".join(value.strip().casefold().split())
        if not 4 <= len(value) <= 150:
            raise ShopError("Вкажіть ідентифікатор банківської операції: 4–150 символів.")
        return value

    def confirm_payment(self, actor, oid, reference, receipt_id=None):
        self.admin(actor)
        ref = self.reference(reference)
        try:
            with self.transaction():
                order = self._order(oid)
                if order["status"] != "review":
                    raise ShopError("Підтвердження можливе лише для квитанції на перевірці. Оновіть замовлення.")
                latest = self.db.execute("SELECT MAX(id) FROM receipts WHERE order_id=?", (oid,)).fetchone()[0]
                if receipt_id is not None and latest != receipt_id:
                    raise ShopError("Покупець надіслав нову квитанцію. Перевірте її заново.")
                self.db.execute("UPDATE orders SET status='paid',payment_ref=?,paid_by=?,paid_at=? WHERE id=?",
                                (ref, actor, time.time(), oid))
                order = self._order(oid)
                self.queue(f"paid:{oid}", order["user_id"],
                           f"✅ Оплату замовлення №{oid} підтверджено.\nАдміністратор готує подарункове посилання.\n"
                           f"Час видачі: {escape(order['delivery_time'])}", order_keys(order, contact_url=self.config.admin_contact_url), oid=oid)
                self.notify_admins(f"paid:{oid}", f"✅ Замовлення №{oid}: оплату підтверджено. Можна видати посилання.", order_keys(order, True), oid)
                self.audit(actor, oid, "payment_confirmed")
                self.clear_session(actor)
        except sqlite3.IntegrityError:
            raise ShopError("Цей ідентифікатор банківської операції вже використано. Перевірте платіж.") from None

    def reject_receipt(self, actor, oid, reason, receipt_id=None):
        self.admin(actor)
        if not 3 <= len(reason.strip()) <= 1000:
            raise ShopError("Вкажіть причину відхилення: 3–1000 символів.")
        with self.transaction():
            order = self._order(oid)
            if order["status"] != "review":
                raise ShopError("Квитанція вже опрацьована. Оновіть замовлення.")
            latest = self.db.execute("SELECT MAX(id) FROM receipts WHERE order_id=?", (oid,)).fetchone()[0]
            if receipt_id is not None and latest != receipt_id:
                raise ShopError("Покупець надіслав нову квитанцію. Перевірте її заново.")
            self.db.execute("UPDATE orders SET status='rejected',rejection_reason=? WHERE id=?", (reason.strip(), oid))
            rid = self.db.execute("SELECT MAX(id) FROM receipts WHERE order_id=?", (oid,)).fetchone()[0]
            self.queue(f"rejected:{rid}", order["user_id"],
                       f"❌ Квитанцію до замовлення №{oid} відхилено.\nПричина: {escape(reason.strip())}\n\n"
                       "Надішліть нову квитанцію або зверніться до адміністратора.", order_keys(self._order(oid), contact_url=self.config.admin_contact_url), oid=oid)
            self.audit(actor, oid, "receipt_rejected")
            self.clear_session(actor)

    def validate_gift(self, raw):
        raw = raw.strip()
        if not 10 <= len(raw) <= 1800 or any(c.isspace() or ord(c) < 32 for c in raw):
            raise ShopError("Надішліть одне HTTPS-посилання без додаткового тексту (до 1800 символів).")
        try:
            parts = urlsplit(raw)
            host = (parts.hostname or "").lower()
            if parts.scheme != "https" or not host or parts.username or parts.password or parts.port not in (None, 443):
                raise ValueError
            if host == "localhost" or "." not in host or "\\" in raw:
                raise ValueError
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None:
                raise ValueError
            if self.config.allowed_hosts and host not in self.config.allowed_hosts:
                raise ShopError("Домен посилання відсутній у GIFT_ALLOWED_HOSTS.")
            return urlunsplit(("https", host, parts.path, parts.query, parts.fragment))
        except ValueError:
            raise ShopError("Потрібне коректне HTTPS-посилання на публічному домені.") from None

    def prepare_gift(self, actor, oid, raw):
        self.admin(actor)
        order = self._order(oid)
        if order["status"] != "paid":
            raise ShopError("Посилання можна видати лише після підтвердження оплати.")
        url = self.validate_gift(raw)
        if self.db.execute("SELECT id FROM orders WHERE gift_url=?", (url,)).fetchone():
            raise ShopError("Це посилання вже закріплене за замовленням.")
        self.set_session(actor, "gift_confirm", {"oid": oid, "url": url, "nonce": secrets.token_hex(8)})
        return url

    def issue_gift(self, actor, oid, nonce=None):
        self.admin(actor)
        state = self.session(actor)
        if not state or state[0] != "gift_confirm" or state[1]["oid"] != oid or state[1]["nonce"] != nonce:
            raise ShopError("Попередній перегляд застарів. Відкрийте «Видати посилання» ще раз.")
        url = self.validate_gift(state[1]["url"])
        try:
            with self.transaction():
                order = self._order(oid)
                if order["status"] != "paid":
                    raise ShopError("Посилання вже видано або статус оплати змінився.")
                self.db.execute("UPDATE orders SET status='delivery_pending',gift_url=? WHERE id=?", (url, oid))
                order = self._order(oid)
                shown = dict(order, status="issued")
                self.queue(f"gift:{oid}", order["user_id"], gift_message(order), order_keys(shown, contact_url=self.config.admin_contact_url), oid=oid, purpose="gift")
                self.audit(actor, oid, "gift_queued")
                self.clear_session(actor)
        except sqlite3.IntegrityError:
            raise ShopError("Це посилання вже закріплене за іншим замовленням.") from None

    def activate(self, uid, oid):
        with self.transaction():
            order = self.own_order(uid, oid)
            if order["status"] == "activated":
                return
            if order["status"] != "issued":
                raise ShopError("Підтвердження доступне після видачі посилання.")
            self.db.execute("UPDATE orders SET status='activated',activated_at=? WHERE id=?", (time.time(), oid))
            self.notify_admins(f"activated:{oid}", f"✅ Покупець підтвердив активацію замовлення №{oid}.", oid=oid)
            self.audit(uid, oid, "buyer_reported_activation")

    def refund(self, actor, oid, reference):
        self.admin(actor)
        ref = self.reference(reference)
        try:
            with self.transaction():
                order = self._order(oid)
                if order["status"] not in ("paid", "issued", "activated"):
                    raise ShopError("Повернення можна зафіксувати для оплаченого або виданого замовлення.")
                self.db.execute("UPDATE orders SET status='refunded',refund_ref=?,refunded_at=? WHERE id=?", (ref, time.time(), oid))
                self.queue(f"refund:{oid}", order["user_id"],
                           f"↩️ Адміністратор підтвердив повернення {order['price']} грн за замовленням №{oid}.\n"
                           "Якщо кошти не надійшли, зверніться до підтримки.", order_keys(self._order(oid), contact_url=self.config.admin_contact_url), oid=oid)
                self.audit(actor, oid, "manual_bank_refund_recorded")
                self.clear_session(actor)
        except sqlite3.IntegrityError:
            raise ShopError("Цей ідентифікатор повернення вже використано.") from None

    def list_orders(self, uid, page=0, *, admin=False, status="all"):
        if admin:
            self.admin(uid)
            filters = {"all": "1", "review": "status='review'", "paid": "status IN ('paid','delivery_pending')"}
            if status not in filters:
                raise ShopError("Невідомий фільтр.")
            where, args = filters[status], []
        else:
            where, args = "user_id=?", [uid]
        return [dict(r) for r in self.db.execute(f"SELECT * FROM orders WHERE {where} ORDER BY id DESC LIMIT 9 OFFSET ?", (*args, max(0, page) * 8))]

    def submit_support(self, uid, oid, message_id, body):
        if oid:
            self.own_order(uid, oid)
        with self.transaction():
            duplicate = self.db.execute("SELECT ticket_id FROM support_messages WHERE sender_id=? AND message_id=?", (uid, message_id)).fetchone()
            if duplicate:
                return duplicate[0]
            recent = self.db.execute("SELECT created_at FROM support_messages WHERE sender_id=? AND direction='in' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
            if recent and time.time() - recent[0] < 5:
                raise ShopError("Зачекайте кілька секунд перед наступним повідомленням.")
            ticket = self.db.execute("SELECT id FROM tickets WHERE user_id=? AND order_id IS ? AND status='open' ORDER BY id DESC LIMIT 1", (uid, oid or None)).fetchone()
            tid = ticket[0] if ticket else self.db.execute("INSERT INTO tickets(user_id,order_id,created_at,updated_at) VALUES (?,?,?,?)", (uid, oid or None, time.time(), time.time())).lastrowid
            self.db.execute("UPDATE tickets SET updated_at=? WHERE id=?", (time.time(), tid))
            mid = self.db.execute("INSERT INTO support_messages(ticket_id,sender_id,message_id,direction,body,created_at) VALUES (?,?,?,'in',?,?)", (tid, uid, message_id, json.dumps(body), time.time())).lastrowid
            keys = keyboard((("💬 Відповісти", f"a:reply:{tid}"), ("📋 Звернення", f"a:ticket:{tid}")))
            for actor in self.config.admin_ids:
                self.queue(f"support:{mid}:intro:{actor}", actor, f"💬 Звернення №{tid} від ID {uid}" + (f" · замовлення №{oid}" if oid else ""), keys)
                self.queue_content(f"support:{mid}:body:{actor}", actor, body)
            self.clear_session(uid)
            return tid

    def queue_content(self, key, target, body, prefix="", markup=None):
        if body["kind"] == "text":
            self.queue(key, target, prefix + escape(body["text"]), markup)
        else:
            payload = {body["kind"]: body["file_id"], "caption": prefix + escape(body.get("text", "")),
                       "parse_mode": "HTML", "protect_content": True}
            self.queue(key, target, method=f"send_{body['kind']}", payload=payload, markup=markup)

    def ticket(self, actor, tid):
        self.admin(actor)
        row = self.db.execute("SELECT t.*,u.name,u.username FROM tickets t JOIN users u ON u.id=t.user_id WHERE t.id=?", (tid,)).fetchone()
        if not row:
            raise ShopError("Звернення не знайдено.")
        return dict(row)

    def ticket_messages(self, actor, tid, page=0):
        self.ticket(actor, tid)
        return [dict(r) for r in self.db.execute("SELECT * FROM support_messages WHERE ticket_id=? ORDER BY id DESC LIMIT 6 OFFSET ?", (tid, max(0, page) * 5))]

    def reply_support(self, actor, tid, message_id, body):
        self.admin(actor)
        with self.transaction():
            ticket = self.ticket(actor, tid)
            if ticket["status"] != "open":
                raise ShopError("Звернення вже закрите.")
            if self.db.execute("SELECT id FROM support_messages WHERE sender_id=? AND message_id=?", (actor, message_id)).fetchone():
                return
            mid = self.db.execute("INSERT INTO support_messages(ticket_id,sender_id,message_id,direction,body,created_at) VALUES (?,?,?,'out',?,?)", (tid, actor, message_id, json.dumps(body), time.time())).lastrowid
            self.queue_content(f"support_reply:{mid}", ticket["user_id"], body,
                               f"💬 Відповідь адміністратора · звернення №{tid}\n\n",
                               keyboard((("💬 Написати адміну", self.config.admin_contact_url),)))
            self.db.execute("UPDATE tickets SET updated_at=? WHERE id=?", (time.time(), tid))
            self.clear_session(actor)

    def contact_buyer(self, actor, oid, message_id, body):
        order = self.admin_order(actor, oid)
        with self.transaction():
            ticket = self.db.execute("SELECT id FROM tickets WHERE order_id=? AND status='open' ORDER BY id DESC LIMIT 1", (oid,)).fetchone()
            tid = ticket[0] if ticket else self.db.execute("INSERT INTO tickets(user_id,order_id,created_at,updated_at) VALUES (?,?,?,?)", (order["user_id"], oid, time.time(), time.time())).lastrowid
        self.reply_support(actor, tid, message_id, body)

    def close_ticket(self, actor, tid):
        self.ticket(actor, tid)
        self.db.execute("UPDATE tickets SET status='closed',updated_at=? WHERE id=?", (time.time(), tid))

    def tickets(self, actor, page=0):
        self.admin(actor)
        return [dict(r) for r in self.db.execute("SELECT * FROM tickets WHERE status='open' ORDER BY updated_at DESC LIMIT 9 OFFSET ?", (max(0, page) * 8,))]

    def stats(self, actor):
        self.admin(actor)
        return {
            "users": self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "statuses": [dict(r) for r in self.db.execute("SELECT status,COUNT(*) n FROM orders GROUP BY status")],
            "money": dict(self.db.execute("SELECT COALESCE(SUM(CASE WHEN paid_at IS NOT NULL THEN price ELSE 0 END),0) gross, COALESCE(SUM(CASE WHEN refunded_at IS NOT NULL THEN price ELSE 0 END),0) refunds FROM orders").fetchone()),
            "sources": [dict(r) for r in self.db.execute("""SELECT u.source,COUNT(DISTINCT u.id) users,
                COUNT(DISTINCT CASE WHEN o.paid_at IS NOT NULL THEN o.id END) payments
                FROM users u LEFT JOIN orders o ON o.user_id=u.id GROUP BY u.source ORDER BY users DESC LIMIT 20""")],
            "pending": self.db.execute("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL").fetchone()[0],
            "parked": self.db.execute("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL AND parked=1").fetchone()[0],
        }

    def due_messages(self):
        # Preserve order per recipient; a blocked buyer cannot block other recipients.
        return [dict(r) for r in self.db.execute("""SELECT o.* FROM outbox o
            WHERE sent_at IS NULL AND parked=0 AND next_attempt<=?
            AND NOT EXISTS (SELECT 1 FROM outbox prior WHERE prior.chat_id=o.chat_id
                AND prior.id<o.id AND prior.sent_at IS NULL AND prior.parked=0)
            ORDER BY id LIMIT 20""", (time.time(),))]

    def delivered(self, mid):
        with self.transaction():
            row = self.db.execute("SELECT * FROM outbox WHERE id=?", (mid,)).fetchone()
            if not row or row["sent_at"]:
                return
            self.db.execute("UPDATE outbox SET sent_at=?,last_error=NULL WHERE id=?", (time.time(), mid))
            if row["purpose"] == "gift":
                oid = row["order_id"]
                self.db.execute("UPDATE orders SET status='issued',issued_at=? WHERE id=? AND status='delivery_pending'", (time.time(), oid))
                self.audit(0, oid, "gift_delivered")
                self.notify_admins(f"issued:{oid}", f"🎁 Посилання до замовлення №{oid} доставлено покупцю.", order_keys(self._order(oid), True), oid)

    def delivery_error(self, mid, error_type, *, delay=None, permanent=False):
        with self.transaction():
            row = self.db.execute("SELECT * FROM outbox WHERE id=?", (mid,)).fetchone()
            attempts = row["attempts"] + 1
            parked = permanent or attempts >= 10
            wait = delay if delay is not None else min(300, 2 ** min(attempts, 9))
            self.db.execute("UPDATE outbox SET attempts=?,next_attempt=?,last_error=?,parked=? WHERE id=?",
                            (attempts, time.time() + wait, error_type, int(parked), mid))
            if row["purpose"] == "gift" and parked:
                oid = row["order_id"]
                self.notify_admins(f"gift_failed:{mid}:{attempts}", f"⚠️ Не вдалося доставити посилання до замовлення №{oid}. Покупець може розблокувати бот і натиснути /start; потім повторіть доставку.", order_keys(self._order(oid), True), oid)

    def retry_delivery(self, actor, oid=None):
        self.admin(actor)
        if oid is not None:
            self.admin_order(actor, oid)
            self.db.execute("UPDATE outbox SET parked=0,attempts=0,next_attempt=0 WHERE sent_at IS NULL AND order_id=?", (oid,))
        else:
            self.db.execute("UPDATE outbox SET parked=0,attempts=0,next_attempt=0 WHERE sent_at IS NULL")

    def resume_recipient(self, uid):
        self.db.execute("UPDATE outbox SET parked=0,attempts=0,next_attempt=0 WHERE chat_id=? AND sent_at IS NULL AND parked=1", (uid,))
