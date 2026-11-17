"""Stage A filters and Stage B deterministic scoring."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import ScoringConfig
from app.models import UserFilters, priority_for, utcnow
from app.processing.filters import HardFilter
from app.processing.scorer import JobScorer, human_budget, readiness_label


def filters(**kwargs) -> UserFilters:
    payload = {
        "min_budget_rub": 1500,
        "min_budget_usd": 20,
        "max_proposals": 5,
        "allow_unknown_proposals": True,
        "min_notify_score": 55,
        "allow_negotiable": True,
        "max_age_minutes": 1440,
        "exclude_keywords": ["казино", "за отзыв"],
    }
    payload.update(kwargs)
    return UserFilters.model_validate(payload)


def test_allows_good_job(make_job) -> None:
    decision = HardFilter(filters()).check(make_job())
    assert decision.passed is True


@pytest.mark.parametrize("status", ["closed", "in_progress"])
def test_rejects_inactive_status(make_job, status) -> None:
    decision = HardFilter(filters()).check(make_job(status=status))
    assert decision.passed is False
    assert "активен" in decision.reason


def test_rejects_stale_job(make_job) -> None:
    job = make_job(published_at=utcnow() - timedelta(hours=30))
    decision = HardFilter(filters()).check(job)
    assert decision.passed is False
    assert "устарел" in decision.reason


def test_rejects_too_many_proposals(make_job) -> None:
    decision = HardFilter(filters(max_proposals=3)).check(make_job(proposals_count=4))
    assert decision.passed is False
    assert "конкуренция" in decision.reason


def test_unknown_proposals_toggle(make_job) -> None:
    job = make_job(proposals_count=None)
    assert HardFilter(filters(allow_unknown_proposals=True)).check(job).passed is True
    assert HardFilter(filters(allow_unknown_proposals=False)).check(job).passed is False


def test_rejects_low_budget(make_job) -> None:
    job = make_job(budget_min=900, budget_max=900, budget_rub=900, budget_usd=10)
    decision = HardFilter(filters()).check(job)
    assert decision.passed is False
    assert "бюджет" in decision.reason


def test_usd_budget_uses_usd_threshold(make_job) -> None:
    job = make_job(
        currency="USD",
        budget_min=100,
        budget_max=100,
        budget_rub=9000,
        budget_usd=100,
        budget_type="fixed",
    )
    assert HardFilter(filters()).check(job).passed is True

    cheap = make_job(
        currency="USD",
        budget_min=10,
        budget_max=10,
        budget_rub=900,
        budget_usd=10,
        budget_type="fixed",
    )
    assert HardFilter(filters()).check(cheap).passed is False


def test_negotiable_toggle(make_job) -> None:
    job = make_job(
        budget_min=None,
        budget_max=None,
        budget_rub=None,
        budget_usd=None,
        budget_type="negotiable",
    )
    assert HardFilter(filters(allow_negotiable=True)).check(job).passed is True
    assert HardFilter(filters(allow_negotiable=False)).check(job).passed is False


def test_exclude_keyword(make_job) -> None:
    job = make_job(description="Нужен скрипт для казино, оплата высокая")
    decision = HardFilter(filters()).check(job)
    assert decision.passed is False
    assert "стоп-слово" in decision.reason


def test_exclude_regex(make_job) -> None:
    job = make_job(title="Срочно нужен бот за 100 рублей")
    decision = HardFilter(filters(exclude_regex=[r"за \d+\s*рублей"])).check(job)
    assert decision.passed is False
    assert "стоп-regex" in decision.reason


def test_include_keywords(make_job) -> None:
    job = make_job(title="Вёрстка лендинга", description="HTML CSS адаптив")
    rejected = HardFilter(filters(include_keywords=["python", "бот"])).check(job)
    assert rejected.passed is False
    accepted = HardFilter(filters(include_keywords=["лендинг"])).check(job)
    assert accepted.passed is True


def test_include_regex(make_job) -> None:
    job = make_job(description="Нужен телеграм бот для заявок")
    assert HardFilter(filters(include_regex=[r"телеграм\s*бот"])).check(job).passed is True
    other = make_job(description="Нужен дизайн визитки")
    assert HardFilter(filters(include_regex=[r"телеграм\s*бот"])).check(other).passed is False


def test_excluded_source(make_job) -> None:
    job = make_job(source="kwork")
    assert HardFilter(filters(exclude_sources=["kwork"])).check(job).passed is False


def test_scorer_prefers_fresh_low_competition(make_job) -> None:
    scorer = JobScorer(ScoringConfig(), filters())
    fresh = scorer.score(make_job(published_at=utcnow(), proposals_count=0))
    old = scorer.score(
        make_job(published_at=utcnow() - timedelta(hours=20), proposals_count=5)
    )
    assert fresh.total_score > old.total_score
    assert fresh.components.freshness > old.components.freshness
    assert fresh.components.competition > old.components.competition


def test_scorer_bounds(make_job) -> None:
    scorer = JobScorer(ScoringConfig(), filters())
    result = scorer.score(make_job())
    assert 0 <= result.total_score <= 100
    for value in result.components.model_dump().values():
        assert 0.0 <= value <= 1.0
    assert result.priority in {"top", "good", "ok"}
    assert result.reasons


def test_priority_thresholds() -> None:
    assert priority_for(90) == "top"
    assert priority_for(75) == "top"
    assert priority_for(60) == "good"
    assert priority_for(55) == "good"
    assert priority_for(54.9) == "ok"


def test_red_flags_reduce_text_score(make_job) -> None:
    scorer = JobScorer(ScoringConfig(), filters())
    clean = scorer.score(make_job(description="Понятное ТЗ. Требования: Python, API. Срок 3 дня."))
    toxic = scorer.score(
        make_job(
            description=(
                "Нужно сделать всё под ключ за 500р, оплата после сдачи, тестовое задание бесплатно"
            )
        )
    )
    assert toxic.components.text < clean.components.text


def test_verified_client_increases_client_score(make_job) -> None:
    scorer = JobScorer(ScoringConfig(), filters())
    verified = scorer.score(make_job(client_verified=True, client_rating=4.9, client_reviews=40))
    unknown = scorer.score(
        make_job(client_verified=False, client_rating=None, client_reviews=None, client_hire_rate=None)
    )
    assert verified.components.client > unknown.components.client


def test_human_budget_and_readiness(make_job) -> None:
    assert "5 000" in human_budget(make_job())
    negotiable = make_job(budget_min=None, budget_max=None, budget_rub=None, budget_type="negotiable")
    assert human_budget(negotiable) == "Договорная"
    assert readiness_label(make_job(description="Парсер на Python")) in {"простая", "средняя"}
