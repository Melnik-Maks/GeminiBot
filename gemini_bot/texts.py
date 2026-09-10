from html import escape

PRODUCT = "Google AI Pro (Gemini Pro) — 18 місяців"
STATUS = {
    "awaiting_payment": "Очікує оплати / квитанції",
    "review": "Квитанція на перевірці",
    "rejected": "Потрібна нова квитанція",
    "paid": "Оплату підтверджено — очікує видачі",
    "delivery_pending": "Посилання готується до доставки",
    "issued": "Посилання видано",
    "activated": "Покупець підтвердив активацію",
    "cancelled": "Скасовано без оплати",
    "refunded": "Повернення коштів підтверджено адміном",
}
INSTRUCTIONS = (
    "1. Увійдіть у потрібний Google-акаунт.\n"
    "2. Відкрийте подарункове посилання.\n"
    "3. Натисніть «Активувати» або «Прийняти пропозицію», якщо така кнопка є.\n"
    "4. Виконайте кроки на сторінці Google та підтвердьте підключення.\n"
    "5. Перевірте статус і термін підписки в налаштуваннях Google-акаунта.\n\n"
    "Активуйте пропозицію саме на тому акаунті, де плануєте користуватися Gemini Pro. "
    "Пароль або доступ до вашого акаунта передавати не потрібно."
)


def keyboard(*rows):
    return {"inline_keyboard": [[
        {"text": text, "url" if target.startswith(("https://", "tg://")) else "callback_data": target}
        for text, target in row] for row in rows]}


def home_keys(admin=False, *, contact_url):
    rows = [(("💳 Купити підписку", "buy"),),
            (("📖 Як активувати", "instructions"),),
            (("📦 Мої замовлення", "orders:0"),),
            (("💬 Написати адміну", contact_url),)]
    if admin:
        rows.append((("⚙️ Адмін-меню", "admin"),))
    return keyboard(*rows)


def back_keys(target="home"):
    row = (("← Назад", target),)
    if target != "home":
        row += (("🏠 Головна", "home"),)
    return keyboard(row)


def order_keys(order, admin=False, *, contact_url=None, back_to=None):
    oid, status = order["id"], order["status"]
    rows = []
    if admin:
        if status == "review":
            rows.extend([(("✅ Перевірив зарахування", f"a:pay:{oid}"),),
                         (("❌ Відхилити квитанцію", f"a:reject:{oid}"),)])
        if status == "paid":
            rows.append((("🎁 Видати посилання", f"a:gift:{oid}"),))
        if status == "delivery_pending":
            rows.append((("🔄 Повторити доставку", f"a:retry:{oid}"),))
        if status in ("paid", "issued", "activated"):
            rows.append((("↩️ Зафіксувати повернення", f"a:refund:{oid}"),))
        rows.extend([(("📎 Квитанції", f"a:receipts:{oid}"),),
                     (("💬 Написати покупцю", f"a:message:{oid}"),),
                     (("🔄 Оновити", f"a:order:{oid}"), ("⚙️ Меню", "admin")),
                     (("← Назад", back_to or "a:list:all:0"),)])
    else:
        if not contact_url:
            raise ValueError("Buyer buttons require an admin contact URL.")
        if status in ("awaiting_payment", "rejected"):
            rows.append((("📎 Надіслати квитанцію", f"receipt:{oid}"),))
        if status == "awaiting_payment":
            rows.append((("Скасувати (якщо не оплачували)", f"cancel:{oid}"),))
        if status == "issued":
            rows.append((("✅ Активував", f"activate:{oid}"),))
        rows.extend([(("❓ Потрібна допомога", contact_url),),
                     (("🔄 Оновити", f"order:{oid}"), ("🏠 Головна", "home")),
                     (("← Назад", back_to or "orders:0"),)])
    return keyboard(*rows)


def gift_message(order):
    return (f"🎁 <b>Ваше замовлення №{order['id']} готове!</b>\n\n"
            f"{escape(PRODUCT)}\n\n"
            f"Подарункове посилання:\n{escape(order['gift_url'])}\n\n"
            f"<b>Інструкція з активації</b>\n{INSTRUCTIONS}")
