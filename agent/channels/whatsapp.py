from __future__ import annotations

import os

from agent.channels.base import Channel, ChannelHealth, ChannelMessage, NotConfigured, SendResult
from agent.connections import ConnectionStore


class WhatsAppChannel(Channel):
    name = "whatsapp"

    def send(self, handle: str, text: str) -> SendResult:
        if not self.is_configured():
            raise NotConfigured("Twilio WhatsApp credentials are missing")
        if not handle.startswith("whatsapp:+"):
            raise ValueError("WhatsApp handle must use whatsapp:+1... format")
        from twilio.rest import Client

        client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
        message = client.messages.create(
            body=text,
            from_=os.environ["TWILIO_FROM_NUMBER"],
            to=handle,
        )
        return SendResult(
            ok=True,
            channel=self.name,
            handle=handle,
            message_id=str(message.sid),
        )

    def read_recent(self, handle: str, limit: int = 50) -> list[ChannelMessage]:
        return []

    def health_check(self) -> ChannelHealth:
        if not self.is_configured():
            return ChannelHealth(configured=False, healthy=False, detail="Twilio env vars missing")
        try:
            from twilio.rest import Client

            client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
            account = client.api.accounts(os.environ["TWILIO_ACCOUNT_SID"]).fetch()
            return ChannelHealth(configured=True, healthy=True, detail=f"Twilio {account.status}")
        except Exception as exc:
            return ChannelHealth(configured=True, healthy=False, detail=str(exc))

    def connect_instructions(self) -> str:
        return "Configure Twilio WhatsApp sandbox and set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_FROM_NUMBER`."

    def is_configured(self) -> bool:
        store = ConnectionStore()
        return all(
            os.environ.get(key) or store.get_credential(self.name, key)
            for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")
        )
