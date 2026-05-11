from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.connections import ConnectionStore


class _FakeKeyring:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.data[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.data.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self.data.pop((service, username), None)


def test_connection_store_round_trip_list_and_delete(monkeypatch) -> None:
    fake_keyring = _FakeKeyring()
    monkeypatch.setattr("agent.connections.keyring", fake_keyring)

    store = ConnectionStore(service_name="test-connections")
    store.set_credential("telegram", "TELEGRAM_BOT_TOKEN", "bot-token")
    store.set_credential("telegram", "TELEGRAM_CHAT_ID", "7400773187")

    assert store.get_credential("telegram", "TELEGRAM_BOT_TOKEN") == "bot-token"
    assert store.get_credential("telegram", "TELEGRAM_CHAT_ID") == "7400773187"
    assert store.list_credentials("telegram") == {
        "TELEGRAM_BOT_TOKEN": True,
        "TELEGRAM_CHAT_ID": True,
    }

    store.delete_credential("telegram", "TELEGRAM_CHAT_ID")

    assert store.get_credential("telegram", "TELEGRAM_CHAT_ID") is None
    assert store.list_credentials("telegram") == {"TELEGRAM_BOT_TOKEN": True}


def test_connection_store_falls_back_to_env_and_applies_keychain(monkeypatch) -> None:
    fake_keyring = _FakeKeyring()
    monkeypatch.setattr("agent.connections.keyring", fake_keyring)
    monkeypatch.setenv("X_BEARER_TOKEN", "env-bearer")
    monkeypatch.delenv("X_OAUTH1_KEY", raising=False)

    store = ConnectionStore(service_name="test-connections")
    store.set_credential("x", "X_OAUTH1_KEY", "keychain-oauth-key")

    assert store.get_credential("x", "X_BEARER_TOKEN") == "env-bearer"

    store.apply_to_env()

    assert store.get_credential("x", "X_OAUTH1_KEY") == "keychain-oauth-key"
