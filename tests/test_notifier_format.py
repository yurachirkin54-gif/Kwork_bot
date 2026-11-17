"""Telegram formatting: HTML escaping, budget lines, length limits and digest."""

from __future__ import annotations

from app.models import ScoringResult, ScoreComponents
from app.storage.orm import Job
from app.telegram.notifier import (
    format_budget,
    format_digest,
    format_job_card,
    scoring_from_row,
    source_label,
)


def score(total: float = 82.0, llm=None) -> ScoringResult:  # noqa: ANN001
    return ScoringResult(
        base_score=total,
        total_score=total,
        components=ScoreComponents(freshness=1.0, competition=1.0, budget=0.8, client=0.7, text=0.9),
        reasons=["свежий заказ"],
        priority="top" if total >= 75 else "good" if total >= 55 else "ok",
        llm=llm,
    )


def test_card_contains_key_sections(make_job) -> None:
    job = make_job()
    text = format_job_card(job, score())
    assert "🔥 ТОП" in text
    assert "82/100" in text
    assert "Kwork" in source_label(job.source) or source_label(job.source) in text
    assert "5 000 ₽" in text
    assert "~$55" in text
    assert "Откликов: <b>0</b>" in text
    assert "https://example.com/jobs/1" in text
    assert "82" in text


def test_card_escapes_html(make_job) -> None:
    job = make_job(title="<script>alert('xss')</script>", description="Условия: <b>важно</b> & срочно")
    text = format_job_card(job, score())
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&lt;b&gt;важно&lt;/b&gt;" in text or "&amp;" in text


def test_card_limits_length(make_job) -> None:
    job = make_job(description="Очень длинное описание. " * 2000)
    text = format_job_card(job, score())
    assert len(text) <= 4096
    assert text


def test_card_without_http_url_is_not_link(make_job) -> None:
    job = make_job(url="ftp://example.com/file")
    text = format_job_card(job, score())
    assert "<a href" not in text
    assert "<b>" in text


def test_card_stale_marker(make_job) -> None:
    text = format_job_card(make_job(), score(), stale_reason="заказ закрыт")
    assert "Неактуально" in text
    assert "заказ закрыт" in text


def test_card_llm_summary_and_flags(make_job) -> None:
    from app.models import LLMAnalysis

    analysis = LLMAnalysis(
        easiness_score=9,
        price_fairness_score=8,
        clarity_score=7,
        scam_or_toxic_risk="medium",
        estimated_time="1-2 часа",
        summary_ru="Нужен простой парсер на Python.",
        red_flags=["нет ТЗ"],
        draft_proposal="Здравствуйте!",
    )
    text = format_job_card(make_job(description="сырое описание"), score(llm=analysis))
    assert "Нужен простой парсер" in text
    assert "Лёгкость 9/10" in text
    assert "нет ТЗ" in text


def test_budget_variants(make_job) -> None:
    assert format_budget(make_job()).startswith("5 000 ₽")
    usd = make_job(currency="USD", budget_min=25, budget_max=25, budget_usd=25, budget_rub=2250)
    formatted = format_budget(usd)
    assert "$25" in formatted
    assert "₽" in formatted
    negotiable = make_job(budget_min=None, budget_max=None, budget_type="negotiable")
    assert format_budget(negotiable) == "Договорная"
    hourly = make_job(budget_type="hourly", budget_min=1000, budget_max=2000)
    assert "/ час" in format_budget(hourly)


def test_unknown_proposals_displayed(make_job) -> None:
    text = format_job_card(make_job(proposals_count=None), score())
    assert "н/д" in text


def test_digest_formatting(make_job) -> None:
    rows = [
        Job(
            source="kwork",
            external_id="1",
            url="https://kwork.ru/1",
            title="Парсер на Python",
            score=80.0,
            budget_rub=5000,
            proposals_count=0,
            currency="RUB",
        ),
        Job(
            source="fl_ru",
            external_id="2",
            url="https://fl.ru/2",
            title="Бот для заявок",
            score=70.0,
            budget_rub=None,
            budget_min=3000,
            proposals_count=None,
            currency="RUB",
        ),
    ]
    text = format_digest(rows)
    assert "дайджест" in text.lower()
    assert "Парсер на Python" in text
    assert "н/д" in text
    assert len(text) <= 4096


def test_keyboards_build(make_job) -> None:
    from app.models import SourceStatus, UserFilters
    from app.telegram.keyboards import (
        job_keyboard,
        menu_keyboard,
        sources_keyboard,
        filters_keyboard,
    )

    markup = job_keyboard(1, "https://kwork.ru/1")
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]
    assert "job:proposal:1" in callbacks
    assert "job:refresh:1" in callbacks
    assert any(button.url for row in markup.inline_keyboard for button in row)

    assert menu_keyboard(paused=False).inline_keyboard
    assert menu_keyboard(paused=True).inline_keyboard
    assert filters_keyboard(UserFilters()).inline_keyboard
    statuses = [
        SourceStatus(name="kwork", kind="kwork", enabled=True, state="ok"),
        SourceStatus(name="fl_ru", kind="fl_ru", enabled=False, state="blocked"),
    ]
    keyboard = sources_keyboard(statuses)
    data = [button.callback_data for row in keyboard.inline_keyboard for button in row if button.callback_data]
    assert "src:toggle:kwork" in data
    assert "src:check:fl_ru" in data


def test_scoring_from_row(make_job) -> None:
    row = Job(
        source="kwork",
        external_id="1",
        url="https://kwork.ru/1",
        title="Заказ",
        score=71.0,
        base_score=68.0,
        llm_score=77.0,
        score_components={"freshness": 0.8, "competition": 0.6},
        score_reasons=["свежий"],
    )
    result = scoring_from_row(row)
    assert result.total_score == 71.0
    assert result.base_score == 68.0
    assert result.llm_score == 77.0
    assert result.components.freshness == 0.8
    assert result.priority == "good"
