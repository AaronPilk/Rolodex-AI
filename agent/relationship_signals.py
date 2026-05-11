from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from agent.models import MessageSample, PersonRecord
from agent.person_utils import looks_like_phone_label
from agent.store import get_person_by_handle


_ROLE_NAME_LABELS = {
    "dad",
    "daddy",
    "papa",
    "pop",
    "pops",
    "father",
    "mom",
    "momma",
    "mommy",
    "mama",
    "mother",
    "mother-in-law",
    "father-in-law",
    "mom home",
    "mom work",
    "dad home",
    "dad work",
    "sister",
    "sis",
    "brother",
    "bro",
    "son",
    "daughter",
    "aunt",
    "auntie",
    "uncle",
    "grandma",
    "grandpa",
    "granny",
    "nana",
    "nonna",
    "poppa",
    "gigi",
    "pawpaw",
    "memaw",
    "pa",
    "gran",
    "grandmother",
    "grandfather",
}
_FAMILY_PREFIXES = ("aunt", "uncle", "cousin", "grandma", "grandpa", "granny", "nana")
_SPOUSE_MARKERS = ("wife", "husband", "fiance", "fiancée", "hubby", "spouse")
_SERVICE_PROVIDER_KEYWORDS = (
    "landlord",
    "plumber",
    "realtor",
    "doctor",
    "dr.",
    "dentist",
    "mechanic",
    "insurance",
    "apartment",
    "property mgr",
)
_COMMON_LAST_NAMES = {
    "smith",
    "johnson",
    "williams",
    "brown",
    "jones",
    "garcia",
    "miller",
    "davis",
    "rodriguez",
    "martinez",
    "hernandez",
    "lopez",
    "gonzalez",
    "wilson",
    "anderson",
    "thomas",
    "taylor",
    "moore",
    "jackson",
    "martin",
    "lee",
    "perez",
    "thompson",
    "white",
    "harris",
    "sanchez",
    "clark",
    "ramirez",
    "lewis",
    "robinson",
    "walker",
    "young",
    "allen",
    "king",
    "wright",
    "scott",
    "torres",
    "nguyen",
    "hill",
    "flores",
    "green",
    "adams",
    "nelson",
    "baker",
    "hall",
}
_OUTBOUND_FAMILY_PATTERNS = (
    re.compile(r"\b(?:mom|mommy|mama|dad|daddy|father|mother)\b", re.IGNORECASE),
    re.compile(r"\blove you (?:mom|dad|mommy|daddy)\b", re.IGNORECASE),
)
_INBOUND_FAMILY_PATTERNS = (
    re.compile(r"\bson\b", re.IGNORECASE),
    re.compile(r"\bkiddo\b", re.IGNORECASE),
    re.compile(r"\bhoney son\b", re.IGNORECASE),
    re.compile(r"\blove you son\b", re.IGNORECASE),
    re.compile(r"\blove you\b", re.IGNORECASE),
)
_COMPANY_KEYWORDS = (
    "llc",
    "inc",
    "corp",
    "company",
    "co.",
    "group",
    "services",
    "management",
    "properties",
    "holdings",
    "studio",
    "agency",
    "partners",
)


@dataclass(frozen=True)
class RelationshipInference:
    label: str | None
    confidence: float
    reasoning: str
    rule_id: str
    should_skip_llm: bool


def _empty_inference() -> RelationshipInference:
    return RelationshipInference(
        label=None,
        confidence=0.0,
        reasoning="No deterministic relationship signal was strong enough.",
        rule_id="none",
        should_skip_llm=False,
    )


def _build_inference(label: str, confidence: float, reasoning: str, rule_id: str) -> RelationshipInference:
    return RelationshipInference(
        label=label,
        confidence=confidence,
        reasoning=reasoning,
        rule_id=rule_id,
        should_skip_llm=confidence >= 0.9,
    )


def _normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def _person_name_text(person: PersonRecord) -> str:
    full_name = " ".join(part for part in [person.first_name, person.last_name] if part).strip()
    return full_name or (person.display_name or "").strip()


def _count_matches(messages: Iterable[MessageSample], *, direction: str, patterns: tuple[re.Pattern[str], ...]) -> int:
    hits = 0
    for message in messages:
        if message.direction != direction:
            continue
        text = (message.text or "").strip()
        if text and any(pattern.search(text) for pattern in patterns):
            hits += 1
    return hits


def _shares_user_last_name(person: PersonRecord, user_last_name: str) -> bool:
    candidate = _normalize_text(person.last_name)
    return bool(candidate and candidate == _normalize_text(user_last_name))


def _known_family_labels(store, person: PersonRecord) -> int:
    family_count = 0
    person_handles = {_normalize_text(handle) for handle in person.handles}
    for group in person.group_threads:
        for handle in group.handles:
            if _normalize_text(handle) in person_handles:
                continue
            other = get_person_by_handle(store, handle)
            if other is None:
                continue
            label = (other.user_override_class or other.relationship_class or "").strip().lower()
            if label == "family":
                family_count += 1
    return family_count


def _has_high_confidence_family_signal(person: PersonRecord, store, user_last_name: str) -> bool:
    if (person.user_override_class or person.relationship_class or "").strip().lower() == "family":
        return True
    for rule in (
        _rule_role_name_first_name,
        _rule_family_prefix_display_name,
        _rule_spouse_in_law_marker,
        _rule_surname_match_with_user,
        _rule_outbound_pet_name,
    ):
        inference = rule(person, store, user_last_name)
        if inference is not None and inference.label == "family":
            return True
    return False


def _rule_role_name_first_name(
    person: PersonRecord,
    _store,
    _user_last_name: str,
) -> RelationshipInference | None:
    """Treat explicit kinship labels in first_name as ground-truth family metadata."""
    normalized = _normalize_text(person.first_name)
    if not normalized:
        return None
    first_token = normalized.split(" ", 1)[0]
    if normalized in _ROLE_NAME_LABELS or first_token in _ROLE_NAME_LABELS:
        return _build_inference(
            "family",
            0.99,
            f"Contact first_name is an explicit family role label: {person.first_name!r}.",
            "role_name_first_name",
        )
    return None


_OPERATOR_NOTE_PREFIX = r"(?:is|this\s+is|that\s+is|he'?s|she'?s)\s+"


_OPERATOR_NOTE_RULES: tuple[tuple[re.Pattern[str], str, float, str], ...] = (
    (
        re.compile(rf"\b(?:{_OPERATOR_NOTE_PREFIX})?my\s+(?:mom|mother|mama|momma|stepmom|stepmother)\b", re.IGNORECASE),
        "family",
        0.99,
        "Operator note explicitly identifies this person as the operator's mom.",
    ),
    (
        re.compile(rf"\b(?:{_OPERATOR_NOTE_PREFIX})?my\s+(?:dad|daddy|father|stepdad|stepfather|pops?)\b", re.IGNORECASE),
        "family",
        0.99,
        "Operator note explicitly identifies this person as the operator's dad.",
    ),
    (
        re.compile(
            rf"\b(?:{_OPERATOR_NOTE_PREFIX})?my\s+(?:sister|brother|sibling|son|daughter|aunt|uncle|cousin|niece|nephew|grandma|grandpa|grandparent)\b",
            re.IGNORECASE,
        ),
        "family",
        0.97,
        "Operator note explicitly identifies a family relationship.",
    ),
    (
        re.compile(rf"\b(?:{_OPERATOR_NOTE_PREFIX})?my\s+(?:wife|husband|partner|girlfriend|boyfriend|fiance)\b", re.IGNORECASE),
        "partner",
        0.99,
        "Operator note explicitly identifies a romantic partner.",
    ),
    (
        re.compile(
            rf"\b(?:{_OPERATOR_NOTE_PREFIX})?my\s+(?:business\s+partner|cofounder|co-?founder|co-?owner)\b",
            re.IGNORECASE,
        ),
        "business",
        0.95,
        "Operator note explicitly identifies a business partner.",
    ),
    (
        re.compile(
            rf"\b(?:{_OPERATOR_NOTE_PREFIX})?my\s+(?:boss|manager|coworker|colleague|teammate|employee|client|customer)\b",
            re.IGNORECASE,
        ),
        "professional",
        0.9,
        "Operator note explicitly identifies a professional relationship.",
    ),
    (
        re.compile(r"\b(?:old\s+)?college\s+(?:buddy|friend|roommate)\b", re.IGNORECASE),
        "old_friend",
        0.9,
        "Operator note identifies this person as an old college friend.",
    ),
    (
        re.compile(r"\b(?:old\s+)?high\s+school\s+(?:friend|buddy|classmate)\b", re.IGNORECASE),
        "old_friend",
        0.9,
        "Operator note identifies this person as an old high-school friend.",
    ),
    (
        re.compile(
            r"\b(?:realtor|landlord|plumber|doctor|dentist|accountant|lawyer|attorney|mechanic)\b",
            re.IGNORECASE,
        ),
        "service_provider",
        0.9,
        "Operator note identifies this person as a service provider.",
    ),
    (
        re.compile(rf"\b(?:{_OPERATOR_NOTE_PREFIX})?(?:an?\s+)?old\s+friend\b", re.IGNORECASE),
        "old_friend",
        0.95,
        "Operator note explicitly says this person is an old friend.",
    ),
)


def _rule_operator_note_role(
    person: PersonRecord,
    _store,
    _user_last_name: str,
) -> RelationshipInference | None:
    note = (person.user_note or "").strip()
    if not note:
        return None
    for pattern, label, confidence, reasoning in _OPERATOR_NOTE_RULES:
        if pattern.search(note):
            rule_id = "operator_note_business_partner" if "business partner" in reasoning.lower() else "operator_note_role"
            return _build_inference(label, confidence, reasoning, rule_id)
    return None


def _rule_family_prefix_display_name(
    person: PersonRecord,
    _store,
    _user_last_name: str,
) -> RelationshipInference | None:
    """Treat display names that start with kinship titles as family labels."""
    display = (person.display_name or "").strip()
    if display and re.match(rf"^(?:{'|'.join(re.escape(prefix) for prefix in _FAMILY_PREFIXES)})\b", display, re.IGNORECASE):
        return _build_inference(
            "family",
            0.95,
            f"Display name starts with a family title: {display!r}.",
            "family_prefix_display_name",
        )
    return None


def _rule_spouse_in_law_marker(
    person: PersonRecord,
    _store,
    _user_last_name: str,
) -> RelationshipInference | None:
    """Treat spouse keywords in the contact name as relationship signals.

    Critical disambiguation: a contact like "Benji Boyce Wife" is the wife of
    someone the user knows — NOT the user's partner. The pattern `[Name] Wife`
    or `[Name]'s Wife` is shorthand-by-marriage and means family-adjacent, not
    romantic partner. Only standalone "Wife" / "My Wife" / "Husband" / etc. or
    explicit possessive ("My fiancé") should trigger the romantic `partner`
    label.

    Heuristic: split the name into tokens. If the spouse marker is the LAST
    token and there are 1+ other capitalized name tokens before it, this is
    "[OtherPerson] Wife" pattern → label as `family` (in-law), confidence 0.7.
    Only when the marker stands alone or is preceded by "my"/"the" do we
    treat as romantic partner.
    """
    text = _normalize_text(_person_name_text(person))
    if not text:
        return None
    tokens = text.split()
    for marker in _SPOUSE_MARKERS:
        if not re.search(rf"\b{re.escape(marker)}\b", text):
            continue
        # Possessive partner: "my wife", "my husband"
        if re.search(rf"\bmy\s+{re.escape(marker)}\b", text):
            return _build_inference(
                "partner",
                0.95,
                f"Name explicitly says 'my {marker}'.",
                "spouse_in_law_marker_explicit",
            )
        # Standalone marker (single-token name like just "Wife"): partner.
        if tokens == [marker]:
            return _build_inference(
                "partner",
                0.9,
                f"Contact is named only {marker!r} — almost certainly the operator's partner.",
                "spouse_in_law_marker_standalone",
            )
        # "[Name] Wife" / "[Name]'s Wife" pattern → in-law / family-by-marriage,
        # NOT the operator's romantic partner.
        if marker in {"wife", "husband", "fiance", "fiancée", "hubby", "spouse"} and tokens[-1] == marker and len(tokens) >= 2:
            return _build_inference(
                "family",
                0.85,
                f"Pattern '[name] {marker}' indicates someone else's spouse — family-adjacent, not the operator's partner.",
                "spouse_in_law_marker_inlaw",
            )
        # In-law explicit (e.g. "mother-in-law", though those are in
        # _ROLE_NAME_LABELS already). Keep family fallback.
        return _build_inference(
            "family",
            0.7,
            f"Name contains a spouse-related marker: {marker!r}.",
            "spouse_in_law_marker",
        )
    return None


# Patterns that signal a romantic partner via message content. We require
# multiple independent signals to fire to avoid false positives — close
# friends use "love you" too, but rarely combined with daily cadence + pet
# names + heart emojis + good-morning/night patterns.
_PARTNER_PET_NAMES = re.compile(
    r"\b(babe|baby|honey|hun|sweetie|sweetheart|darling|my\s+love|love)\b",
    re.IGNORECASE,
)
_PARTNER_LOVE_PHRASES = re.compile(
    r"\b(i\s+love\s+you|love\s+you|miss\s+you|i\s+miss\s+you)\b",
    re.IGNORECASE,
)
_PARTNER_HEART_EMOJIS = re.compile(r"[❤❤️💕💖💗💘💝💞💓💌😘😍🥰😻]")
_PARTNER_GREETING_PHRASES = re.compile(
    r"\b(good\s*morning|goodnight|good\s+night|goodmorning)\b",
    re.IGNORECASE,
)


def _rule_romantic_partner_signal(
    person: PersonRecord,
    _store,
    _user_last_name: str,
) -> RelationshipInference | None:
    """High-bar message-content detection for the operator's romantic partner.

    Requires the union of multiple signals — close friends use any one of
    these; only romantic partners reliably hit several at once:
      - Outbound pet name 3+ ("babe", "honey", "love", etc.)
      - Outbound love phrase 2+ ("love you", "miss you")
      - Inbound heart emoji or pet name 3+
      - At least one "good morning" / "goodnight" pattern
    """
    if not person.recent_messages:
        return None

    out_pet = 0
    in_pet = 0
    out_love = 0
    out_hearts = 0
    in_hearts = 0
    in_pet_or_heart = 0
    greeting_hits = 0

    for msg in person.recent_messages:
        text = msg.text or ""
        if not text.strip():
            continue
        is_out = msg.direction == "outbound"
        if _PARTNER_PET_NAMES.search(text):
            if is_out:
                out_pet += 1
            else:
                in_pet += 1
                in_pet_or_heart += 1
        if is_out and _PARTNER_LOVE_PHRASES.search(text):
            out_love += 1
        if _PARTNER_HEART_EMOJIS.search(text):
            if is_out:
                out_hearts += 1
            else:
                in_hearts += 1
                in_pet_or_heart += 1
        if _PARTNER_GREETING_PHRASES.search(text):
            greeting_hits += 1

    if out_pet >= 3 and out_love >= 2 and in_pet_or_heart >= 3 and greeting_hits >= 1:
        return _build_inference(
            "partner",
            0.92,
            f"Romantic partner signal stack — out_pet={out_pet} out_love={out_love} in_pet_or_heart={in_pet_or_heart} greetings={greeting_hits}.",
            "romantic_partner_message_signal",
        )
    return None


def _rule_surname_match_with_user(
    person: PersonRecord,
    _store,
    user_last_name: str,
) -> RelationshipInference | None:
    """Use an exact surname match with the user as a likely family signal."""
    if _shares_user_last_name(person, user_last_name):
        return _build_inference(
            "family",
            0.85,
            f"Last name matches the user surname {user_last_name!r}.",
            "surname_match_with_user",
        )
    return None


def _rule_surname_cluster(
    person: PersonRecord,
    store,
    user_last_name: str,
) -> RelationshipInference | None:
    """Propagate family across uncommon surname clusters.

    A self-seeding rule: when 3+ contacts share an uncommon last name, that
    cluster is almost certainly a family unit. We require either (a) another
    cluster member already has a strong family signal (e.g. "Aunt Smith"),
    OR (b) the cluster has 3+ members each with stored phone/email contact
    metadata, which is itself strong evidence of an in-network family group.
    A bare set of three strangers who happen to share a surname won't
    typically all sit in someone's address book.
    """
    last_name = _normalize_text(person.last_name)
    if not last_name or last_name in _COMMON_LAST_NAMES:
        return None
    cluster = [other for other in store.people if _normalize_text(other.last_name) == last_name]
    if len(cluster) < 3:
        return None
    seeded = any(
        _has_high_confidence_family_signal(other, store, user_last_name)
        for other in cluster
        if other.person_id != person.person_id
    )
    if seeded:
        return _build_inference(
            "family",
            0.85,
            f"{len(cluster)} people share the uncommon surname {person.last_name!r}, and another member has a strong family signal.",
            "surname_cluster_family",
        )
    # Self-seeding case: every member has real contact metadata (i.e. they
    # exist in the operator's address book or have an active iMessage thread).
    in_address_book = sum(
        1 for other in cluster
        if (other.first_name or other.last_name or other.handles)
    )
    if in_address_book >= 3:
        return _build_inference(
            "family",
            0.8,
            f"{len(cluster)} contacts in the operator's address book share the uncommon surname {person.last_name!r} — almost certainly a family unit.",
            "surname_cluster_family_self_seeded",
        )
    return None


_RENTAL_CONTEXT_PATTERNS = (
    re.compile(r"\b(land\s*lord|landlady|rental\s+property|property\s+manag(?:er|ement)|"
               r"co-?landlord|co-?owner.+(?:property|rental)|tenant|leasing|"
               r"rent(?:al|ing)?\s+(?:agent|application|payment)|apartment\s+complex)\b",
               re.IGNORECASE),
)


def _rule_landlord_context(
    person: PersonRecord,
    _store,
    _user_last_name: str,
) -> RelationshipInference | None:
    """Catch service providers (landlords, property managers, etc.) that the
    LLM hallucinated into "family" because of co-ownership phrasing.

    Only fires when the contact has NO explicit family/role label in their
    contact metadata — i.e. just a phone number with rental/property words
    in the LLM-generated context summary.
    """
    # Don't override real contact metadata.
    if person.first_name or person.last_name:
        return None
    blob = " ".join([
        person.context_summary or "",
        " ".join(person.topics or []),
    ])
    if not blob.strip():
        return None
    if not any(p.search(blob) for p in _RENTAL_CONTEXT_PATTERNS):
        return None
    # Don't fire if the user explicitly claims this person is family.
    if (person.user_override_class or "").strip().lower() == "family":
        return None
    return _build_inference(
        "service_provider",
        0.92,
        "Phone-only contact with rental/landlord/property-management context — not family.",
        "landlord_context_override",
    )


def _rule_outbound_pet_name(
    person: PersonRecord,
    _store,
    _user_last_name: str,
) -> RelationshipInference | None:
    """Treat repeated outbound kinship language as evidence that the contact is family."""
    hits = _count_matches(person.recent_messages, direction="outbound", patterns=_OUTBOUND_FAMILY_PATTERNS)
    if hits >= 3:
        return _build_inference(
            "family",
            0.85,
            f"Outbound messages contain family address terms in {hits} messages.",
            "outbound_family_pet_name",
        )
    return None


def _rule_inbound_pet_name(
    person: PersonRecord,
    _store,
    _user_last_name: str,
) -> RelationshipInference | None:
    """Treat repeated inbound parent-like language as evidence that the sender is family."""
    hits = _count_matches(person.recent_messages, direction="inbound", patterns=_INBOUND_FAMILY_PATTERNS)
    if hits >= 3:
        return _build_inference(
            "family",
            0.7,
            f"Inbound messages contain parent-like language in {hits} messages.",
            "inbound_family_pet_name",
        )
    return None


def _rule_service_provider_keywords(
    person: PersonRecord,
    _store,
    _user_last_name: str,
) -> RelationshipInference | None:
    """Treat service-role keywords in the display name as explicit service-provider metadata."""
    display = _normalize_text(person.display_name)
    if not display:
        return None
    for keyword in _SERVICE_PROVIDER_KEYWORDS:
        if keyword in display:
            return _build_inference(
                "service_provider",
                0.9,
                f"Display name contains a service-provider keyword: {keyword!r}.",
                "service_provider_keyword",
            )
    return None


def _rule_business_keywords(
    person: PersonRecord,
    _store,
    user_last_name: str,
) -> RelationshipInference | None:
    """Treat clearly non-personal company metadata as a professional relationship signal."""
    company = _normalize_text(person.company or person.contact_organization)
    if not company or _shares_user_last_name(person, user_last_name):
        return None
    personal_name = _normalize_text(_person_name_text(person))
    if company == personal_name:
        return None
    if any(keyword in company for keyword in _COMPANY_KEYWORDS) or len(company.split()) >= 2:
        return _build_inference(
            "professional",
            0.7,
            f"Company metadata looks non-personal: {person.company or person.contact_organization!r}.",
            "business_keywords",
        )
    return None


def _rule_family_group_coparticipation(
    person: PersonRecord,
    store,
    _user_last_name: str,
) -> RelationshipInference | None:
    """Use repeated group-chat co-membership with known family as a low-confidence family hint."""
    family_count = _known_family_labels(store, person)
    if family_count >= 2:
        return _build_inference(
            "family",
            0.6,
            f"Contact appears in group threads with {family_count} known family members.",
            "family_group_coparticipation",
        )
    return None


_RULES = (
    _rule_operator_note_role,
    _rule_role_name_first_name,
    _rule_family_prefix_display_name,
    _rule_spouse_in_law_marker,
    _rule_romantic_partner_signal,
    _rule_surname_match_with_user,
    _rule_surname_cluster,
    _rule_outbound_pet_name,
    _rule_inbound_pet_name,
    _rule_service_provider_keywords,
    _rule_landlord_context,
    _rule_business_keywords,
    _rule_family_group_coparticipation,
)


def infer_relationship(person: PersonRecord, store, user_last_name: str) -> RelationshipInference:
    best = _empty_inference()
    for rule in _RULES:
        inference = rule(person, store, user_last_name)
        if inference is None:
            continue
        if inference.confidence > best.confidence:
            best = inference
        if inference.should_skip_llm:
            return inference
    if best.label is None and looks_like_phone_label(person.display_name or None) and _known_family_labels(store, person) >= 2:
        return _build_inference(
            "family",
            0.6,
            "Unknown phone-only contact appears repeatedly with family in group chats.",
            "family_group_coparticipation",
        )
    return best
