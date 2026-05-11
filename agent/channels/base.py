from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ChannelError(RuntimeError):
    """Base error for channel operations."""


class NotConfigured(ChannelError):
    """Raised when a channel is not configured."""


@dataclass(slots=True)
class ChannelMessage:
    handle: str
    text: str
    direction: str
    sent_at: str | None = None
    message_id: str | None = None
    channel: str | None = None
    raw: dict | None = None


@dataclass(slots=True)
class SendResult:
    ok: bool
    channel: str
    handle: str
    message_id: str | None = None
    error: str | None = None
    raw: dict | None = None


@dataclass(slots=True)
class ChannelHealth:
    configured: bool
    healthy: bool
    detail: str = ""
    instructions_url: str | None = None
    meta: dict[str, str] = field(default_factory=dict)


class Channel(ABC):
    name: str

    @abstractmethod
    def send(self, handle: str, text: str) -> SendResult:
        raise NotImplementedError

    @abstractmethod
    def read_recent(self, handle: str, limit: int = 50) -> list[ChannelMessage]:
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> ChannelHealth:
        raise NotImplementedError

    @abstractmethod
    def connect_instructions(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def is_configured(self) -> bool:
        raise NotImplementedError
