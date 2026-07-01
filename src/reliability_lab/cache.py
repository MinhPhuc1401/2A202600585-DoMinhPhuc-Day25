from __future__ import annotations

from collections import Counter
import hashlib
import math
import re
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """Simple in-memory cache skeleton.

    Add a better semantic similarity function and false-hit guardrails.
    Use the module-level _is_uncacheable() and _looks_like_false_hit() helpers in your
    get() and set() methods.  For production, replace with SharedRedisCache.
    """

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, Any]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response by semantic similarity.

        Implement cache lookup with guardrails:
        1. Return (None, 0.0) if _is_uncacheable(query) — privacy check
        2. Evict expired entries (compare time.time() - created_at vs ttl_seconds)
        3. Find best matching entry using self.similarity(query, entry.key)
        4. If best_score >= similarity_threshold:
           a. Check _looks_like_false_hit(query, best_key) — if true, log to
              self.false_hit_log and return (None, best_score)
           b. Otherwise return (best_value, best_score)
        5. Return (None, best_score) if no match above threshold
        """
        if _is_uncacheable(query):
            return None, 0.0

        now = time.time()
        self._entries = [entry for entry in self._entries if now - entry.created_at <= self.ttl_seconds]

        if not self._entries:
            return None, 0.0

        best_score = -1.0
        best_entry = None

        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_entry = entry

        if best_entry is not None and best_score >= self.similarity_threshold:
            if _looks_like_false_hit(query, best_entry.key):
                self.false_hit_log.append({
                    "reason": "date_or_number_mismatch",
                    "query": query,
                    "cached_key": best_entry.key
                })
                return None, best_score
            return best_entry.value, best_score

        return None, max(0.0, best_score)

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in cache.

        Implement with privacy guardrail:
        1. Return immediately if _is_uncacheable(query)
        2. Append a CacheEntry to self._entries
        """
        if _is_uncacheable(query):
            return
        self._entries.append(CacheEntry(
            key=query,
            value=value,
            created_at=time.time(),
            metadata=metadata or {}
        ))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Compute semantic similarity between two strings.

        Implement cosine similarity over character n-grams + word tokens.
        The naive token-overlap (Jaccard) approach loses too much information.

        Suggested approach:
        1. If a == b, return 1.0
        2. Tokenize both strings: split into words + character n-grams (n=3)
           e.g., "hello world" → ["hello", "world", "hel", "ell", "llo", "wor", "orl", "rld"]
        3. Build Counter (bag-of-words) vectors from these tokens
        4. Compute cosine similarity: dot(a,b) / (|a| * |b|)

        Hint: Use collections.Counter and math.sqrt.
        Import them at the top of the file.
        """
        if a == b:
            return 1.0
        a_clean = a.lower().strip()
        b_clean = b.lower().strip()
        if a_clean == b_clean:
            return 1.0

        words_a = re.findall(r"\w+", a_clean)
        words_b = re.findall(r"\w+", b_clean)

        tokens_a: list[str] = list(words_a)
        tokens_b: list[str] = list(words_b)

        for w in words_a:
            if len(w) >= 3:
                for i in range(len(w) - 2):
                    tokens_a.append(w[i : i + 3])

        for w in words_b:
            if len(w) >= 3:
                for i in range(len(w) - 2):
                    tokens_b.append(w[i : i + 3])

        if not tokens_a or not tokens_b:
            return 0.0

        vec_a = Counter(tokens_a)
        vec_b = Counter(tokens_b)

        dot_product = sum(vec_a[t] * vec_b[t] for t in vec_a if t in vec_b)
        mag_a = math.sqrt(sum(val * val for val in vec_a.values()))
        mag_b = math.sqrt(sum(val * val for val in vec_b.values()))

        if mag_a == 0.0 or mag_b == 0.0:
            return 0.0

        return min(1.0, dot_product / (mag_a * mag_b))


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    TODO(student): Implement the get() and set() methods using Redis commands
    so that cache state is shared across multiple gateway instances.

    Data model (suggested):
        Key    = "{prefix}{query_hash}"   (Redis String namespace)
        Value  = Redis Hash with fields:  "query", "response"
        TTL    = Redis EXPIRE (automatic cleanup — no manual eviction)

    For similarity lookup: SCAN all keys with self.prefix, HGET each entry's
    "query" field, compute similarity locally via ResponseCache.similarity().

    Provided helpers:
        _is_uncacheable(query)          — True if privacy-sensitive
        _looks_like_false_hit(q, key)   — True if 4-digit numbers differ
        self._query_hash(query)         — deterministic short hash for Redis key
        ResponseCache.similarity(a, b)  — reuse your improved similarity function
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis.

        Implement cache lookup. Suggested steps:
        1. Return (None, 0.0) if _is_uncacheable(query)
        2. Build exact-match key: f"{self.prefix}{self._query_hash(query)}"
        3. Try self._redis.hget(key, "response") — if found return (response, 1.0)
        4. Otherwise self._redis.scan_iter(f"{self.prefix}*") to iterate all cached keys
        5. For each key, HGET "query" field and compute
           ResponseCache.similarity(query, cached_query)
        6. Track best match that is >= self.similarity_threshold
        7. Before returning a match, check _looks_like_false_hit(); if true,
           append to self.false_hit_log and return (None, best_score)
        """
        if _is_uncacheable(query):
            return None, 0.0

        exact_key = f"{self.prefix}{self._query_hash(query)}"
        try:
            response = self._redis.hget(exact_key, "response")
            if response is not None:
                return response, 1.0
        except Exception:
            pass

        best_score = -1.0
        best_response = None
        best_query = None

        try:
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                entry = self._redis.hgetall(key)
                cached_query = entry.get("query")
                cached_response = entry.get("response")
                if cached_query and cached_response:
                    score = ResponseCache.similarity(query, cached_query)
                    if score > best_score:
                        best_score = score
                        best_response = cached_response
                        best_query = cached_query
        except Exception:
            pass

        if best_query is not None and best_score >= self.similarity_threshold:
            if _looks_like_false_hit(query, best_query):
                self.false_hit_log.append({
                    "reason": "date_or_number_mismatch",
                    "query": query,
                    "cached_key": best_query
                })
                return None, best_score
            return best_response, best_score

        return None, max(0.0, best_score)

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL.

        Implement cache storage. Suggested steps:
        1. Return immediately if _is_uncacheable(query)
        2. Build key: f"{self.prefix}{self._query_hash(query)}"
        3. self._redis.hset(key, mapping={"query": query, "response": value})
        4. self._redis.expire(key, self.ttl_seconds)
        """
        if _is_uncacheable(query):
            return
        key = f"{self.prefix}{self._query_hash(query)}"
        try:
            self._redis.hset(key, mapping={"query": query, "response": value})
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            pass

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
