from __future__ import annotations

import math
import re
from collections import Counter

from agent.models import MessageSample, ToneProfile

_PROFANITY = {"damn", "shit", "fuck", "hell", "wtf"}


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", text.lower())


def _word_count(text: str) -> int:
    return len(_tokenize(text))


def _emoji_count(text: str) -> int:
    return sum(1 for char in text if ord(char) > 10000)


def _ngrams(words: list[str], n: int) -> list[str]:
    return [" ".join(words[idx : idx + n]) for idx in range(0, len(words) - n + 1)]


def extract_style_examples(messages: list[MessageSample], limit: int = 5) -> list[str]:
    outbound = [msg.text.strip() for msg in messages if msg.direction == "outbound" and msg.text.strip()]
    if len(outbound) <= limit:
        return outbound
    ranked = sorted(
        enumerate(outbound),
        key=lambda item: (len(_tokenize(item[1])), item[0]),
    )
    picks = [ranked[0][1], ranked[len(ranked) // 2][1], ranked[-1][1]]
    recent = list(reversed(outbound[-limit:]))
    seen: list[str] = []
    for candidate in [*recent, *picks, *outbound]:
        if candidate not in seen:
            seen.append(candidate)
        if len(seen) >= limit:
            return seen[:limit]
    return seen[:limit]


def build_tone_profile(messages: list[MessageSample]) -> ToneProfile:
    outbound = [msg for msg in messages if msg.direction == "outbound" and msg.text.strip()]
    if not outbound:
        return ToneProfile(preferred_voice_label="warm_proper_full_sentences")
    starts_cap = sum(1 for msg in outbound if msg.text[:1].isupper())
    ends_punct = sum(1 for msg in outbound if msg.text.rstrip().endswith((".", "!", "?")))
    texts = [msg.text.strip() for msg in outbound]
    words = [_word_count(text) for text in texts]
    total_words = max(sum(words), 1)
    profanity_hits = sum(sum(1 for token in _tokenize(text) if token in _PROFANITY) for text in texts)
    emoji_hits = sum(_emoji_count(text) for text in texts)
    ngram_counts: Counter[str] = Counter()
    signoffs: Counter[str] = Counter()
    nounish: Counter[str] = Counter()
    callbacks: Counter[str] = Counter()
    for text in texts:
        tokens = _tokenize(text)
        for n in range(2, 5):
            ngram_counts.update(_ngrams(tokens, n))
        parts = re.findall(r"\b[A-Z][a-zA-Z]+\b", text)
        nounish.update(part.lower() for part in parts)
        callbacks.update(part for part in parts if len(part) > 3)
        tail = tokens[-3:]
        for size in (3, 2, 1):
            if len(tail) >= size:
                signoffs[" ".join(tail[-size:])] += 1
    avg_words = sum(words) / len(words)
    variance = sum((count - avg_words) ** 2 for count in words) / len(words)
    capitalization_rate = starts_cap / len(outbound)
    punctuation_rate = ends_punct / len(outbound)
    if capitalization_rate < 0.3 and punctuation_rate < 0.3:
        voice = "lowercase_casual_bro"
    elif avg_words <= 6 and punctuation_rate >= 0.6:
        voice = "biz_concise"
    elif any("love" in text.lower() or "family" in text.lower() for text in texts):
        voice = "family_warm"
    else:
        voice = "warm_proper_full_sentences"
    return ToneProfile(
        capitalization_rate=round(capitalization_rate, 3),
        punctuation_rate=round(punctuation_rate, 3),
        emoji_per_100w=round((emoji_hits / total_words) * 100, 3),
        profanity_per_100w=round((profanity_hits / total_words) * 100, 3),
        avg_msg_words=round(avg_words, 3),
        stdev_msg_words=round(math.sqrt(variance), 3),
        shibboleth_phrases=[
            phrase for phrase, count in ngram_counts.most_common() if count > 3
        ][:5],
        sign_off_pattern=signoffs.most_common(1)[0][0] if signoffs else None,
        topic_graph=[token for token, _ in nounish.most_common(8)],
        callbacks=[token for token, count in callbacks.most_common() if count > 1][:8],
        preferred_voice_label=voice,
    )
