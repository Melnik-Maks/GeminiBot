import asyncio
import json
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

log = logging.getLogger(__name__)


async def deliver_batch(bot, store):
    rows = store.due_messages()
    for row in rows:
        try:
            method = {"send_message": bot.send_message, "send_photo": bot.send_photo,
                      "send_document": bot.send_document}[row["method"]]
            await method(chat_id=row["chat_id"], **json.loads(row["payload"]))
        except TelegramRetryAfter as exc:
            store.delivery_error(row["id"], type(exc).__name__, delay=exc.retry_after + 1)
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            store.delivery_error(row["id"], type(exc).__name__, permanent=True)
            log.warning("Delivery parked: id=%s type=%s", row["id"], type(exc).__name__)
        except Exception as exc:
            # No raw exception text: API errors can contain private gift URLs or tokens.
            store.delivery_error(row["id"], type(exc).__name__)
            log.warning("Delivery will retry: id=%s type=%s", row["id"], type(exc).__name__)
        else:
            store.delivered(row["id"])
    return len(rows)


async def delivery_worker(bot, store):
    while True:
        await deliver_batch(bot, store)
        await asyncio.sleep(1)
