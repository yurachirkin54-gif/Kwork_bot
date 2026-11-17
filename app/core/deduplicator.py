"""Two-level deduplication: exact (source, external_id/url) and fuzzy cross-marketplace."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from rapidfuzz import fuzz

from app.models import UnifiedJob

_URL_RE = re.compile(r"https?://\S+")
_HTML_RE = re.compile(r"<[^>]+>")
_PUNCT_RE = re.compile(r"[^\w\s]|_", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")

STOPWORDS: set[str] = {
    # ru
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то", "все", "она", "так", "его", "но",
    "да", "ты", "к", "у", "же", "вы", "за", "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня",
    "еще", "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли", "если", "уже", "или",
    "ни", "быть", "был", "него", "до", "вас", "нибудь", "опять", "уж", "вам", "ведь", "там", "потом",
    "себя", "ничего", "ей", "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя",
    "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже", "себе", "под", "будет",
    "ж", "тогда", "кто", "этот", "того", "потому", "этого", "какой", "совсем", "ним", "здесь", "этом",
    "один", "почти", "мой", "тем", "чтобы", "нее", "сейчас", "были", "куда", "зачем", "всех", "никогда",
    "можно", "при", "наконец", "два", "об", "другой", "хоть", "после", "над", "больше", "тот", "через",
    "эти", "нас", "про", "всего", "них", "какая", "много", "разве", "три", "эту", "моя", "впрочем",
    "хорошо", "свою", "этой", "перед", "иногда", "лучше", "чуть", "том", "нельзя", "такой", "им",
    "более", "всегда", "конечно", "всю", "между",
    # en
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "so", "to", "of", "in", "on", "for", "with",
    "at", "by", "from", "as", "is", "are", "was", "were", "be", "been", "being", "this", "that", "these",
    "those", "it", "its", "we", "you", "they", "he", "she", "i", "our", "your", "their", "will", "would",
    "can", "could", "should", "may", "might", "must", "do", "does", "did", "have", "has", "had", "not",
    "no", "just", "very", "also", "into", "about", "need", "needed", "looking", "looking", "please",
    "job", "work", "project", "task", "required", "requirements", "looking for",
}


def normalize_text(text: str) -> str:
    """Lowercase, strip HTML/URLs/punctuation and collapse spaces."""

    if not text:
        return ""
    cleaned = _HTML_RE.sub(" ", text)
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = cleaned.lower().replace("ё", "е")
    cleaned = _PUNCT_RE.sub(" ", cleaned)
    return _SPACE_RE.sub(" ", cleaned).strip()


def tokenize(text: str) -> list[str]:
    """Normalized tokens without stopwords and single characters."""

    normalized = normalize_text(text)
    tokens = [tok for tok in normalized.split() if len(tok) > 1 and tok not in STOPWORDS]
    return tokens


def normalize_url(url: str) -> str:
    """Canonical URL: lowercase host, drop tracking params, fragment and trailing slash."""

    if not url:
        return ""
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        return url.strip().lower()
    query_parts = []
    for chunk in parsed.query.split("&"):
        if not chunk:
            continue
        key = chunk.split("=", 1)[0].lower()
        if key.startswith("utm_") or key in {"ref", "referrer", "from", "source", "fbclid", "gclid", "yclid"}:
            continue
        query_parts.append(chunk)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            "&".join(query_parts),
            "",
        )
    )


def _hash64(token: str) -> int:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=False)


def simhash(tokens: Iterable[str]) -> int:
    """64-bit SimHash of a token stream."""

    vector = [0] * 64
    tokens = list(tokens)
    if not tokens:
        return 0
    for token in tokens:
        hashed = _hash64(token)
        for bit in range(64):
            if hashed & (1 << bit):
                vector[bit] += 1
            else:
                vector[bit] -= 1
    result = 0
    for bit in range(64):
        if vector[bit] > 0:
            result |= 1 << bit
    return result


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def simhash_similarity(a: str, b: str) -> float:
    """1.0 = identical fingerprints."""

    left = simhash(tokenize(a))
    right = simhash(tokenize(b))
    if left == 0 or right == 0:
        return 0.0
    return 1.0 - hamming_distance(left, right) / 64.0


def jaccard_similarity(a: str, b: str, n: int = 3) -> float:
    """Jaccard similarity over character n-grams of normalized text."""

    left_text = normalize_text(a)
    right_text = normalize_text(b)
    if not left_text or not right_text:
        return 0.0
    if left_text == right_text:
        return 1.0
    left = {left_text[i : i + n] for i in range(max(1, len(left_text) - n + 1))}
    right = {right_text[i : i + n] for i in range(max(1, len(right_text) - n + 1))}
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def text_similarity(a: str, b: str) -> float:
    """Weighted blend of token-set ratio, Jaccard n-grams and SimHash."""

    left = normalize_text(a)
    right = normalize_text(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    token_ratio = fuzz.token_set_ratio(left, right) / 100.0
    jaccard = jaccard_similarity(left, right)
    sim = simhash_similarity(left, right)
    return round(0.5 * token_ratio + 0.3 * jaccard + 0.2 * sim, 4)


@dataclass(slots=True)
class DuplicateMatch:
    external_id: str
    source: str
    similarity: float
    db_id: int | None = None
    url: str = ""


class Deduplicator:
    """Fuzzy cross-marketplace matcher (level 2 of deduplication)."""

    def __init__(
        self,
        similarity_threshold: float = 0.88,
        min_tokens: int = 6,
        window_hours: int = 48,
    ) -> None:
        self.similarity_threshold = similarity_threshold
        self.min_tokens = min_tokens
        self.window_hours = window_hours

    def similarity(self, left: UnifiedJob, right: UnifiedJob) -> float:
        title_score = text_similarity(left.title, right.title)
        body_score = text_similarity(f"{left.title} {left.description}", f"{right.title} {right.description}")
        budget_bonus = 0.0
        if left.budget_value and right.budget_value:
            high = max(left.budget_value, right.budget_value)
            low = min(left.budget_value, right.budget_value)
            if high > 0 and low / high > 0.85:
                budget_bonus = 0.03
        return min(1.0, round(0.65 * title_score + 0.35 * body_score + budget_bonus, 4))

    def find_duplicate(self, job: UnifiedJob, candidates: Iterable[UnifiedJob]) -> DuplicateMatch | None:
        """Return the most similar job from another source, if it is a real duplicate."""

        best: DuplicateMatch | None = None
        tokens = tokenize(f"{job.title} {job.description}")
        short_text = len(tokens) < self.min_tokens
        for candidate in candidates:
            if candidate.source == job.source:
                continue
            if candidate.external_id == job.external_id and candidate.source == job.source:
                continue
            score = self.similarity(job, candidate)
            if short_text and text_similarity(job.title, candidate.title) < 0.9:
                continue
            if score >= self.similarity_threshold and (best is None or score > best.similarity):
                best = DuplicateMatch(
                    external_id=candidate.external_id,
                    source=candidate.source,
                    similarity=score,
                    url=candidate.url,
                )
        return best

    def is_duplicate(self, left: UnifiedJob, right: UnifiedJob) -> bool:
        return self.similarity(left, right) >= self.similarity_threshold
