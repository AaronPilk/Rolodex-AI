from __future__ import annotations

from agent.models import MessageSample
from agent.tonality import build_tone_profile, extract_style_examples


def test_build_tone_profile_extracts_basic_features() -> None:
    messages = [
        MessageSample(direction="outbound", text="hey man sounds good"),
        MessageSample(direction="outbound", text="i can do friday lol"),
        MessageSample(direction="outbound", text="family dinner Sunday?"),
        MessageSample(direction="inbound", text="works for me"),
        MessageSample(direction="outbound", text="see you there"),
    ]
    profile = build_tone_profile(messages)
    assert profile.avg_msg_words > 0
    assert profile.preferred_voice_label in {
        "lowercase_casual_bro",
        "family_warm",
        "biz_concise",
        "warm_proper_full_sentences",
    }


def test_extract_style_examples_returns_recent_varied_examples() -> None:
    messages = [
        MessageSample(direction="outbound", text=f"message number {idx}")
        for idx in range(8)
    ]
    examples = extract_style_examples(messages, limit=5)
    assert len(examples) == 5
    assert len(set(examples)) == 5
