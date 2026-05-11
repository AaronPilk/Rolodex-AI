"""
holidays.py — Calendar-aware priority boosts for Rolodex AI.

When today is a relationship-relevant holiday (Mother's Day, Father's Day,
birthdays, etc.), the people that holiday is "for" get a large priority
boost so they surface to the top of the Due Now queue.

Pure-Python: no external `holidays` package, no internet — just date math.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

from agent.models import PersonRecord


# ─── Holiday definitions ────────────────────────────────────────────────────


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """Return the n-th occurrence of `weekday` (0=Mon..6=Sun) in month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of `weekday` in month."""
    if month == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, month + 1, 1)
    last_day = next_first - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def mothers_day(year: int) -> date:
    """US Mother's Day = 2nd Sunday of May."""
    return _nth_weekday_of_month(year, 5, weekday=6, n=2)


def fathers_day(year: int) -> date:
    """US Father's Day = 3rd Sunday of June."""
    return _nth_weekday_of_month(year, 6, weekday=6, n=3)


def thanksgiving(year: int) -> date:
    """US Thanksgiving = 4th Thursday of November."""
    return _nth_weekday_of_month(year, 11, weekday=3, n=4)


# ─── Holiday/Person matching ────────────────────────────────────────────────


@dataclass(frozen=True)
class HolidayBoost:
    """A boost to apply to a person's priority score for a holiday window."""

    name: str
    days_until: int
    boost: float
    reason: str


# A "window" of N days before the holiday during which we boost priority.
# Mother's Day boosts kick in 5 days before, peak on the day, decay 1 day
# after (so you can still send a "happy belated" message Monday morning).
_DEFAULT_WINDOW = (5, 1)  # (days_before, days_after)


_MOM_NAME_TOKENS = {
    "mom", "momma", "mommy", "mother", "mama", "ma", "mum",
    "mother-in-law", "stepmother", "stepmom",
}
_DAD_NAME_TOKENS = {
    "dad", "daddy", "father", "papa", "pop", "pops", "pa",
    "father-in-law", "stepfather", "stepdad",
}
_GRANDPARENT_TOKENS_F = {
    "grandma", "granny", "nana", "nonna", "memaw", "gigi", "gg",
    "grandmother",
}
_GRANDPARENT_TOKENS_M = {
    "grandpa", "gramps", "papa", "pawpaw", "poppa", "grandfather",
}


def _person_role_tokens(person: PersonRecord) -> set[str]:
    """Return the role-label tokens present in a person's contact name."""
    tokens: set[str] = set()
    for source in (person.first_name, person.display_name):
        if not source:
            continue
        for piece in source.lower().replace("-", " ").split():
            tokens.add(piece)
    return tokens


def _has_outbound_pet_name(person: PersonRecord, tokens: set[str], threshold: int = 2) -> bool:
    """True if the user has sent ≥`threshold` outbound messages to this
    person containing one of the given pet-name tokens (e.g. "mom"/"dad")."""
    hits = 0
    for message in person.recent_messages:
        if message.direction != "outbound":
            continue
        text = (message.text or "").lower()
        if not text:
            continue
        # Tokenize loosely on punctuation + whitespace.
        for word in text.replace("'", " ").replace(",", " ").split():
            if word.strip(".!?") in tokens:
                hits += 1
                if hits >= threshold:
                    return True
                break
    return False


def _user_note_marks_role(person: PersonRecord, tokens: set[str]) -> bool:
    """User explicitly wrote 'this is my mom' / 'my dad' / etc. in their note."""
    note = (person.user_note or "").lower()
    if not note:
        return False
    for token in tokens:
        # Be a little strict: require "my <token>" or "is my <token>" or
        # the bare word "<token>" with word boundaries — otherwise notes
        # like "shopping for mom's birthday" would false-trigger.
        for phrase in (f"my {token}", f"is my {token}", f"({token})"):
            if phrase in note:
                return True
    return False


def is_mom(person: PersonRecord) -> bool:
    """True if this person is plausibly the user's mother / mom-figure."""
    role_tokens = _person_role_tokens(person)
    if role_tokens & _MOM_NAME_TOKENS:
        return True
    if _user_note_marks_role(person, _MOM_NAME_TOKENS):
        return True
    cls = (person.user_override_class or person.relationship_class or "").lower()
    if cls != "family":
        return False
    return _has_outbound_pet_name(person, _MOM_NAME_TOKENS, threshold=3)


def is_dad(person: PersonRecord) -> bool:
    """True if this person is plausibly the user's father / dad-figure."""
    role_tokens = _person_role_tokens(person)
    if role_tokens & _DAD_NAME_TOKENS:
        return True
    if _user_note_marks_role(person, _DAD_NAME_TOKENS):
        return True
    cls = (person.user_override_class or person.relationship_class or "").lower()
    if cls != "family":
        return False
    return _has_outbound_pet_name(person, _DAD_NAME_TOKENS, threshold=3)


def is_grandparent(person: PersonRecord) -> bool:
    role_tokens = _person_role_tokens(person)
    return bool(role_tokens & (_GRANDPARENT_TOKENS_F | _GRANDPARENT_TOKENS_M))


def _birthday_match(person: PersonRecord, today: date) -> bool:
    if not person.birthday:
        return False
    try:
        bday = person.birthday
        if isinstance(bday, str):
            # Accept "MM-DD" or "YYYY-MM-DD" or ISO datetime.
            parts = bday[:10].split("-")
            if len(parts) == 3:
                month, day = int(parts[1]), int(parts[2])
            elif len(parts) == 2:
                month, day = int(parts[0]), int(parts[1])
            else:
                return False
        else:
            month, day = bday.month, bday.day
    except (TypeError, ValueError):
        return False
    return today.month == month and today.day == day


# ─── Public API ──────────────────────────────────────────────────────────────


def compute_holiday_boosts(person: PersonRecord, today: date) -> list[HolidayBoost]:
    """
    Return all active holiday boosts for `person` on `today`.

    The boost amount is a 0–100 number that gets added directly to the priority
    score (which is itself a 0–100 scale). It decays linearly with distance
    from the actual holiday — full boost on the day, half boost 2 days out,
    quarter boost 4 days out.
    """
    boosts: list[HolidayBoost] = []
    year = today.year

    # Mother's Day
    md = mothers_day(year)
    md_delta = (md - today).days
    if -_DEFAULT_WINDOW[1] <= md_delta <= _DEFAULT_WINDOW[0]:
        if is_mom(person):
            magnitude = _decay(abs(md_delta), peak=70.0)
            boosts.append(
                HolidayBoost(
                    name="Mother's Day",
                    days_until=md_delta,
                    boost=magnitude,
                    reason=f"Mother's Day is in {md_delta} day(s)" if md_delta > 0 else "Mother's Day is today" if md_delta == 0 else "Mother's Day was yesterday",
                )
            )

    # Father's Day
    fd = fathers_day(year)
    fd_delta = (fd - today).days
    if -_DEFAULT_WINDOW[1] <= fd_delta <= _DEFAULT_WINDOW[0]:
        if is_dad(person):
            magnitude = _decay(abs(fd_delta), peak=70.0)
            boosts.append(
                HolidayBoost(
                    name="Father's Day",
                    days_until=fd_delta,
                    boost=magnitude,
                    reason=f"Father's Day is in {fd_delta} day(s)" if fd_delta > 0 else "Father's Day is today" if fd_delta == 0 else "Father's Day was yesterday",
                )
            )

    # Birthday
    if _birthday_match(person, today):
        boosts.append(
            HolidayBoost(
                name="Birthday",
                days_until=0,
                boost=80.0,
                reason="It's their birthday today",
            )
        )

    # Christmas / New Year — broad family + close friend bump
    if today.month == 12 and 22 <= today.day <= 26:
        cls = (person.user_override_class or person.relationship_class or "").lower()
        if cls in {"family", "close_friend", "partner"}:
            boosts.append(
                HolidayBoost(
                    name="Christmas",
                    days_until=(date(year, 12, 25) - today).days,
                    boost=20.0,
                    reason="Christmas window — close people are top of mind",
                )
            )

    # Thanksgiving (US) — family bump in the 5 days before
    tg = thanksgiving(year)
    tg_delta = (tg - today).days
    if -1 <= tg_delta <= 4:
        cls = (person.user_override_class or person.relationship_class or "").lower()
        if cls == "family":
            boosts.append(
                HolidayBoost(
                    name="Thanksgiving",
                    days_until=tg_delta,
                    boost=_decay(abs(tg_delta), peak=30.0),
                    reason=f"Thanksgiving in {tg_delta} day(s)" if tg_delta >= 0 else "Day after Thanksgiving",
                )
            )

    return boosts


def _decay(distance_days: int, *, peak: float) -> float:
    """Linear decay from `peak` on the day to ~0 at the window edge."""
    if distance_days <= 0:
        return peak
    if distance_days == 1:
        return peak * 0.85
    if distance_days == 2:
        return peak * 0.65
    if distance_days == 3:
        return peak * 0.45
    if distance_days == 4:
        return peak * 0.30
    return peak * 0.15


def total_holiday_boost(person: PersonRecord, today: date) -> tuple[float, list[HolidayBoost]]:
    """Sum of all active holiday boosts. Returns (total, breakdown)."""
    boosts = compute_holiday_boosts(person, today)
    return sum(b.boost for b in boosts), boosts
