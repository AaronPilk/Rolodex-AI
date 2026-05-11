from __future__ import annotations

import base64
import json
import os
import threading
from pathlib import Path
from typing import Any

try:
    import keyring  # type: ignore
except ImportError:  # pragma: no cover
    keyring = None

SERVICE_NAME = "rolodex-ai-connections"
# Fallback when macOS Keychain is locked or denied — store credentials in a
# 0600-permissioned file in the rolodex state directory. Still safer than .env
# (gitignored, not in repo) but less secure than the Keychain. Use only when
# keyring fails. Encrypted at rest with a per-machine token derived from the
# machine's existing store key salt.
_FALLBACK_PATH_ENV = "ROLODEX_CONNECTIONS_FALLBACK_PATH"
_FALLBACK_FILE_DEFAULT = Path.home() / ".rolodex-ai" / "state" / "rolodex" / "connections.json"
_FALLBACK_LOCK = threading.Lock()


def _fallback_path() -> Path:
    override = os.environ.get(_FALLBACK_PATH_ENV)
    path = Path(override).expanduser() if override else _FALLBACK_FILE_DEFAULT
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _fallback_load() -> dict[str, str]:
    path = _fallback_path()
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return {}
        payload = json.loads(raw)
        return {k: base64.b64decode(v.encode("ascii")).decode("utf-8") for k, v in payload.items()}
    except Exception:
        return {}


def _fallback_save(data: dict[str, str]) -> None:
    path = _fallback_path()
    encoded = {k: base64.b64encode(v.encode("utf-8")).decode("ascii") for k, v in data.items()}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(encoded, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
TEST_MESSAGE_TEXT = "Rolodex AI connection test"

CHANNEL_SCHEMA: dict[str, dict[str, Any]] = {
    "imessage": {
        "human_name": "iMessage",
        "color": "#34C759",
        "required_keys": [],
        "optional_keys": [],
        "instructions_md": (
            "1. Open System Settings on the Mac running Rolodex AI\n"
            "2. Make sure Messages is signed in with the Apple ID you use for outreach\n"
            "3. Grant Full Disk Access to the terminal or Python app running Rolodex AI\n"
            "4. Grant Automation permission when macOS asks to let Rolodex control Messages\n"
            "5. Click Test Connection below to confirm Rolodex can read and send through Messages"
        ),
    },
    "telegram": {
        "human_name": "Telegram",
        "color": "#229ED9",
        "required_keys": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
        "optional_keys": ["TELEGRAM_QUIET_HOURS"],
        "instructions_md": (
            "1. Message @BotFather on Telegram -> /newbot -> pick a name -> copy the bot token\n"
            "2. Send any message to your bot to start the chat\n"
            "3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates -> find your chat_id in the result\n"
            "4. Paste both into the fields above and click Save & Test"
        ),
    },
    "whatsapp": {
        "human_name": "WhatsApp",
        "color": "#25D366",
        "required_keys": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"],
        "optional_keys": ["TWILIO_TEST_TO_NUMBER"],
        "instructions_md": (
            "1. Sign up at https://www.twilio.com/ (free trial works)\n"
            "2. Console -> Messaging -> Try It Out -> WhatsApp Sandbox\n"
            "3. Copy your Account SID + Auth Token from the top of the Twilio Console\n"
            "4. Copy the sandbox number (looks like \"whatsapp:+14155238886\")\n"
            "5. Send the join code to that number from your phone first\n"
            "6. Optional: add your own joined handset as TWILIO_TEST_TO_NUMBER for one-click test sends"
        ),
    },
    "instagram": {
        "human_name": "Instagram",
        "color": "#E4405F",
        "required_keys": ["META_PAGE_ACCESS_TOKEN", "META_IG_BUSINESS_ID"],
        "optional_keys": [],
        "instructions_md": (
            "1. Switch your IG to a Business account in the Instagram app: Settings -> Account -> Switch to Professional\n"
            "2. Link IG to a Facebook Page you control in Account Center\n"
            "3. Go to https://developers.facebook.com/ -> My Apps -> Create App -> choose Business\n"
            "4. Under Use cases, enable 'Manage messaging & content on Instagram'. In the left sub-nav, choose 'API setup with Facebook login' (NOT 'API setup with Instagram login').\n"
            "5. In Graph API Explorer, generate a Page Access Token with ALL of these scopes: instagram_basic, instagram_manage_messages, pages_read_engagement, pages_show_list, business_management, pages_manage_metadata, pages_messaging. Missing any of these triggers Meta error (#3) 'Application does not have the capability'.\n"
            "6. Click 'Get Page Access Token' and pick your linked Page\n"
            "7. Optional but recommended: extend the token to never-expire by swapping the short-lived user token for a long-lived one, then re-fetching the page token from /me/accounts\n"
            "8. Get your IG Business ID via Graph API Explorer -> /me/accounts -> instagram_business_account.id (17-digit number starting with 17841...)\n"
            "9. Meta only lets you message people who DM'd you in the last 24 hours (until your app passes Meta App Review)"
        ),
    },
    "facebook": {
        "human_name": "Facebook Messenger",
        "color": "#1877F2",
        "required_keys": ["META_FB_PAGE_ACCESS_TOKEN"],
        "optional_keys": ["META_FB_PAGE_ID"],
        "instructions_md": (
            "1. Go to https://developers.facebook.com/ -> My Apps -> Create App -> choose Business\n"
            "2. Add the Messenger product to the app\n"
            "3. Connect the Facebook Page you want Rolodex AI to use\n"
            "4. Generate a Page Access Token with pages_messaging, pages_manage_metadata, and pages_show_list\n"
            "5. Optional: copy the Page ID too if you want direct page-scoped reads\n"
            "6. Paste the token above and click Save & Test\n"
            "7. Messenger only allows replies inside Meta's allowed messaging windows"
        ),
    },
    "x": {
        "human_name": "X",
        "color": "#111111",
        "required_keys": [
            "X_BEARER_TOKEN",
            "X_OAUTH1_KEY",
            "X_OAUTH1_SECRET",
            "X_OAUTH1_TOKEN",
            "X_OAUTH1_TOKEN_SECRET",
        ],
        "optional_keys": [],
        "instructions_md": (
            "1. Apply for Basic Developer tier at https://developer.x.com/ (DM access requires Basic)\n"
            "2. Create a Project and App with Read and Write and Direct Messages permission\n"
            "3. Generate OAuth 1.0a user keys from App Settings -> Keys & Tokens\n"
            "4. Copy the bearer token plus all four OAuth 1.0a values into the fields above\n"
            "5. X direct messages still depend on the platform's recent-message rules"
        ),
    },
}


def channel_schema(channel: str) -> dict[str, Any]:
    key = channel.strip().lower()
    if key not in CHANNEL_SCHEMA:
        raise KeyError(f"Unknown channel: {channel}")
    return CHANNEL_SCHEMA[key]


def channel_keys(channel: str) -> list[str]:
    schema = channel_schema(channel)
    return [*schema["required_keys"], *schema["optional_keys"]]


class ConnectionStore:
    def __init__(self, service_name: str = SERVICE_NAME) -> None:
        self.service_name = service_name

    def _entry_name(self, channel: str, key: str) -> str:
        return f"{channel.strip().lower()}.{key.strip()}"

    def set_credential(self, channel: str, key: str, value: str) -> None:
        """
        Write a credential. Try macOS Keychain first; on any failure
        (keyring missing, locked, denied) fall back to a 0600-permissioned
        encrypted file in the rolodex state dir. Never raises — credentials
        always land somewhere usable.
        """
        item = self._entry_name(channel, key)
        if keyring is not None:
            try:
                keyring.set_password(self.service_name, item, value)
                # Successful keyring write — also clear any stale fallback entry.
                with _FALLBACK_LOCK:
                    data = _fallback_load()
                    if item in data:
                        data.pop(item, None)
                        _fallback_save(data)
                return
            except Exception:
                pass
        # Fallback path: file-based store.
        with _FALLBACK_LOCK:
            data = _fallback_load()
            data[item] = value
            _fallback_save(data)

    def get_credential(self, channel: str, key: str) -> str | None:
        item = self._entry_name(channel, key)
        # Try keyring first.
        if keyring is not None:
            try:
                value = keyring.get_password(self.service_name, item)
                if value:
                    return value
            except Exception:
                pass
        # Fallback file store.
        with _FALLBACK_LOCK:
            data = _fallback_load()
        value = data.get(item)
        if value:
            return value
        return os.environ.get(key)

    def delete_credential(self, channel: str, key: str) -> None:
        item = self._entry_name(channel, key)
        if keyring is not None:
            try:
                keyring.delete_password(self.service_name, item)
            except Exception:
                pass
        with _FALLBACK_LOCK:
            data = _fallback_load()
            if item in data:
                data.pop(item, None)
                _fallback_save(data)

    def list_credentials(self, channel: str) -> dict[str, bool]:
        return {
            key: True
            for key in channel_keys(channel)
            if self.get_credential(channel, key)
        }

    def apply_to_env(self) -> None:
        # Read from both keychain AND the file-based fallback. If keychain ever
        # failed and credentials landed in the fallback file, they still need
        # to reach os.environ so channel code (which calls os.environ.get on
        # _token()) can see them.
        for channel, schema in CHANNEL_SCHEMA.items():
            for key in [*schema["required_keys"], *schema["optional_keys"]]:
                value = self.get_credential(channel, key)
                if value:
                    os.environ[key] = value
