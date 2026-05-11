"""
telegram_bot.py — standalone Telegram bot for daily digest delivery + approvals.

Replaces PILK's `telegram_notify` + the rolodex callback handler that lived inside
PILK's approval queue.

What it does:
- Sends the daily digest message to the configured chat as a single message with
  inline buttons (📤 Send / ✏️ Edit / ⏭️ Skip / 💤 Snooze) per candidate.
- Listens for button callbacks and dispatches to the agent's action handlers.
- Provides a /status command to query the agent's health from Telegram.

Required env vars:
- TELEGRAM_BOT_TOKEN — your bot token from @BotFather
- TELEGRAM_CHAT_ID — your own user ID (the bot only talks to this chat ID)

Optional:
- TELEGRAM_QUIET_HOURS — "22-7" means don't send between 10pm and 7am local

NOTE: This module imports python-telegram-bot at call time so the daemon can
start even if the package isn't installed yet (you'd just lose Telegram delivery).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

ActionHandler = Callable[[str, str, str], Awaitable[str]]
"""Action handler signature: (action, person_id, run_id) -> reply_text"""


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str
    quiet_hours: tuple[int, int] | None = None  # (start_hour, end_hour) in local time


def load_config() -> TelegramConfig:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError(
            "Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
        )
    qh = os.environ.get("ROLODEX_QUIET_HOURS") or os.environ.get("TELEGRAM_QUIET_HOURS")
    quiet_hours = None
    if qh and "-" in qh:
        try:
            a, b = qh.split("-")
            quiet_hours = (int(a), int(b))
        except ValueError:
            pass
    return TelegramConfig(bot_token=token, chat_id=chat_id, quiet_hours=quiet_hours)


def is_quiet_now(cfg: TelegramConfig, now: datetime | None = None) -> bool:
    if not cfg.quiet_hours:
        return False
    now = now or datetime.now()
    h = now.hour
    start, end = cfg.quiet_hours
    if start <= end:
        return start <= h < end
    return h >= start or h < end


async def send_digest(
    text: str,
    *,
    candidates: list[dict],
    cfg: TelegramConfig | None = None,
) -> dict:
    """
    Send a digest message with one inline-keyboard row per candidate.

    `candidates` is a list of dicts each with at least:
        person_id: str
        display_name: str
        run_id: str

    Returns the Telegram message dict on success.
    """
    cfg = cfg or load_config()
    if is_quiet_now(cfg):
        return {"ok": False, "reason": "quiet hours"}

    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    for c in candidates:
        pid = c["person_id"]
        rid = c["run_id"]
        rows.append(
            [
                InlineKeyboardButton("📤 Send", callback_data=f"send|{pid}|{rid}"),
                InlineKeyboardButton("✏️ Edit", callback_data=f"edit|{pid}|{rid}"),
                InlineKeyboardButton("⏭️ Skip", callback_data=f"skip|{pid}|{rid}"),
                InlineKeyboardButton("💤 Snooze", callback_data=f"snooze|{pid}|{rid}"),
            ]
        )
    markup = InlineKeyboardMarkup(rows)

    bot = Bot(token=cfg.bot_token)
    msg = await bot.send_message(
        chat_id=cfg.chat_id,
        text=text,
        reply_markup=markup,
    )
    return {"ok": True, "message_id": msg.message_id}


async def send_simple(text: str, *, cfg: TelegramConfig | None = None) -> dict:
    """Send a plain text message (used for errors / health alerts)."""
    cfg = cfg or load_config()
    if is_quiet_now(cfg):
        return {"ok": False, "reason": "quiet hours"}

    from telegram import Bot

    bot = Bot(token=cfg.bot_token)
    msg = await bot.send_message(chat_id=cfg.chat_id, text=text)
    return {"ok": True, "message_id": msg.message_id}


async def run_callback_listener(handler: ActionHandler) -> None:
    """
    Long-running listener for button callbacks. Call from the daemon main loop.

    `handler` is called with (action, person_id, run_id) and should return a string
    to display as a confirmation toast back in Telegram.
    """
    from telegram import Update
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

    cfg = load_config()
    app = Application.builder().token(cfg.bot_token).build()

    async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not q or not q.data:
            return
        if str(q.from_user.id) != cfg.chat_id:
            await q.answer("Not authorized.", show_alert=True)
            return
        try:
            action, person_id, run_id = q.data.split("|", 2)
        except ValueError:
            await q.answer("Bad action.", show_alert=True)
            return
        try:
            reply = await handler(action, person_id, run_id)
            await q.answer(reply, show_alert=False)
        except Exception as e:
            await q.answer(f"Failed: {e}", show_alert=True)

    async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if str(update.effective_user.id) != cfg.chat_id:
            return
        # Stub — the agent's status assembler should be wired in by the daemon.
        await update.message.reply_text("Status command received. (Wire up via daemon.)")

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(CommandHandler("status", on_status))

    await app.run_polling()


def health_check() -> tuple[bool, str]:
    try:
        load_config()
        return True, "Telegram configured"
    except RuntimeError as e:
        return False, str(e)
