from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from agent.channels.base import ChannelHealth, NotConfigured

# Meta Graph API can take 15-20s on the first /me/conversations call after a
# token change, especially during cold caches. 12s was too tight and produced
# spurious "read operation timed out" failures in the inbox UI.
_TIMEOUT = 30


def instructions_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s)]+", text)
    return match.group(0) if match else None


def is_meta_capability_error(exc: Exception) -> bool:
    text = str(exc)
    return "(#3)" in text and "Application does not have the capability to make this API call" in text


def request_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: dict | None = None) -> dict:
    data = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{exc.code}: {raw}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc
    if not raw:
        return {}
    return json.loads(raw)


class MetaChannelMixin:
    graph_version = "v19.0"
    oauth_scope = ""
    env_token_name = ""
    env_account_name = ""
    platform = ""

    def _token(self) -> str:
        token = os.environ.get(self.env_token_name)
        if not token:
            raise NotConfigured(f"{self.env_token_name} is not set")
        return token

    def _account_id(self) -> str:
        account_id = os.environ.get(self.env_account_name)
        if not account_id:
            raise NotConfigured(f"{self.env_account_name} is not set")
        return account_id

    def _self_graph_id(self) -> str:
        try:
            return self._account_id()
        except NotConfigured:
            data = request_json(self._graph_url("me", fields="id"))
            return str(data.get("id") or "")

    def _graph_url(self, path: str, **params: str) -> str:
        query = urllib.parse.urlencode({"access_token": self._token(), **params})
        return f"https://graph.facebook.com/{self.graph_version}/{path}?{query}"

    def health_check(self) -> ChannelHealth:
        if not self.is_configured():
            return ChannelHealth(
                configured=False,
                healthy=False,
                detail=f"{self.env_token_name} not configured",
                instructions_url=instructions_url(self.connect_instructions()),
            )
        try:
            data = request_json(self._graph_url("me", fields="id"))
            return ChannelHealth(
                configured=True,
                healthy=True,
                detail=f"Connected to Meta id {data.get('id', 'unknown')}",
                instructions_url=instructions_url(self.connect_instructions()),
            )
        except Exception as exc:
            return ChannelHealth(
                configured=True,
                healthy=False,
                detail=str(exc),
                instructions_url=instructions_url(self.connect_instructions()),
            )
