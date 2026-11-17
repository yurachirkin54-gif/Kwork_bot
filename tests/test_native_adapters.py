"""Native adapters: Kwork, FL.ru, Freelance.ru, Habr, Freelancehunt, Freelancer.com."""

from __future__ import annotations

import pytest

from app.sources.base import SourceBlocked
from app.sources.http_client import HttpError
from app.sources.native.fl_ru import FlRuAdapter
from app.sources.native.freelance_ru import FreelanceRuAdapter
from app.sources.native.freelancehunt import FreelanceHuntAdapter
from app.sources.native.freelancer_com import FreelancerComAdapter
from app.sources.native.habr_freelance import HabrFreelanceAdapter
from app.sources.native.kwork import KworkAdapter


async def test_kwork_rss_and_detail(fixture_bytes, make_config, stub_factory, converter) -> None:
    config = make_config(
        name="kwork", kind="kwork", params={"rss_url": "https://kwork.ru/rss", "fetch_details": True}
    )
    http = stub_factory(
        {
            "https://kwork.ru/rss": fixture_bytes("kwork_rss.xml"),
            "https://kwork.ru/script-programming/55524084": fixture_bytes("kwork_detail.html"),
        }
    )
    adapter = KworkAdapter(config, http, converter)
    jobs = await adapter.fetch_listing()

    assert len(jobs) == 2
    first = jobs[0]
    assert first.source == "kwork"
    assert first.external_id == "55524084"
    assert first.budget_min == 5000
    assert first.budget_rub == 5000
    assert first.status == "open"
    assert first.category == "Разработка и IT"
    assert "Python" in first.description

    enriched = await adapter.fetch_details(first)
    assert enriched.proposals_count == 3
    assert "CSV" in enriched.description
    assert enriched.budget_min == 5500
    assert enriched.client_rating == pytest.approx(4.9)


async def test_kwork_html_fallback(make_config, stub_factory, converter, fixture_bytes) -> None:
    config = make_config(
        name="kwork", kind="kwork", params={"rss_url": "https://kwork.ru/rss", "listing_url": "https://kwork.ru/projects"}
    )
    http = stub_factory(
        {
            "https://kwork.ru/rss": "<rss version='2.0'><channel></channel></rss>",
            "https://kwork.ru/projects": fixture_bytes("kwork_detail.html"),
        }
    )
    adapter = KworkAdapter(config, http, converter)
    jobs = await adapter.fetch_listing()
    assert len(jobs) == 1
    assert jobs[0].external_id == "55524084"
    assert jobs[0].budget_min == 5500


async def test_fl_ru_extracts_budget_from_title(fixture_bytes, make_config, stub_factory, converter) -> None:
    config = make_config(name="fl_ru", kind="fl_ru", params={"rss_url": "https://www.fl.ru/rss/all.xml"})
    http = stub_factory({"https://www.fl.ru/rss/all.xml": fixture_bytes("fl_rss.xml")})
    adapter = FlRuAdapter(config, http, converter)
    jobs = await adapter.fetch_listing()

    assert len(jobs) == 2
    first = jobs[0]
    assert first.budget_min == 3000
    assert first.budget_rub == 3000
    assert "₽" in first.title
    assert "&#8381;" not in first.title
    assert first.source == "fl_ru"
    assert first.published_at is not None


async def test_freelance_ru_html(fixture_bytes, make_config, stub_factory, converter) -> None:
    config = make_config(
        name="freelance_ru", kind="freelance_ru", params={"url": "https://freelance.ru/projects/"}
    )
    http = stub_factory({"https://freelance.ru/projects/": fixture_bytes("freelance_ru.html")})
    adapter = FreelanceRuAdapter(config, http, converter)
    jobs = await adapter.fetch_listing()

    assert len(jobs) == 2
    first = jobs[0]
    assert first.external_id == "15718"
    assert first.url == "https://freelance.ru/task/view/15718"
    assert first.budget_min == 5500
    assert first.proposals_count == 1
    assert first.category == "Фото и Контент"
    assert first.published_at is not None
    assert first.published_at.year == 2026

    second = jobs[1]
    assert second.proposals_count == 6
    assert second.budget_min == 550


async def test_habr_closed_service_is_blocked(fixture_bytes, make_config, stub_factory, converter) -> None:
    config = make_config(name="habr_freelance", kind="habr_freelance")
    http = stub_factory({"https://freelance.habr.com/tasks.rss": fixture_bytes("habr_closed.html")})
    adapter = HabrFreelanceAdapter(config, http, converter)
    with pytest.raises(SourceBlocked):
        await adapter.fetch_listing()


async def test_habr_rss_parsing(fixture_bytes, make_config, stub_factory, converter) -> None:
    config = make_config(
        name="habr_freelance",
        kind="habr_freelance",
        params={"rss_url": "https://freelance.habr.com/tasks.rss", "fetch_details": False},
    )
    http = stub_factory({"https://freelance.habr.com/tasks.rss": fixture_bytes("habr_rss.xml")})
    adapter = HabrFreelanceAdapter(config, http, converter)
    jobs = await adapter.fetch_listing()
    assert len(jobs) == 1
    assert jobs[0].external_id == "604321"
    assert jobs[0].budget_min == 6000
    assert jobs[0].proposals_count == 2


async def test_freelancehunt_api(
    fixture_bytes, make_config, stub_factory, converter, monkeypatch
) -> None:
    monkeypatch.setenv("FREELANCEHUNT_TOKEN", "test-token")
    config = make_config(
        name="freelancehunt",
        kind="freelancehunt",
        token_env="FREELANCEHUNT_TOKEN",
        params={
            "api_url": "https://api.freelancehunt.com/v2/projects",
            "fallback_rss": "https://freelancehunt.com/projects.rss",
        },
    )
    http = stub_factory({"https://api.freelancehunt.com/v2/projects": fixture_bytes("freelancehunt_api.json")})
    adapter = FreelanceHuntAdapter(config, http, converter)
    jobs = await adapter.fetch_listing()

    assert len(jobs) == 2
    first = jobs[0]
    assert first.external_id == "1665218"
    assert first.proposals_count == 2
    assert first.budget_min == 3000
    assert first.currency == "UAH"
    assert first.budget_rub is not None
    assert first.published_at is not None


async def test_freelancehunt_rss_fallback(
    fixture_bytes, make_config, stub_factory, converter, monkeypatch
) -> None:
    monkeypatch.delenv("FREELANCEHUNT_TOKEN", raising=False)
    config = make_config(
        name="freelancehunt",
        kind="freelancehunt",
        token_env="FREELANCEHUNT_TOKEN",
        params={
            "api_url": "https://api.freelancehunt.com/v2/projects",
            "fallback_rss": "https://freelancehunt.com/projects.rss",
        },
    )
    http = stub_factory({"https://freelancehunt.com/projects.rss": fixture_bytes("freelancehunt_rss.xml")})
    adapter = FreelanceHuntAdapter(config, http, converter)
    jobs = await adapter.fetch_listing()

    assert len(jobs) == 1
    assert "Парсер" in jobs[0].title
    assert jobs[0].budget_min == 4000
    assert jobs[0].currency == "UAH"
    assert jobs[0].proposals_count == 4


async def test_freelancehunt_blocked_without_alternatives(
    make_config, stub_factory, converter, monkeypatch
) -> None:
    monkeypatch.setenv("FREELANCEHUNT_TOKEN", "test-token")
    config = make_config(
        name="freelancehunt",
        kind="freelancehunt",
        token_env="FREELANCEHUNT_TOKEN",
        params={
            "api_url": "https://api.freelancehunt.com/v2/projects",
            "fallback_rss": "https://freelancehunt.com/projects.rss",
        },
    )
    http = stub_factory(
        {
            "https://api.freelancehunt.com/v2/projects": HttpError(403, "api", "forbidden"),
            "https://freelancehunt.com/projects.rss": HttpError(403, "rss", "cloudflare"),
        }
    )
    adapter = FreelanceHuntAdapter(config, http, converter)
    with pytest.raises(SourceBlocked):
        await adapter.fetch_listing()


async def test_freelancer_com_api(fixture_bytes, make_config, stub_factory, converter) -> None:
    config = make_config(
        name="freelancer_com",
        kind="freelancer_com",
        params={
            "api_url": "https://www.freelancer.com/api/projects/0.1/projects/active/",
            "query": "python",
            "limit": 10,
        },
    )
    http = stub_factory(
        {"https://www.freelancer.com/api/projects/0.1/projects/active/": fixture_bytes("freelancer_api.json")}
    )
    adapter = FreelancerComAdapter(config, http, converter)
    jobs = await adapter.fetch_listing()

    assert len(jobs) == 2
    first = jobs[0]
    assert first.external_id == "40822850"
    assert first.url == "https://www.freelancer.com/projects/android/Secure-Android-APK-Audit-Signing"
    assert first.proposals_count == 16
    assert first.budget_min == 1500
    assert first.budget_max == 12500
    assert first.currency == "INR"
    assert first.budget_type == "fixed"
    assert first.skills == ["Python", "Android"]
    assert first.status == "open"

    second = jobs[1]
    assert second.budget_type == "negotiable"
    assert second.budget_min is None

    assert "job_details=true" in http.calls[0]
    assert "query=python" in http.calls[0]
