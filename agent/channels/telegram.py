from __future__ import annotations

import asyncio
import os
import threading

from agent.channels.base import Channel, ChannelHealth, ChannelMessage, NotConfigured, SendResult
from agent.connections import ConnectionStore


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, object] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover
            error["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error["error"]
    return result.get("value")


def _require_bot_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise NotConfigured("TELEGRAM_BOT_TOKEN is not set")
    return token


class TelegramChannel(Channel):
    name = "telegram"

    async def _send_async(self, handle: str, text: str) -> SendResult:
        from telegram import Bot

        bot = Bot(token=_require_bot_token())
        message = await bot.send_message(
            chat_id=int(handle),
            text=text,
            read_timeout=12,
            write_timeout=12,
            connect_timeout=12,
            pool_timeout=12,
        )
        return SendResult(
            ok=True,
            channel=self.name,
            handle=handle,
            message_id=str(message.message_id),
        )

    async def _read_async(self, handle: str, limit: int) -> list[ChannelMessage]:
        from telegram import Bot

        bot = Bot(token=_require_bot_token())
        updates = await bot.get_updates(
            timeout=12,
            limit=max(1, limit * 3),
            read_timeout=12,
            write_timeout=12,
            connect_timeout=12,
            pool_timeout=12,
        )
        chat_id = str(handle)
        messages: list[ChannelMessage] = []
        for update in reversed(updates):
            message = getattr(update, "message", None)
            if not message or str(message.chat_id) != chat_id:
                continue
            text = getattr(message, "text", None) or ""
            if not text:
                continue
            messages.append(
                ChannelMessage(
                    handle=chat_id,
                    text=text,
                    direction="outbound" if getattr(message.from_user, "is_bot", False) else "inbound",
                    sent_at=message.date.isoformat() if getattr(message, "date", None) else None,
                    message_id=str(message.message_id),
                    channel=self.name,
                )
            )
            if len(messages) >= limit:
                break
        return list(reversed(messages))

    async def _health_async(self) -> ChannelHealth:
        from telegram import Bot

        if not self.is_configured():
            return ChannelHealth(configured=False, healthy=False, detail="Telegram bot token missing")
        bot = Bot(token=_require_bot_token())
        me = await bot.get_me()
        return ChannelHealth(
            configured=True,
            healthy=True,
            detail=f"Connected as @{me.username or me.first_name or 'telegram-bot'}",
        )

    def send(self, handle: str, text: str) -> SendResult:
        return _run_async(self._send_async(handle, text))

    def read_recent(self, handle: str, limit: int = 50) -> list[ChannelMessage]:
        return _run_async(self._read_async(handle, limit))

    def health_check(self) -> ChannelHealth:
        try:
            return _run_async(self._health_async())
        except Exception as exc:
            return ChannelHealth(configured=self.is_configured(), healthy=False, detail=str(exc))

    def connect_instructions(self) -> str:
        return "Create a Telegram bot with BotFather and set `TELEGRAM_BOT_TOKEN`."

    def is_configured(self) -> bool:
        store = ConnectionStore()
        return bool(os.environ.get("TELEGRAM_BOT_TOKEN") or store.get_credential(self.name, "TELEGRAM_BOT_TOKEN"))
