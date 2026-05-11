from __future__ import annotations

from agent.channels.base import ChannelHealth
from agent.channels.facebook import FacebookChannel
from agent.channels.imessage import IMessageChannel
from agent.channels.instagram import InstagramChannel
from agent.channels.telegram import TelegramChannel
from agent.channels.whatsapp import WhatsAppChannel
from agent.channels.x import XChannel

_CHANNELS = {
    "imessage": IMessageChannel,
    "telegram": TelegramChannel,
    "whatsapp": WhatsAppChannel,
    "instagram": InstagramChannel,
    "facebook": FacebookChannel,
    "x": XChannel,
}


def get_channel(name: str):
    key = name.strip().lower()
    if key not in _CHANNELS:
        raise KeyError(f"Unknown channel: {name}")
    return _CHANNELS[key]()


def available_channels() -> list[str]:
    return list(_CHANNELS.keys())


def channel_health() -> dict[str, ChannelHealth]:
    return {name: get_channel(name).health_check() for name in available_channels()}
