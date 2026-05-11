from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


Direction = Literal["inbound", "outbound"]
ToneRating = Literal["sounds_like_me", "off", "edited"]
SendChannel = Literal["imessage", "sms"]
PersonSource = Literal[
    "imessage",
    "contacts_only",
    "imessage+contacts",
    "telegram_only",
    "whatsapp_only",
    "instagram_only",
    "facebook_only",
    "x_only",
]


class Channel(BaseModel):
    type: str
    handle: str
    chat_id: int | None = None
    message_count: int = 0
    last_message_at: str | None = None
    last_message_direction: Direction | None = None
    active: bool = True


class ToneProfile(BaseModel):
    capitalization_rate: float = 0.0
    punctuation_rate: float = 0.0
    emoji_per_100w: float = 0.0
    profanity_per_100w: float = 0.0
    avg_msg_words: float = 0.0
    stdev_msg_words: float = 0.0
    shibboleth_phrases: list[str] = Field(default_factory=list)
    sign_off_pattern: str | None = None
    topic_graph: list[str] = Field(default_factory=list)
    callbacks: list[str] = Field(default_factory=list)
    preferred_voice_label: str | None = None
    feedback_log: list["ToneFeedback"] = Field(default_factory=list)


class ToneFeedback(BaseModel):
    timestamp: str
    draft_sent: str
    rating: ToneRating
    edit_diff: str | None = None


class GroupThread(BaseModel):
    chat_id: int
    title: str
    handles: list[str] = Field(default_factory=list)
    last_message_at: str | None = None


class LifeEvent(BaseModel):
    kind: str
    label: str
    event_date: str | None = None
    days_window: int = 14
    source: str | None = None
    notes: str | None = None


class ScoringFields(BaseModel):
    warmth: float = 0.0
    responsiveness: float = 0.0
    freshness_decay: float = 0.0
    user_priority_boost: float = 0.0
    life_event_proximity: float = 0.0
    priority_score: float = 0.0
    natural_end_score: float = 0.0


class NaturalEndResult(BaseModel):
    score: float = 0.0
    reason: str = ""


class NaturalEndClassification(NaturalEndResult):
    hash: str | None = None
    classified_at: str | None = None


class CadenceState(BaseModel):
    tier: str = "T3"
    target_days: int | None = None
    days_since_last: int | None = None
    is_overdue: bool = False
    days_overdue: int = 0
    snooze_until: str | None = None
    last_digest_run_id: str | None = None
    last_digest_at: str | None = None
    last_sent_at: str | None = None
    last_skipped_run_id: str | None = None


class ContactMatch(BaseModel):
    query: str
    matched_name: str
    first_name: str | None = None
    last_name: str | None = None
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    company: str | None = None


class MessageSample(BaseModel):
    rowid: int | None = None
    at: str | None = None
    direction: Direction
    text: str = ""
    handle: str | None = None
    channel: str | None = None


class HistoryEntry(BaseModel):
    at: str
    kind: str
    message: str | None = None
    message_id: str | None = None
    run_id: str | None = None
    status: str | None = None
    channel_used: SendChannel | None = None


class ContactHistory(BaseModel):
    log: list[HistoryEntry] = Field(default_factory=list)
    total_contacts_initiated_by_me: int = 0


class ThreadSnapshot(BaseModel):
    chat_id: int
    title: str
    is_group: bool = False
    handle: str | None = None
    handles: list[str] = Field(default_factory=list)
    last_at: str | None = None
    message_count: int = 0
    last_message_direction: Direction | None = None
    messages: list[MessageSample] = Field(default_factory=list)


class DigestCandidate(BaseModel):
    run_id: str
    person_id: str
    display_name: str
    inferred_name: str | None = None
    relationship_class: str | None = None
    reason: str
    priority: float = 0.0
    due_days: int | None = None
    draft_preview: str | None = None
    status: str = "pending"


class DraftBundle(BaseModel):
    run_id: str
    person_id: str
    reason: str
    prompt: str
    top_draft: str
    alternates: list[str] = Field(default_factory=list)
    style_examples: list[str] = Field(default_factory=list)
    created_at: str | None = None


class SendReceipt(BaseModel):
    message_id: str
    channel_used: SendChannel
    provider_message_id: str | None = None
    delivered_at: str


class PersonRecord(BaseModel):
    person_id: str
    display_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    inferred_name: str | None = None
    company: str | None = None
    contact_organization: str | None = None
    contact_tags: list[str] = Field(default_factory=list)
    birthday: date | None = None
    source: PersonSource = "imessage"
    notes: str | None = None
    user_note: str | None = None
    user_override_class: str | None = None
    user_override_tier: str | None = None
    user_priority_boost: int | None = None
    user_marked_at: datetime | None = None
    context_summary: str | None = None
    topics: list[str] = Field(default_factory=list)
    handles: list[str] = Field(default_factory=list)
    instagram_username: str | None = None
    facebook_handle: str | None = None
    twitter_handle: str | None = None
    linkedin_url: str | None = None
    snapchat_username: str | None = None
    tiktok_handle: str | None = None
    how_we_met: str | None = None
    connected_channels: list[str] = Field(default_factory=list)
    channels: list[Channel] = Field(default_factory=list)
    tone_profile: ToneProfile = Field(default_factory=ToneProfile)
    group_threads: list[GroupThread] = Field(default_factory=list)
    life_events: list[LifeEvent] = Field(default_factory=list)
    scoring: ScoringFields = Field(default_factory=ScoringFields)
    cadence: CadenceState = Field(default_factory=CadenceState)
    recent_messages: list[MessageSample] = Field(default_factory=list)
    relationship_class: str | None = None
    tier: str = "T3"
    user_priority: float = 0.0
    do_not_contact: bool = False
    onboarding_reviewed: bool = False
    onboarding_reviewed_at: datetime | None = None
    relationship_classification_hash: str | None = None
    relationship_classified_at: datetime | None = None
    profile_enrichment_hash: str | None = None
    profile_enriched_at: datetime | None = None
    sensitivity_flags: list[str] = Field(default_factory=list)
    sensitivity_classification_hash: str | None = None
    sensitivity_classified_at: datetime | None = None
    natural_end_classification: NaturalEndClassification | None = None
    last_contacted: str | None = None
    last_message_at: str | None = None
    last_message_direction: Direction | None = None
    inbound_message_count: int = 0
    outbound_message_count: int = 0
    history: ContactHistory = Field(default_factory=ContactHistory)
    created_at: str | None = None
    updated_at: str | None = None


class SyncReport(BaseModel):
    scanned_threads: int = 0
    updated_people: int = 0
    created_people: int = 0
    skipped_group_threads: int = 0
    tagged_group_threads: int = 0
    total_people: int = 0
    store_path: str | None = None
    people: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RolodexStore(BaseModel):
    version: int = 1
    updated_at: str | None = None
    last_sync_at: str | None = None
    last_digest_at: str | None = None
    people: list[PersonRecord] = Field(default_factory=list)
    drafts: dict[str, DraftBundle] = Field(default_factory=dict)
    digests: dict[str, list[DigestCandidate]] = Field(default_factory=dict)
    daily_sends: dict[str, int] = Field(default_factory=dict)
    inbound_poll_state: dict[str, str] = Field(default_factory=dict)
    inbound_poll_offsets: dict[str, str] = Field(default_factory=dict)
    inbound_poll_status: dict[str, dict[str, str | int | None]] = Field(default_factory=dict)
    recent_errors: list[str] = Field(default_factory=list)


class RolodexHealth(BaseModel):
    person_count: int = 0
    last_sync_at: str | None = None
    last_digest_at: str | None = None
    sends_today: int = 0
    cap: int = 0
    recent_errors: list[str] = Field(default_factory=list)
    encrypted_store_present: bool = False
    keychain_accessible: bool = False
    imessage_db_accessible: bool = False
    twilio_configured: bool = False
