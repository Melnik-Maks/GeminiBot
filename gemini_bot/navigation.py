"""One persistent menu message per private chat, with temporary attachments."""
import logging

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InputMediaPhoto

log = logging.getLogger(__name__)


class Navigation:
    def __init__(self, bot, store):
        self.bot, self.store = bot, store

    async def remove(self, uid, message_id):
        try:
            await self.bot.delete_message(chat_id=uid, message_id=message_id)
            return True
        except TelegramBadRequest as exc:
            if "message to delete not found" in exc.message.lower():
                return True
            # Telegram may forbid deleting an old message; retire its buttons.
            try:
                await self.bot.edit_message_reply_markup(chat_id=uid, message_id=message_id, reply_markup=None)
                return True
            except TelegramAPIError:
                return False
        except TelegramAPIError as exc:
            log.warning("Menu cleanup failed: type=%s", type(exc).__name__)
            return False

    async def begin(self, uid, clicked=None):
        state = self.store.screen(uid)
        if clicked is not None:
            previous = state.get("message_id")
            if previous and previous != clicked.message_id:
                state.setdefault("extras", []).append(previous)
                state.pop("photo", None)
            state["message_id"] = clicked.message_id
            state["media"] = not bool(getattr(clicked, "text", None))
        remaining = []
        for message_id in dict.fromkeys(state.get("extras", [])):
            if message_id != state.get("message_id") and not await self.remove(uid, message_id):
                remaining.append(message_id)
        state["extras"] = remaining
        self.store.save_screen(uid, state)

    async def render(self, uid, text, markup, *, new_message=False, photo=None):
        state = self.store.screen(uid)
        previous = state.get("message_id")
        options = dict(parse_mode="HTML", reply_markup=InlineKeyboardMarkup.model_validate(markup))
        if photo:
            options["caption"] = text
        else:
            options.update(text=text, link_preview_options={"is_disabled": True})
        can_edit = state.get("photo") == str(photo) if photo else not state.get("media")
        if previous and can_edit and not new_message:
            try:
                method = self.bot.edit_message_caption if photo else self.bot.edit_message_text
                return await method(chat_id=uid, message_id=previous, **options)
            except TelegramBadRequest as exc:
                reason = exc.message.lower()
                if "message is not modified" in reason:
                    return None
                if not any(marker in reason for marker in (
                    "message to edit not found", "message can't be edited",
                    "message can not be edited", "there is no text in the message",
                )):
                    raise
        # Send successfully before removing the old screen, so failures leave
        # the existing navigation available. Never delete user-sent messages.
        if photo:
            message = await self.bot.send_photo(chat_id=uid, photo=FSInputFile(photo), **options)
        else:
            message = await self.bot.send_message(chat_id=uid, **options)
        state.update(message_id=message.message_id, media=bool(photo), photo=str(photo) if photo else None)
        self.store.save_screen(uid, state)
        if previous and previous != message.message_id and not await self.remove(uid, previous):
            state.setdefault("extras", []).append(previous)
            self.store.save_screen(uid, state)
        return message

    async def album(self, uid, paths):
        messages = await self.bot.send_media_group(
            chat_id=uid, media=[InputMediaPhoto(media=FSInputFile(path)) for path in paths])
        state = self.store.screen(uid)
        state.setdefault("extras", []).extend(message.message_id for message in messages)
        self.store.save_screen(uid, state)
        return messages

    async def extra(self, uid, method, **kwargs):
        message = await method(chat_id=uid, **kwargs)
        state = self.store.screen(uid)
        state.setdefault("extras", []).append(message.message_id)
        self.store.save_screen(uid, state)
        return message

    def remember(self, uid, key, value):
        state = self.store.screen(uid)
        state[key] = value
        self.store.save_screen(uid, state)

    def recalled(self, uid, key, default):
        return self.store.screen(uid).get(key, default)
