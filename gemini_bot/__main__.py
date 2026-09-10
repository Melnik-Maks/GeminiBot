import argparse
import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeChat

from .config import Config
from .delivery import delivery_worker
from .store import Store
from .ui import UI


class InstanceLock:
    """OS lock released automatically on process exit; only one worker per database."""
    def __init__(self, database):
        self.path = Path(database).resolve().with_suffix(".lock")
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("Інший екземпляр бота вже використовує цю базу.") from None
        return self

    def __exit__(self, *args):
        self.file.close()


async def run(config):
    bot = Bot(config.token)
    store = Store(config)
    tasks = []
    try:
        # Do not drop pending receipts on restart and do not remove webhooks silently.
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise RuntimeError("Для цього токена налаштований webhook. Приберіть його перед запуском polling.")
        commands = [BotCommand(command=c, description=d) for c, d in (
            ("start", "Головне меню"), ("orders", "Мої замовлення"),
            ("support", "Написати адміну"), ("paysupport", "Допомога з оплатою"),
            ("cancel", "Скасувати введення"),
            ("id", "Мій Telegram ID"))]
        await bot.set_my_commands(commands)
        for admin in config.admin_ids:
            try:
                await bot.set_my_commands(commands + [BotCommand(command="admin", description="Адмін-меню")],
                                          scope=BotCommandScopeChat(chat_id=admin))
            except Exception as exc:
                logging.warning("Admin must open the bot: id=%s type=%s", admin, type(exc).__name__)
        ui = UI(bot, store)
        dispatcher = Dispatcher()
        dispatcher.include_router(ui.router)
        tasks = [
            asyncio.create_task(delivery_worker(bot, store), name="delivery"),
            asyncio.create_task(dispatcher.start_polling(
                bot, handle_as_tasks=False, close_bot_session=False,
                allowed_updates=dispatcher.resolve_used_update_types()), name="polling"),
        ]
        logging.info("Bot started; sales_open=%s", store.setting("sales_open"))
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()
        store.close()


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Gemini subscription Telegram bot")
    parser.add_argument("--check", action="store_true", help="Validate .env without network calls")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = Config.from_env()
        if args.check:
            print("Configuration OK. No Telegram requests were sent.")
            return
        with InstanceLock(config.database):
            asyncio.run(run(config))
    except KeyboardInterrupt:
        pass
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")
    except Exception as exc:
        # Do not print API exception payloads: they may include private data.
        parser.exit(1, f"Bot stopped: {type(exc).__name__}. Check network, token and server configuration.\n")


if __name__ == "__main__":
    main()
