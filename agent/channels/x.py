from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from agent.channels.base import Channel, ChannelHealth, ChannelMessage, NotConfigured, SendResult
from agent.connections import ConnectionStore

_TIMEOUT = 12


def _percent_encode(value: str) -> str:
    return urllib.parse.quote(str(value), safe="~-._")


def _oauth_header(method: str, url: str, extra_params: dict[str, str] | None = None, body: dict | None = None) -> str:
    key = os.environ["X_OAUTH1_KEY"]
    secret = os.environ["X_OAUTH1_SECRET"]
    token = os.environ["X_OAUTH1_TOKEN"]
    token_secret = os.environ["X_OAUTH1_TOKEN_SECRET"]
    oauth_params = {
        "oauth_consumer_key": key,
        "oauth_nonce": uuid.uuid4().hex,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    query_params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True))
    if extra_params:
        query_params.update(extra_params)
    if body:
        query_params.update({k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in body.items()})
    signature_params = {**oauth_params, **query_params}
    normalized = "&".join(
        f"{_percent_encode(k)}={_percent_encode(signature_params[k])}"
        for k in sorted(signature_params)
    )
    base_url = urllib.parse.urlunsplit(urllib.parse.urlsplit(url)._replace(query=""))
    signature_base = "&".join(_percent_encode(part) for part in [method.upper(), base_url, normalized])
    signing_key = f"{_percent_encode(secret)}&{_percent_encode(token_secret)}"
    digest = hmac.new(signing_key.encode("utf-8"), signature_base.encode("utf-8"), hashlib.sha1).digest()
    oauth_params["oauth_signature"] = base64.b64encode(digest).decode("ascii")
    return "OAuth " + ", ".join(
        f'{_percent_encode(k)}="{_percent_encode(v)}"' for k, v in sorted(oauth_params.items())
    )


def _request_json(method: str, url: str, *, body: dict | None = None) -> dict:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {os.environ['X_BEARER_TOKEN']}",
        "User-Agent": "rolodex-ai/0.1",
    }
    data = None
    if method.upper() != "GET":
        headers["Content-Type"] = "application/json"
        headers["Authorization"] = _oauth_header(method, url, body=body)
        data = json.dumps(body or {}).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{exc.code}: {raw}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc


class XChannel(Channel):
    name = "x"

    def send(self, handle: str, text: str) -> SendResult:
        if not self.is_configured():
            raise NotConfigured("X DM credentials are missing")
        url = f"https://api.x.com/2/dm_conversations/with/{handle}/messages"
        data = _request_json("POST", url, body={"text": text})
        return SendResult(
            ok=True,
            channel=self.name,
            handle=handle,
            message_id=str(data.get("data", {}).get("dm_event_id") or ""),
            raw=data,
        )

    def read_recent(self, handle: str, limit: int = 50) -> list[ChannelMessage]:
        if not self.is_configured():
            raise NotConfigured("X DM credentials are missing")
        url = f"https://api.x.com/2/dm_events?dm_conversation_id={urllib.parse.quote(handle)}&max_results={max(5, min(limit, 100))}"
        data = _request_json("GET", url)
        return [
            ChannelMessage(
                handle=handle,
                text=str(item.get("text") or ""),
                direction="inbound",
                sent_at=item.get("created_at"),
                message_id=str(item.get("id") or ""),
                channel=self.name,
                raw=item,
            )
            for item in data.get("data", [])[:limit]
        ]

    def health_check(self) -> ChannelHealth:
        if not self.is_configured():
            return ChannelHealth(configured=False, healthy=False, detail="X credentials missing")
        try:
            data = _request_json("GET", "https://api.x.com/2/users/me")
            return ChannelHealth(
                configured=True,
                healthy=True,
                detail=f"Connected as {data.get('data', {}).get('username', 'x-user')}",
            )
        except Exception as exc:
            return ChannelHealth(configured=True, healthy=False, detail=str(exc))

    def connect_instructions(self) -> str:
        return "Create an X developer app with DM scopes and set `X_BEARER_TOKEN`, `X_OAUTH1_KEY`, `X_OAUTH1_SECRET`, `X_OAUTH1_TOKEN`, and `X_OAUTH1_TOKEN_SECRET`."

    def is_configured(self) -> bool:
        store = ConnectionStore()
        return all(
            os.environ.get(key) or store.get_credential(self.name, key)
            for key in (
                "X_BEARER_TOKEN",
                "X_OAUTH1_KEY",
                "X_OAUTH1_SECRET",
                "X_OAUTH1_TOKEN",
                "X_OAUTH1_TOKEN_SECRET",
            )
        )
