from dataclasses import dataclass
import os
from pathlib import Path
import re


@dataclass(frozen=True)
class Config:
    token: str
    admin_ids: tuple[int, ...]
    card: str
    recipient: str
    bank: str = ""
    price: int = 200
    delivery_time: str = "1–5 хв"
    admin_username: str = ""
    database: str = "data/bot.sqlite3"
    allowed_hosts: tuple[str, ...] = ()
    sales_open: bool = False

    @property
    def admin_contact_url(self):
        if self.admin_username:
            return f"https://t.me/{self.admin_username}"
        return f"tg://user?id={self.admin_ids[0]}"

    @classmethod
    def from_env(cls):
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        token = os.getenv("BOT_TOKEN", "").strip()
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{30,}", token):
            raise ValueError("Заповніть BOT_TOKEN у .env.")
        raw_ids = os.getenv("ADMIN_IDS", "")
        try:
            ids = tuple(dict.fromkeys(int(x.strip()) for x in raw_ids.split(",") if x.strip()))
        except ValueError:
            raise ValueError("ADMIN_IDS має містити числові Telegram ID через кому.") from None
        if not ids or any(x <= 0 for x in ids):
            raise ValueError("Заповніть ADMIN_IDS додатними числовими Telegram ID.")
        admin_username = os.getenv("ADMIN_USERNAME", "").strip().lstrip("@")
        if admin_username and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", admin_username):
            raise ValueError("ADMIN_USERNAME має містити Telegram username без посилання.")
        card = re.sub(r"[ -]", "", os.getenv("PAYMENT_CARD", ""))
        if not re.fullmatch(r"[0-9]{16}", card):
            raise ValueError("PAYMENT_CARD має містити 16 цифр.")
        recipient = os.getenv("PAYMENT_RECIPIENT", "").strip()
        if not recipient:
            raise ValueError("Заповніть PAYMENT_RECIPIENT.")
        price = int(os.getenv("PRICE_UAH", "200"))
        if not 1 <= price <= 100000:
            raise ValueError("PRICE_UAH має бути в межах 1–100000.")
        for key, maximum in (("PAYMENT_RECIPIENT", 150), ("PAYMENT_BANK", 80), ("DELIVERY_TIME", 300)):
            value = os.getenv(key)
            if value is not None and (not value.strip() and key != "PAYMENT_BANK" or len(value) > maximum):
                raise ValueError(f"Перевірте {key}: текст має містити 1–{maximum} символів.")
        return cls(token=token, admin_ids=ids, card=card, recipient=recipient,
                   bank=os.getenv("PAYMENT_BANK", "").strip(), price=price,
                   delivery_time=os.getenv("DELIVERY_TIME", cls.delivery_time).strip(),
                   admin_username=admin_username,
                   database=os.getenv("DATABASE_PATH", "data/bot.sqlite3"),
                   allowed_hosts=tuple(x.strip().lower() for x in os.getenv("GIFT_ALLOWED_HOSTS", "").split(",") if x.strip()),
                   sales_open=os.getenv("SALES_OPEN", "false").lower() == "true")
