"""Level 1/2 deduplication: normalization, SimHash, Jaccard and cross-source matching."""

from __future__ import annotations

from app.core.deduplicator import (
    Deduplicator,
    hamming_distance,
    jaccard_similarity,
    normalize_text,
    normalize_url,
    simhash,
    simhash_similarity,
    text_similarity,
    tokenize,
)


def test_normalize_url_strips_tracking_and_slash() -> None:
    raw = "https://Kwork.RU/projects/123/?utm_source=rss&ref=1#top"
    assert normalize_url(raw) == "https://kwork.ru/projects/123"


def test_normalize_text_removes_html_and_punctuation() -> None:
    text = "<p>Нужен ПАРСЕР, Python!</p> https://example.com"
    normalized = normalize_text(text)
    assert "<p>" not in normalized
    assert "парсер" in normalized
    assert "example.com" not in normalized


def test_tokenize_drops_stopwords() -> None:
    tokens = tokenize("Нужен парсер на Python и с выгрузкой в CSV")
    assert "и" not in tokens
    assert "на" not in tokens
    assert "парсер" in tokens


def test_simhash_identity_and_difference() -> None:
    a = simhash(tokenize("Нужен парсер каталога на Python с выгрузкой в CSV"))
    b = simhash(tokenize("Нужен парсер каталога на Python с выгрузкой в CSV"))
    c = simhash(tokenize("Дизайн логотипа и брендбука для кофейни"))
    assert a == b
    assert hamming_distance(a, b) == 0
    assert hamming_distance(a, c) > 10
    assert simhash_similarity("парсер python", "парсер python") == 1.0


def test_jaccard_similarity() -> None:
    assert jaccard_similarity("парсер каталога python", "парсер каталога python") == 1.0
    assert jaccard_similarity("парсер каталога python", "дизайн логотипа") < 0.3


def test_text_similarity_ordering() -> None:
    base = "Нужен парсер каталога на Python с выгрузкой в CSV"
    near = "Требуется парсер каталога на Python, выгрузка в CSV"
    far = "Разработать брендбук и логотип для кофейни"
    assert text_similarity(base, near) > text_similarity(base, far)


def test_find_cross_source_duplicate(make_job) -> None:
    dedupe = Deduplicator(similarity_threshold=0.85)
    job_a = make_job(
        source="kwork",
        external_id="111",
        title="Нужен парсер каталога Avito",
        description="Собрать все карточки товаров Avito и выгрузить в Excel. Python, requests.",
    )
    job_b = make_job(
        source="fl_ru",
        external_id="222",
        title="Нужен парсер каталога Avito",
        description="Собрать все карточки товаров Avito и выгрузить в Excel. Python, requests.",
    )
    match = dedupe.find_duplicate(job_b, [job_a])
    assert match is not None
    assert match.source == "kwork"
    assert match.external_id == "111"
    assert match.similarity >= 0.85


def test_same_source_is_never_cross_duplicate(make_job) -> None:
    dedupe = Deduplicator(similarity_threshold=0.85)
    job_a = make_job(source="kwork", external_id="1", title="Нужен парсер", description="Парсер каталога")
    job_b = make_job(source="kwork", external_id="2", title="Нужен парсер", description="Парсер каталога")
    assert dedupe.find_duplicate(job_b, [job_a]) is None


def test_different_jobs_not_duplicates(make_job) -> None:
    dedupe = Deduplicator(similarity_threshold=0.85)
    job_a = make_job(source="kwork", title="Парсер каталога Avito", description="Python, CSV, прокси")
    job_b = make_job(source="fl_ru", title="Логотип для кофейни", description="Вектор, брендбук, 3 варианта")
    assert dedupe.find_duplicate(job_b, [job_a]) is None
