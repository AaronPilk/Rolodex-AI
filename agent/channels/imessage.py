from __future__ import annotations

from agent.channels.base import Channel, ChannelHealth, ChannelMessage, SendResult


class IMessageChannel(Channel):
    name = "imessage"

    def send(self, handle: str, text: str) -> SendResult:
        from agent.imessage_sender import send_imessage

        receipt = send_imessage(handle, text, timeout=12.0)
        return SendResult(
            ok=True,
            channel=receipt.channel,
            handle=handle,
            message_id=receipt.provider_id,
        )

    def read_recent(self, handle: str, limit: int = 50) -> list[ChannelMessage]:
        from agent.imessage_reader import list_threads, read_thread

        normalized = handle.strip().lower()
        for thread in list_threads(limit=200):
            handles = [item.strip().lower() for item in thread.handles]
            if normalized not in handles:
                continue
            return [
                ChannelMessage(
                    handle=message.handle,
                    text=message.text,
                    direction="outbound" if message.is_from_me else "inbound",
                    sent_at=message.sent_at.isoformat(),
                    message_id=str(message.rowid),
                    channel=self.name,
                )
                for message in reversed(read_thread(thread.chat_id, limit=limit))
            ]
        return []

    def health_check(self) -> ChannelHealth:
        from agent.imessage_reader import health_check as reader_health_check
        from agent.imessage_sender import health_check as sender_health_check

        readable, detail = reader_health_check()
        sender = sender_health_check()
        healthy = readable and bool(sender.get("osascript_available"))
        return ChannelHealth(
            configured=True,
            healthy=healthy,
            detail=detail,
        )

    def connect_instructions(self) -> str:
        return (
            "Enable Messages.app and grant Full Disk Access plus Automation permission "
            "for the Python process running Rolodex AI."
        )

    def is_configured(self) -> bool:
        return True
