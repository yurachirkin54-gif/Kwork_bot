"""Optional LLM stage: strict JSON, blending and transparent fallback."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.config import LLMConfig
from app.models import ScoringResult
from app.processing.llm_analyzer import (
    LLMAnalyzer,
    LLMResponseError,
    template_proposal,
)

ANALYSIS = {
    "easiness_score": 8,
    "price_fairness_score": 7,
    "clarity_score": 9,
    "scam_or_toxic_risk": "low",
    "estimated_time": "2-4 часа",
    "summary_ru": "Нужен парсер каталога на Python с выгрузкой в CSV.",
    "red_flags": [],
    "draft_proposal": "Здравствуйте! Сделаю парсер на Python, покажу результат через 2 часа.",
}

LLM_URL = "https://llm.test/v1/chat/completions"


def config(**overrides) -> LLMConfig:
    payload = {
        "enabled": True,
        "api_key": "test-key",
        "base_url": "https://llm.test/v1",
        "model": "test-model",
        "timeout": 5,
        "base_weight": 0.65,
    }
    payload.update(overrides)
    return LLMConfig(**payload)


def base_score() -> ScoringResult:
    return ScoringResult(base_score=60.0, total_score=60.0, priority="good", reasons=["свежий заказ"])


def completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def test_disabled_llm_is_transparent(make_job) -> None:
    analyzer = LLMAnalyzer(LLMConfig(enabled=False))
    job = make_job()
    assert analyzer.enabled is False
    assert await analyzer.analyze(job) is None
    result = await analyzer.enrich(job, base_score())
    assert result.total_score == 60.0
    assert result.llm is None
    assert await analyzer.draft_proposal(job) is None


def test_parse_analysis_with_code_fence() -> None:
    payload = "```json\n" + json.dumps(ANALYSIS, ensure_ascii=False) + "\n```"
    analysis = LLMAnalyzer.parse_analysis(payload)
    assert analysis.easiness_score == 8
    assert analysis.scam_or_toxic_risk == "low"
    assert analysis.summary_ru.startswith("Нужен парсер")


def test_parse_analysis_rejects_garbage() -> None:
    with pytest.raises(LLMResponseError):
        LLMAnalyzer.parse_analysis("sorry, I cannot help")
    with pytest.raises(LLMResponseError):
        LLMAnalyzer.parse_analysis('{"easiness_score": 99}')


async def test_analyze_success_and_combine(make_job) -> None:
    analyzer = LLMAnalyzer(config())
    async with respx.mock(assert_all_called=False) as mock:
        route = mock.post(LLM_URL).mock(return_value=completion(json.dumps(ANALYSIS, ensure_ascii=False)))
        job = make_job()
        analysis = await analyzer.analyze(job)
        assert analysis is not None
        assert route.call_count == 1
        assert analysis.easiness_score == 8

        result = analyzer.combine(base_score(), analysis)
        assert result.llm is not None
        assert result.llm_score == pytest.approx(79.0, abs=0.1)
        assert result.total_score == pytest.approx(66.7, abs=0.2)
        assert result.total_score > 60
        assert any("AI" in reason for reason in result.reasons)
    await analyzer.aclose()


async def test_risk_penalty(make_job) -> None:
    analyzer = LLMAnalyzer(config())
    low = analyzer.combine(base_score(), LLMAnalyzer.parse_analysis(json.dumps(ANALYSIS)))
    risky_payload = dict(ANALYSIS, scam_or_toxic_risk="high")
    high = analyzer.combine(base_score(), LLMAnalyzer.parse_analysis(json.dumps(risky_payload)))
    assert high.total_score < low.total_score
    assert high.total_score == pytest.approx(max(0.0, low.total_score - 12), abs=0.2)


async def test_llm_http_error_falls_back(make_job) -> None:
    analyzer = LLMAnalyzer(config())
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(LLM_URL).mock(return_value=httpx.Response(500, text="server error"))
        job = make_job()
        assert await analyzer.analyze(job) is None
        result = await analyzer.enrich(job, base_score())
        assert result.total_score == 60.0
    await analyzer.aclose()


async def test_llm_invalid_json_falls_back(make_job) -> None:
    analyzer = LLMAnalyzer(config())
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(LLM_URL).mock(return_value=completion("Вот ваш JSON: {oops"))
        assert await analyzer.analyze(make_job()) is None
    await analyzer.aclose()


async def test_llm_retries_without_response_format(make_job) -> None:
    analyzer = LLMAnalyzer(config())
    async with respx.mock(assert_all_called=False) as mock:
        route = mock.post(LLM_URL).mock(
            side_effect=[
                httpx.Response(400, json={"error": "response_format unsupported"}),
                completion(json.dumps(ANALYSIS, ensure_ascii=False)),
            ]
        )
        analysis = await analyzer.analyze(make_job())
        assert analysis is not None
        assert route.call_count == 2
    await analyzer.aclose()


def test_template_proposal_offline(make_job) -> None:
    text = template_proposal(make_job())
    assert "Здравствуйте" in text
    assert len(text) > 100
