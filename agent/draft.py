from __future__ import annotations

from datetime import UTC, datetime

from agent.llm_client import draft as llm_draft
from agent.models import DraftBundle, PersonRecord
from agent.person_utils import display_name
from agent.scoring import active_relationship_class
from agent.tonality import build_tone_profile, extract_style_examples


_CLASS_DRAFT_GUIDANCE = {
    "family": (
        "WARM and casual. Skip formality. Light, like dropping a quick check-in "
        "to a parent or sibling. e.g. 'hey mom, how was your week?' or "
        "'thinking about you — how's everything going?'. Don't be over-the-top sentimental."
    ),
    "partner": (
        "Intimate but not over-the-top. The kind of midday or end-of-day text "
        "between people who live in each other's pocket. e.g. 'hey babe how's "
        "your day' / 'miss u'."
    ),
    "close_friend": (
        "Bro/sis-level casual. Banter-friendly. e.g. 'yo it's been a sec — "
        "you alive?' or 'we need to catch up soon'. Match their energy, not formal."
    ),
    "old_friend": (
        "Warm reconnect from a friend who hasn't talked in a while. Acknowledge "
        "the gap without making it heavy. e.g. 'yo what's up bro, just checking "
        "in with some old friends, been a while' or 'hey stranger, how's life?'."
    ),
    "casual_friend": (
        "Friendly check-in, not too involved. e.g. 'hey how's it going?' or "
        "'long time no talk, hope all is well'."
    ),
    "met_at_event": (
        "Friendly reminder of where you met, then a light reach. e.g. 'hey it's "
        "Aaron from [event] — how have you been?'."
    ),
    "met_briefly": (
        "Re-introduce briefly + ask one specific question. Polite, low pressure."
    ),
    "business": (
        "Professional but not stiff. The kind of message you'd send a former "
        "collaborator. e.g. 'hey [name], been a while since we spoke — just "
        "wanted to check in and see how things are going. hope all is well "
        "on your end.'. Avoid pitches; this is a relationship-maintenance check-in."
    ),
    "professional": (
        "Polished and warm. Reference the work context if relevant. e.g. 'hey, "
        "hope your year is off to a good start — wanted to circle back and see "
        "what you're up to'."
    ),
    "client": (
        "Professional check-in tone. Ask about THEIR business/situation, not yours."
    ),
    "mentor": (
        "Respectful and specific. Acknowledge the gap, share one update, ask "
        "for their take on something. Don't waste their time."
    ),
    "mentee": (
        "Encouraging and warm. Ask how they're doing, offer to help."
    ),
    "service_provider": (
        "Transactional and polite. Only reach out if there's a real reason."
    ),
}


def build_draft_prompt(person: PersonRecord, reason: str) -> str:
    feedback_log = list(person.tone_profile.feedback_log)
    person.tone_profile = build_tone_profile(person.recent_messages)
    person.tone_profile.feedback_log = feedback_log
    examples = extract_style_examples(person.recent_messages, limit=5)
    convo = list(reversed(person.recent_messages[:10]))
    convo_lines = [
        f"- {msg.direction}: {msg.text}"
        for msg in convo
    ]
    rel_class = active_relationship_class(person) or "general"
    class_guidance = _CLASS_DRAFT_GUIDANCE.get(rel_class.lower(), (
        "Casual, friendly, low-pressure check-in. Match the operator's natural texting voice."
    ))
    return "\n".join(
        [
            "Write three short iMessage draft options for the operator to send.",
            "",
            f"Person: {display_name(person)}",
            f"Relationship class: {rel_class}",
            f"Context summary: {person.context_summary or 'unknown'}",
            f"Topics they discuss: {', '.join(person.topics) or 'none'}",
            f"Reason: {reason}",
            f"Reason for outreach: {reason}",
            "",
            f"VOICE FOR THIS RELATIONSHIP TYPE: {class_guidance}",
            "",
            "Hard rules:",
            "- Match the operator's actual texting voice (style examples below).",
            "- Don't address them by full first+last name; use first name only or none.",
            "- Don't open with 'Hey [Name]!' if the operator never does.",
            "- Don't use generic openers like 'I hope this finds you well'.",
            "- Mirror the punctuation/casing/length the operator actually uses.",
            "- If they're family or close, NEVER write business-formal language.",
            "- If they're business/professional, NEVER use slang like 'yo' or 'bro'.",
            "",
            "Tone fingerprint of the operator:",
            f"- preferred_voice_label: {person.tone_profile.preferred_voice_label}",
            f"- capitalization_rate: {person.tone_profile.capitalization_rate}",
            f"- punctuation_rate: {person.tone_profile.punctuation_rate}",
            f"- emoji_per_100w: {person.tone_profile.emoji_per_100w}",
            f"- avg_msg_words: {person.tone_profile.avg_msg_words}",
            f"- sign_off_pattern: {person.tone_profile.sign_off_pattern or 'none'}",
            f"- callbacks: {', '.join(person.tone_profile.callbacks) or 'none'}",
            "",
            "Verbatim style examples:",
            "Verbatim style examples (the operator's actual messages):",
            *[f"- {example}" for example in examples],
            "",
            "Most-recent thread snippet (oldest → newest):",
            *convo_lines,
            "",
            "Output EXACTLY three numbered draft lines. No preamble, no commentary.",
        ]
    )


def _stylometric_similarity(candidate: str, person: PersonRecord) -> float:
    profile = person.tone_profile
    words = candidate.split()
    starts_cap = 1.0 if candidate[:1].isupper() else 0.0
    ends_punct = 1.0 if candidate.rstrip().endswith((".", "!", "?")) else 0.0
    emoji_rate = sum(1 for char in candidate if ord(char) > 10000) / max(len(words), 1) * 100
    diffs = [
        abs(starts_cap - profile.capitalization_rate),
        abs(ends_punct - profile.punctuation_rate),
        abs(len(words) - profile.avg_msg_words) / max(profile.avg_msg_words or 1.0, 1.0),
        abs(emoji_rate - profile.emoji_per_100w) / 100,
    ]
    return round(1.0 - min(sum(diffs) / len(diffs), 1.0), 4)


def _feedback_anchor(person: PersonRecord) -> str:
    def _quoted(text: str) -> str:
        return f"'{text}'"

    samples = person.tone_profile.feedback_log[-3:]
    if not samples:
        return ""
    lines = ["Calibration anchor from recent operator feedback:"]
    for sample in samples:
        if sample.rating == "edited":
            lines.append(
                "Operator's edit: "
                f"original was {sample.edit_diff or '[missing edit diff]'}, "
                f"they sent {_quoted(sample.draft_sent)}. Match the change."
            )
            continue
        if sample.rating == "off":
            lines.append(
                "Operator marked similar draft as off-tone. "
                f"Avoid {_quoted(sample.draft_sent)} pattern."
            )
            continue
        lines.append(
            f"Operator confirmed this sounded right: {_quoted(sample.draft_sent)}. Keep that feel."
        )
    return "\n".join(lines)


async def _ask_llm(llm, prompt: str, system: str) -> str:
    if llm is None:
        return llm_draft(system=system, user=prompt).text
    if callable(llm):
        result = llm(prompt=prompt, system=system)
        return await result if hasattr(result, "__await__") else result
    if hasattr(llm, "ask"):
        return await llm.ask(prompt=prompt, system=system)
    raise TypeError("llm must be callable or expose .ask()")


async def generate_draft(person: PersonRecord, reason: str, llm) -> DraftBundle:
    if person.do_not_contact:
        raise ValueError(f"{person.person_id} is marked do_not_contact")
    if person.sensitivity_flags:
        raise ValueError(
            f"{person.person_id} has sensitive thread flags: {', '.join(person.sensitivity_flags)}"
        )
    prompt = build_draft_prompt(person, reason)
    system = (
        "You are drafting as the operator in their real texting voice. "
        "Match the constraints tightly. Keep it natural and concise."
    )
    anchor = _feedback_anchor(person)
    if anchor:
        system = f"{anchor}\n\n{system}"
    if person.user_note:
        system = (
            f"Operator's note about this person: {person.user_note}. "
            "Respect this context above all else.\n\n"
            f"{system}"
        )
    raw = await _ask_llm(llm, prompt, system)
    drafts = []
    for line in str(raw).splitlines():
        line = line.strip()
        if not line:
            continue
        line = line.lstrip("1234567890. )-")
        if line:
            drafts.append(line.strip())
    if not drafts:
        drafts = ["Hey, been meaning to reach out. Hope you're doing well."]
    while len(drafts) < 3:
        drafts.append(drafts[-1])
    scored = sorted(
        ((draft, _stylometric_similarity(draft, person)) for draft in drafts[:3]),
        key=lambda item: item[1],
        reverse=True,
    )
    return DraftBundle(
        run_id=f"rolodex-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
        person_id=person.person_id,
        reason=reason,
        prompt=prompt,
        top_draft=scored[0][0],
        alternates=[draft for draft, _score in scored[1:]],
        style_examples=extract_style_examples(person.recent_messages, limit=5),
        created_at=datetime.now(UTC).isoformat(),
    )
