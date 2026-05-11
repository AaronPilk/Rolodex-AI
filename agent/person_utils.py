from __future__ import annotations

from agent.models import PersonRecord


def looks_like_phone_label(value: str | None) -> bool:
    if not value:
        return False
    text = value.strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) < 7:
        return False
    stripped = text.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    return stripped.startswith(("+", "••••")) or stripped.isdigit()


def effective_relationship_class(person: PersonRecord) -> str | None:
    return person.user_override_class or person.relationship_class


_TIER_BY_CLASS = {
    "family":            "T1",
    "partner":           "T1",
    "close_friend":      "T2",
    "mentor":            "T2",
    "mentee":            "T2",
    "old_friend":        "T3",
    "casual_friend":     "T3",
    "professional":      "T3",
    "client":            "T3",
    "business":          "T3",
    "service_provider":  "T4",
    "met_at_event":      "T4",
    "met_briefly":       "T4",
    "group_chat_member": "T4",
}


def effective_tier(person: PersonRecord) -> str:
    """
    Tier semantics:
      T1 = Family / partner (parents, siblings, romantic)
      T2 = Strong ties     (close friends, mentors, core circle)
      T3 = Loose ties      (old/casual friends, professional, clients)
      T4 = Faint ties      (long-tail, met-once, group-chat-only)
      T5 = Unknown numbers (phone-only contacts with no name signal)

    User overrides always win. Otherwise relationship_class drives the
    tier. Unclassified phone-only contacts fall to T5.
    """
    if person.user_override_tier:
        return person.user_override_tier

    cls = (person.user_override_class or person.relationship_class or "").lower()
    by_class = _TIER_BY_CLASS.get(cls)
    if by_class:
        return by_class

    if cls == "spam_or_verification":
        return "T5"

    if person.tier:
        return person.tier

    # Unclassified — fall back to whether we even know who this is.
    has_name = bool(
        person.first_name
        or person.last_name
        or (person.display_name and not looks_like_phone_label(person.display_name))
    )
    if not has_name:
        return "T5"
    return "T4"


def display_name(person: PersonRecord) -> str:
    full = " ".join(part for part in [person.first_name, person.last_name] if part)
    if full:
        return full
    if person.display_name and not looks_like_phone_label(person.display_name):
        return person.display_name
    if person.inferred_name:
        return person.inferred_name
    if person.handles:
        return format_handle_label(person.handles[0])
    return person.person_id


def format_handle_label(handle: str) -> str:
    digits = "".join(ch for ch in handle if ch.isdigit())
    if len(digits) >= 4:
        return f"•••• {digits[-4:]}"
    return handle


def is_manually_overridden(person: PersonRecord) -> bool:
    return any(
        value not in (None, "", [], 0)
        for value in (
            person.user_note,
            person.user_override_class,
            person.user_priority_boost,
        )
    ) or person.do_not_contact
