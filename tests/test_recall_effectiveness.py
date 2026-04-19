"""Phase 7: Recall algorithm effectiveness tests.

Validates TF-IDF + temporal decay recall across Chinese, English, mixed
queries, edge cases, and performance benchmarks.
"""

from __future__ import annotations

import time

from pip_agent.memory.recall import search_memories


def _mem(text: str, *, age_days: float = 0, count: int = 1) -> dict:
    """Helper to build a minimal memory dict."""
    ts = time.time() - age_days * 86400
    return {
        "text": text,
        "last_reinforced": ts,
        "first_seen": ts,
        "count": count,
        "category": "preference",
        "source": "auto",
    }


# ---------------------------------------------------------------------------
# A1: Pure Chinese query vs pure Chinese memories
# ---------------------------------------------------------------------------

class TestChineseRecall:
    def test_chinese_exact_match(self):
        memories = [
            _mem("用户偏好简洁的代码风格"),
            _mem("用户喜欢详细的文档注释"),
            _mem("用户经常使用Python编程"),
        ]
        results = search_memories("代码风格", memories)
        assert len(results) > 0
        assert "代码风格" in results[0]["text"]

    def test_chinese_partial_match(self):
        memories = [
            _mem("用户重视测试覆盖率"),
            _mem("用户偏好函数式编程"),
        ]
        results = search_memories("测试", memories)
        assert len(results) > 0
        assert "测试" in results[0]["text"]


# ---------------------------------------------------------------------------
# A2: Mixed Chinese-English queries
# ---------------------------------------------------------------------------

class TestMixedLanguageRecall:
    def test_english_query_chinese_memory(self):
        memories = [
            _mem("User prefers Python over JavaScript"),
            _mem("用户喜欢用Python编程"),
        ]
        results = search_memories("Python", memories, top_k=5)
        assert len(results) == 2

    def test_mixed_query(self):
        memories = [
            _mem("User uses React for frontend development"),
            _mem("用户在前端开发中使用React框架"),
        ]
        results = search_memories("React 前端", memories, top_k=5)
        assert len(results) > 0


# ---------------------------------------------------------------------------
# A3: Temporal decay — newer memories rank higher at equal similarity
# ---------------------------------------------------------------------------

class TestTemporalDecayRanking:
    def test_newer_memory_ranks_higher(self):
        memories = [
            _mem("User prefers dark mode in editors", age_days=60),
            _mem("User prefers dark mode in all applications", age_days=1),
        ]
        results = search_memories("dark mode preference", memories, time_weight=0.3)
        assert len(results) == 2
        assert results[0]["text"] == "User prefers dark mode in all applications"

    def test_much_older_memory_still_found(self):
        memories = [
            _mem("User values code readability above performance", age_days=90),
        ]
        results = search_memories("code readability", memories)
        assert len(results) == 1


# ---------------------------------------------------------------------------
# A4: time_weight extremes
# ---------------------------------------------------------------------------

class TestTimeWeightExtremes:
    def test_pure_semantic_ignores_time(self):
        old_exact = _mem("User prefers tabs over spaces", age_days=365)
        new_vague = _mem("User has some formatting preferences", age_days=0)
        results = search_memories(
            "tabs vs spaces", [old_exact, new_vague], time_weight=0.0
        )
        assert len(results) >= 1
        assert "tabs" in results[0]["text"]

    def test_pure_temporal_favors_newest(self):
        old = _mem("User prefers tabs over spaces", age_days=365)
        new = _mem("User likes consistent formatting", age_days=0)
        results = search_memories(
            "formatting style", [old, new], time_weight=1.0
        )
        assert len(results) >= 1
        assert results[0]["text"] == "User likes consistent formatting"


# ---------------------------------------------------------------------------
# A5: top_k correctly limits results
# ---------------------------------------------------------------------------

class TestTopK:
    def test_top_k_limits(self):
        memories = [_mem(f"Observation number {i}") for i in range(20)]
        results = search_memories("observation number", memories, top_k=3)
        assert len(results) == 3

    def test_top_k_larger_than_pool(self):
        memories = [_mem("Single memory")]
        results = search_memories("memory", memories, top_k=10)
        assert len(results) == 1


# ---------------------------------------------------------------------------
# A6: Field fallback (last_reinforced -> first_seen -> now)
# ---------------------------------------------------------------------------

class TestFieldFallback:
    def test_missing_last_reinforced_uses_first_seen(self):
        mem = {"text": "User prefers simplicity", "first_seen": time.time() - 86400}
        results = search_memories("simplicity", [mem])
        assert len(results) == 1
        assert results[0]["score"] > 0

    def test_missing_both_uses_now(self):
        mem = {"text": "User prefers simplicity"}
        results = search_memories("simplicity", [mem])
        assert len(results) == 1
        assert results[0]["score"] > 0


# ---------------------------------------------------------------------------
# A7: Performance with 500+ memories
# ---------------------------------------------------------------------------

class TestPerformance:
    def test_500_memories_under_500ms(self):
        memories = [_mem(f"User pattern observation about topic number {i}") for i in range(500)]
        start = time.time()
        results = search_memories("topic pattern observation", memories, top_k=5)
        elapsed = time.time() - start
        assert elapsed < 0.5, f"Search took {elapsed:.3f}s, expected < 0.5s"
        assert len(results) == 5

    def test_1000_memories_under_2s(self):
        memories = [_mem(f"Behavioral pattern {i} about decision making") for i in range(1000)]
        start = time.time()
        results = search_memories("decision making behavioral pattern", memories, top_k=10)
        elapsed = time.time() - start
        assert elapsed < 2.0, f"Search took {elapsed:.3f}s, expected < 2.0s"
        assert len(results) == 10


# ---------------------------------------------------------------------------
# New: Semantic fuzzy matching (known limitation)
# ---------------------------------------------------------------------------

class TestSemanticLimitations:
    def test_synonym_matching_is_weak(self):
        """TF-IDF cannot match synonyms; this documents the known limitation."""
        memories = [_mem("User prefers brevity in code")]
        results = search_memories("concise code style", memories)
        # "brevity" and "concise" are synonyms but share no tokens
        # TF-IDF will likely not match well — this is expected
        # The test documents the limitation rather than asserting failure
        if results:
            assert results[0]["score"] < 0.5


# ---------------------------------------------------------------------------
# New: Extremely short queries
# ---------------------------------------------------------------------------

class TestShortQueries:
    def test_single_word_query(self):
        memories = [
            _mem("User frequently uses Python for scripting"),
            _mem("User prefers TypeScript for web projects"),
        ]
        results = search_memories("Python", memories)
        assert len(results) >= 1
        assert "Python" in results[0]["text"]

    def test_single_cjk_character(self):
        memories = [
            _mem("用户喜欢简洁的代码"),
            _mem("用户重视性能优化"),
        ]
        results = search_memories("代", memories)
        assert len(results) >= 1

    def test_empty_query_returns_empty(self):
        memories = [_mem("User prefers dark mode")]
        results = search_memories("", memories)
        assert results == []

    def test_whitespace_query_returns_empty(self):
        memories = [_mem("User prefers dark mode")]
        results = search_memories("   ", memories)
        assert results == []
