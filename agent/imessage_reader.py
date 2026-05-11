"""
imessage_reader.py — standalone iMessage chat.db reader.

Replaces PILK's `messages_search_mine` and `messages_read_thread` tools.

Reads ~/Library/Messages/chat.db directly via sqlite3. Requires Full Disk Access
permission for whatever Python process runs this (System Settings → Privacy & Security
→ Full Disk Access → add Terminal/iTerm/Python).

Apple stores message timestamps as Cocoa Core Data Reference time (seconds since
2001-01-01 UTC), nanosecond precision. We convert to ISO 8601.
"""

from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

CHAT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"
COCOA_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)


def _decode_attributed_body(data: bytes | None) -> str | None:
    """
    Extract message text from an iMessage `attributedBody` typedstream blob.

    Modern macOS / iOS iMessage stores message text in the `attributedBody`
    BLOB (a serialized NSAttributedString in Apple's "typedstream" format)
    rather than the plain `text` column. As of 2023+ this is the case for
    99%+ of messages. Without decoding, every modern message is dropped.

    Format (simplified):
        - Header: ``\\x04\\x0bstreamtyped...``
        - Class chain with markers like ``NSMutableAttributedString``,
          ``NSAttributedString``, ``NSObject``, ``NSString``/``NSMutableString``.
        - The actual UTF-8 text bytes appear after a ``+`` (0x2b) marker,
          prefixed by a length:
              * ``0x01..0x80``       -> 1-byte length
              * ``0x81 + 2-byte BE`` -> 16-bit length (typical for >127 chars)
              * ``0x82 + 4-byte BE`` -> 32-bit length (very long messages)

    This decoder is intentionally minimal — enough to recover plain text from
    the vast majority of messages including emoji, URLs, and unicode. It does
    not preserve attribute runs (bold, links) — Rolodex only needs raw text.
    """
    if not data or not isinstance(data, (bytes, bytearray)) or len(data) < 80:
        return None

    for marker in (b"NSMutableString", b"NSString"):
        idx = data.find(marker)
        if idx == -1:
            continue
        pos = idx + len(marker)
        end = min(pos + 64, len(data))
        for off in range(pos, end):
            if data[off] != 0x2B:
                continue
            if off + 2 >= len(data):
                continue
            length_byte = data[off + 1]
            if length_byte == 0x81:
                if off + 4 >= len(data):
                    continue
                length = (data[off + 2] << 8) | data[off + 3]
                text_start = off + 4
            elif length_byte == 0x82:
                if off + 6 >= len(data):
                    continue
                length = struct.unpack(">I", data[off + 2 : off + 6])[0]
                text_start = off + 6
            elif length_byte < 0x81:
                length = length_byte
                text_start = off + 2
            else:
                continue
            if length <= 0 or length > 200_000 or text_start + length > len(data):
                continue
            try:
                text = data[text_start : text_start + length].decode("utf-8")
            except UnicodeDecodeError:
                continue
            if text.startswith(("NS", "IS", "__kIM")):
                continue
            return text
    return None


@dataclass
class Message:
    """One iMessage message."""

    rowid: int
    chat_id: int
    handle: str  # phone or email of the OTHER party
    is_from_me: bool
    text: str
    sent_at: datetime
    is_group_chat: bool


@dataclass
class Thread:
    """A conversation summary."""

    chat_id: int
    handle: str  # primary other-party identifier
    handles: list[str]  # all participants for group chats
    is_group: bool
    message_count: int
    last_message_at: datetime
    last_message_from_me: bool


def _cocoa_to_dt(cocoa_ns: int) -> datetime:
    """Convert Cocoa Core Data nanosecond timestamp to UTC datetime."""
    seconds = cocoa_ns / 1_000_000_000
    return COCOA_EPOCH + timedelta(seconds=seconds)


def _connect(chat_db: Path = CHAT_DB_PATH) -> sqlite3.Connection:
    if not chat_db.exists():
        raise FileNotFoundError(
            f"chat.db not found at {chat_db}. "
            "Are you on macOS? Have you signed into iMessage?"
        )
    # Open read-only via URI to avoid touching the file
    uri = f"file:{chat_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_threads(*, limit: int | None = 200, chat_db: Path = CHAT_DB_PATH) -> list[Thread]:
    """
    Return the most-recently-active conversations, newest first.

    Excludes empty / orphan rows. Group chats have is_group=True with all participants
    listed in `handles`.
    """
    sql = """
        SELECT
            c.ROWID as chat_id,
            c.style as chat_style,
            COUNT(m.ROWID) as msg_count,
            MAX(m.date) as last_date,
            (SELECT m2.is_from_me FROM message m2
             JOIN chat_message_join cmj2 ON cmj2.message_id = m2.ROWID
             WHERE cmj2.chat_id = c.ROWID
             ORDER BY m2.date DESC LIMIT 1) as last_from_me
        FROM chat c
        JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
        JOIN message m ON m.ROWID = cmj.message_id
        WHERE m.text IS NOT NULL OR m.attributedBody IS NOT NULL
        GROUP BY c.ROWID
        ORDER BY last_date DESC
    """
    params: tuple[object, ...] = ()
    if limit not in (None, 0):
        sql += "\n        LIMIT ?"
        params = (int(limit),)
    threads: list[Thread] = []
    with _connect(chat_db) as conn:
        for row in conn.execute(sql, params):
            handles = _participants_for_chat(conn, row["chat_id"])
            primary = handles[0] if handles else "unknown"
            is_group = row["chat_style"] == 43 or len(handles) > 1
            threads.append(
                Thread(
                    chat_id=row["chat_id"],
                    handle=primary,
                    handles=handles,
                    is_group=is_group,
                    message_count=row["msg_count"],
                    last_message_at=_cocoa_to_dt(row["last_date"]),
                    last_message_from_me=bool(row["last_from_me"]),
                )
            )
    return threads


def _participants_for_chat(conn: sqlite3.Connection, chat_id: int) -> list[str]:
    sql = """
        SELECT h.id
        FROM handle h
        JOIN chat_handle_join chj ON chj.handle_id = h.ROWID
        WHERE chj.chat_id = ?
        ORDER BY h.ROWID
    """
    return [row["id"] for row in conn.execute(sql, (chat_id,))]


def read_thread(
    chat_id: int,
    *,
    limit: int | None = 300,
    chat_db: Path = CHAT_DB_PATH,
) -> list[Message]:
    """Return the last N messages of a thread, newest first."""
    sql = """
        SELECT
            m.ROWID as rowid,
            m.text as text,
            m.attributedBody as attributed_body,
            m.is_from_me as is_from_me,
            m.date as date,
            h.id as handle,
            c.style as chat_style
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN chat c ON c.ROWID = cmj.chat_id
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE c.ROWID = ?
          AND (m.text IS NOT NULL OR m.attributedBody IS NOT NULL)
          AND m.is_audio_message = 0
          AND m.item_type = 0
        ORDER BY m.date DESC
    """
    params: tuple[object, ...] = (chat_id,)
    if limit not in (None, 0):
        sql += "\n        LIMIT ?"
        params = (chat_id, int(limit))
    messages: list[Message] = []
    with _connect(chat_db) as conn:
        for row in conn.execute(sql, params):
            text = row["text"] or ""
            if not text.strip():
                # Modern iMessage stores text in the typedstream blob — decode it.
                text = _decode_attributed_body(row["attributed_body"]) or ""
            if not text.strip():
                continue
            messages.append(
                Message(
                    rowid=row["rowid"],
                    chat_id=chat_id,
                    handle=row["handle"] or "unknown",
                    is_from_me=bool(row["is_from_me"]),
                    text=text,
                    sent_at=_cocoa_to_dt(row["date"]),
                    is_group_chat=row["chat_style"] == 43,
                )
            )
    return messages


def search_messages(
    query: str,
    *,
    limit: int = 50,
    chat_db: Path = CHAT_DB_PATH,
) -> list[Message]:
    """Full-text search across all messages, newest first."""
    # We can't `LIKE` against the binary attributedBody blob, so we pull a
    # broader candidate set (anything containing the query OR anything with
    # a text-less attributedBody) and filter in Python after decoding.
    sql = """
        SELECT
            m.ROWID as rowid,
            m.text as text,
            m.attributedBody as attributed_body,
            m.is_from_me as is_from_me,
            m.date as date,
            h.id as handle,
            cmj.chat_id as chat_id,
            c.style as chat_style
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN chat c ON c.ROWID = cmj.chat_id
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE (m.text LIKE ? OR (m.text IS NULL AND m.attributedBody IS NOT NULL))
          AND m.is_audio_message = 0
          AND m.item_type = 0
        ORDER BY m.date DESC
        LIMIT ?
    """
    pattern = f"%{query}%"
    out: list[Message] = []
    needle = query.lower()
    # Cap the candidate scan so search stays bounded on very large dbs.
    candidate_limit = max(int(limit) * 200, 5000)
    with _connect(chat_db) as conn:
        for row in conn.execute(sql, (pattern, candidate_limit)):
            text = row["text"] or ""
            if not text.strip():
                text = _decode_attributed_body(row["attributed_body"]) or ""
            if not text.strip() or needle not in text.lower():
                continue
            out.append(
                Message(
                    rowid=row["rowid"],
                    chat_id=row["chat_id"],
                    handle=row["handle"] or "unknown",
                    is_from_me=bool(row["is_from_me"]),
                    text=text,
                    sent_at=_cocoa_to_dt(row["date"]),
                    is_group_chat=row["chat_style"] == 43,
                )
            )
            if len(out) >= int(limit):
                break
    return out


def health_check(chat_db: Path = CHAT_DB_PATH) -> tuple[bool, str]:
    """Return (ok, reason) — used by the CLI status command."""
    if not chat_db.exists():
        return False, f"chat.db not found at {chat_db}"
    try:
        with _connect(chat_db) as conn:
            row = conn.execute("SELECT COUNT(*) as n FROM message").fetchone()
            return True, f"chat.db readable, {row['n']:,} messages indexed"
    except sqlite3.OperationalError as e:
        if "authorization denied" in str(e).lower():
            return False, "Full Disk Access required for chat.db"
        return False, f"chat.db error: {e}"
