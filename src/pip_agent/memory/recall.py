"""Pure-Python TF-IDF search over memory entries.

Supports English whitespace tokenization and basic CJK character splitting.
No external dependencies.
"""

from __future__ import annotations

import math
import re
import time

from pip_agent.types import Memory

_CJK_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
    r"\U00020000-\U0002a6df\U0002a700-\U0002ebef]"
)
_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def tokenize(text: str) -> list[str]:
    """Split text into tokens: English words + individual CJK characters."""
    tokens: list[str] = []
    for word in _WORD_RE.findall(text.lower()):
        tokens.append(word)
    for ch in _CJK_RE.findall(text):
        tokens.append(ch)
    return tokens


def _tf(tokens: list[str]) -> dict[str, float]:
    counts: dict[str, int] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1
    total = len(tokens) or 1
    return {t: c / total for t, c in counts.items()}


def _idf(doc_token_sets: list[set[str]], vocab: set[str]) -> dict[str, float]:
    n = len(doc_token_sets) or 1
    result: dict[str, float] = {}
    for term in vocab:
        df = sum(1 for s in doc_token_sets if term in s)
        result[term] = math.log((n + 1) / (df + 1)) + 1
    return result


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    shared = set(a) & set(b)
    if not shared:
        return 0.0
    dot = sum(a[k] * b[k] for k in shared)
    mag_a = math.sqrt(sum(v * v for v in a.values()))
    mag_b = math.sqrt(sum(v * v for v in b.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def temporal_decay(ts: float, half_life_days: float = 30.0) -> float:
    """Exponential decay factor based on age. Returns value in (0, 1]."""
    age_days = (time.time() - ts) / 86400
    if age_days <= 0:
        return 1.0
    return math.exp(-0.693 * age_days / half_life_days)


def search_memories(
    query: str,
    memories: list[Memory],
    *,
    top_k: int = 5,
    time_weight: float = 0.3,
    half_life_days: float = 30.0,
) -> list[Memory]:
    """Search memories by TF-IDF cosine similarity with optional time decay.

    Each memory dict must have at least ``text`` and ``last_reinforced`` (epoch).
    Returns top_k results sorted by score descending, each augmented with ``score``.
    """
    if not memories or not query.strip():
        return []

    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    doc_tokens_list = [tokenize(m.get("text", "")) for m in memories]
    doc_token_sets = [set(t) for t in doc_tokens_list]

    all_tokens = set(query_tokens)
    for s in doc_token_sets:
        all_tokens |= s
    idf = _idf(doc_token_sets, all_tokens)

    query_tf = _tf(query_tokens)
    query_vec = {t: query_tf[t] * idf.get(t, 1.0) for t in query_tf}

    scored: list[tuple[float, int]] = []
    for i, mem in enumerate(memories):
        doc_tf = _tf(doc_tokens_list[i])
        doc_vec = {t: doc_tf[t] * idf.get(t, 1.0) for t in doc_tf}
        sim = _cosine(query_vec, doc_vec)
        if sim <= 0:
            continue

        ts = mem.get("last_reinforced") or mem.get("first_seen") or time.time()
        decay = temporal_decay(ts, half_life_days)
        score = (1 - time_weight) * sim + time_weight * decay
        scored.append((score, i))

    scored.sort(key=lambda x: x[0], reverse=True)

    results: list[Memory] = []
    for score, idx in scored[:top_k]:
        entry = dict(memories[idx])
        entry["score"] = round(score, 4)
        results.append(entry)
    return results
