from __future__ import annotations

import urllib.parse
from typing import Any

from agent.channels.base import Channel, ChannelMessage, NotConfigured, SendResult
from agent.connections import ConnectionStore
from agent.channels.meta_common import MetaChannelMixin, request_json


class InstagramChannel(MetaChannelMixin, Channel):
    name = "instagram"
    env_token_name = "META_PAGE_ACCESS_TOKEN"
    env_account_name = "META_IG_BUSINESS_ID"
    platform = "instagram"
    # Required scopes for the "API setup with Facebook login" path of the Instagram
    # API use case. Meta now rejects tokens missing pages_read_engagement,
    # pages_show_list, or business_management with error (#3) "Application does
    # not have the capability to make this API call."
    oauth_scope = (
        "instagram_basic,"
        "instagram_manage_messages,"
        "pages_read_engagement,"
        "pages_show_list,"
        "business_management,"
        "pages_manage_metadata,"
        "pages_messaging"
    )

    def send(self, handle: str, text: str) -> SendResult:
        if not self.is_configured():
            raise NotConfigured("Meta Instagram messaging is not configured")
        payload = {
            "recipient": {"id": handle},
            "messaging_type": "RESPONSE",
            "message": {"text": text},
        }
        data = request_json(
            self._graph_url("me/messages"),
            method="POST",
            body=payload,
        )
        return SendResult(
            ok=True,
            channel=self.name,
            handle=handle,
            message_id=str(data.get("message_id") or data.get("recipient_id") or ""),
            raw=data,
        )

    def list_conversations(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.is_configured():
            raise NotConfigured("Meta Instagram messaging is not configured")
        page_id = self._self_graph_id()
        # Meta returns 500 "reduce the amount of data" if you ask for 20 convos
        # x 3 messages x full participant expansion. Trim hard: 10 convos, 1
        # preview message, explicit participant subfields.
        capped = max(1, min(limit, 10))
        data = request_json(
            self._graph_url(
                "me/conversations",
                platform=self.platform,
                fields="participants{id,username,name},messages.limit(1){message,created_time,from},updated_time",
                limit=str(capped),
            )
        )
        conversations: list[dict[str, Any]] = []
        for convo in data.get("data", []):
            participants = convo.get("participants", {}).get("data", [])
            participant = next((item for item in participants if str(item.get("id") or "") != page_id), None)
            participant_id = str((participant or {}).get("id") or "")
            messages = convo.get("messages", {}).get("data", [])
            last_item = messages[0] if messages else {}
            sender_id = str(last_item.get("from", {}).get("id") or "")
            conversations.append(
                {
                    "id": str(convo.get("id") or ""),
                    "participant_id": participant_id,
                    "participant_username": (
                        str((participant or {}).get("username") or "")
                        or str((participant or {}).get("name") or "")
                        or None
                    ),
                    "last_message": {
                        "text": str(last_item.get("message") or ""),
                        "from_them": bool(sender_id and sender_id != page_id),
                        "at": last_item.get("created_time"),
                    },
                    "message_count": len(messages),
                    "updated_time": convo.get("updated_time"),
                }
            )
        return conversations

    def read_recent(self, handle: str, limit: int = 50) -> list[ChannelMessage]:
        if not self.is_configured():
            raise NotConfigured("Meta Instagram messaging is not configured")
        data = request_json(
            self._graph_url(
                "me/conversations",
                platform=self.platform,
                fields="participants,messages.limit(50){message,created_time,from,id},updated_time",
                limit=str(max(1, limit)),
            )
        )
        page_id = self._self_graph_id()
        messages: list[ChannelMessage] = []
        for convo in data.get("data", []):
            participants = convo.get("participants", {}).get("data", [])
            if handle not in {str(item.get("id")) for item in participants}:
                continue
            for item in reversed(convo.get("messages", {}).get("data", [])):
                messages.append(
                    ChannelMessage(
                        handle=handle,
                        text=str(item.get("message") or ""),
                        direction="outbound" if str(item.get("from", {}).get("id")) == page_id else "inbound",
                        sent_at=item.get("created_time"),
                        message_id=str(item.get("id") or ""),
                        channel=self.name,
                        raw=item,
                    )
                )
                if len(messages) >= limit:
                    return messages
        return messages

    def connect_instructions(self) -> str:
        redirect_uri = "https://www.facebook.com/connect/login_success.html"
        encoded = urllib.parse.quote(redirect_uri, safe="")
        return (
            "Create a Meta app with Instagram messaging permissions and complete OAuth via "
            f"https://www.facebook.com/{self.graph_version}/dialog/oauth"
            f"?client_id={{APP_ID}}&redirect_uri={encoded}&scope={self.oauth_scope}&response_type=token"
        )

    def is_configured(self) -> bool:
        import os

        store = ConnectionStore()
        return bool(
            (os.environ.get("META_PAGE_ACCESS_TOKEN") or store.get_credential(self.name, "META_PAGE_ACCESS_TOKEN"))
            and (os.environ.get("META_IG_BUSINESS_ID") or store.get_credential(self.name, "META_IG_BUSINESS_ID"))
        )
