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
from app.sources.native.weblancer import WeblancerAdapter
from app.sources.native.workspace import WorkspaceAdapter


async def test_kwork_statedata_and_detail(fixture_bytes, make_config, stub_factory, converter) -> None:
    config = make_config(
        name="kwork",
        kind="kwork",
        params={"listing_url": "https://kwork.ru/projects", "fetch_details": True},
    )
    http = stub_factory(
        {
            "https://kwork.ru/projects/3274099/view": fixture_bytes("kwork_detail_state.html"),
            "https://kwork.ru/projects": fixture_bytes("kwork_projects.html"),
        }
    )
    adapter = KworkAdapter(config, http, converter)
    jobs = await adapter.fetch_listing()

    assert len(jobs) == 2
    first = jobs[0]
    assert first.source == "kwork"
    assert first.external_id == "3274099"
    assert first.url == "https://kwork.ru/projects/3274099/view"
    assert first.budget_min == 4000
    assert first.budget_max == 7000
    assert first.budget_rub == 7000
    assert first.proposals_count == 2
    assert first.status == "open"
    assert first.category is not None and "Разработка и IT" in first.category
    assert first.client_hire_rate == pytest.approx(0.8)
    # 18:00 MSK (UTC+3) -> 15:00 UTC
    assert first.published_at is not None and first.published_at.hour == 15
    assert adapter.needs_details(first) is False

    second = jobs[1]
    assert second.status == "closed"

    enriched = await adapter.fetch_details(first)
    assert enriched.proposals_count == 5
    assert "Расширенное ТЗ" in enriched.description
    assert enriched.budget_min == 4500
    assert enriched.budget_max == 7500


async def test_kwork_html_fallback(make_config, stub_factory, converter, fixture_bytes) -> None:
    config = make_config(
        name="kwork", kind="kwork", params={"listing_url": "https://kwork.ru/projects", "fetch_details": True}
    )
    http = stub_factory(
        {
            "https://kwork.ru/projects": fixture_bytes("kwork_detail.html"),
        }
    )
    adapter = KworkAdapter(config, http, converter)
    jobs = await adapter.fetch_listing()
    assert len(jobs) == 1
    assert jobs[0].external_id == "55524084"
    assert jobs[0].budget_min == 5500
    assert jobs[0].proposals_count == 3


async def test_fl_ru_html_with_proposals(fixture_bytes, make_config, stub_factory, converter) -> None:
    config = make_config(
        name="fl_ru",
        kind="fl_ru",
        params={"listing_url": "https://www.fl.ru/projects/", "fetch_details": False},
    )
    http = stub_factory({"https://www.fl.ru/projects/": fixture_bytes("fl_projects.html")})
    adapter = FlRuAdapter(config, http, converter)
    jobs = await adapter.fetch_listing()

    assert len(jobs) == 2
    assert jobs[0].external_id == "5529633"
    assert jobs[0].budget_min == 6000
    assert jobs[0].proposals_count == 1
    assert jobs[0].published_at is not None
    assert jobs[1].proposals_count == 0
    assert jobs[1].budget_type == "negotiable"


async def test_fl_ru_extracts_budget_from_title(fixture_bytes, make_config, stub_factory, converter) -> None:
    config = make_config(
        name="fl_ru",
        kind="fl_ru",
        params={"listing_url": "https://www.fl.ru/projects/", "rss_url": "https://www.fl.ru/rss/all.xml"},
    )
    http = stub_factory(
        {
            "https://www.fl.ru/projects/": HttpError(503, "fl", "unavailable"),
            "https://www.fl.ru/rss/all.xml": fixture_bytes("fl_rss.xml"),
        }
    )
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


async def test_weblancer_rsc_stream(fixture_bytes, make_config, stub_factory, converter) -> None:
    config = make_config(
        name="weblancer", kind="weblancer", currency="USD", params={"url": "https://www.weblancer.net/freelance/"}
    )
    http = stub_factory({"https://www.weblancer.net/freelance/": fixture_bytes("weblancer_rsc.html")})
    adapter = WeblancerAdapter(config, http, converter)
    jobs = await adapter.fetch_listing()

    assert len(jobs) == 2
    first = jobs[0]
    assert first.external_id == "1270601"
    assert first.currency == "USD"
    assert first.budget_min == 500
    assert first.budget_usd == 500
    assert first.proposals_count == 2
    assert "парсингу сайта" in first.description
    assert first.client_verified is True
    assert first.client_hire_rate == pytest.approx(0.75)
    assert first.client_reviews == 12
    assert first.skills == ["Python"]

    second = jobs[1]
    assert second.budget_type == "negotiable"
    assert second.proposals_count == 0


async def test_workspace_rss_ignores_deadline_dates(fixture_bytes, make_config, stub_factory, converter) -> None:
    config = make_config(
        name="workspace",
        kind="workspace",
        params={"rss_url": "https://workspace.ru/tenders/rss/", "url": "https://workspace.ru/tenders/"},
    )
    http = stub_factory({"https://workspace.ru/tenders/rss/": fixture_bytes("workspace_rss.xml")})
    adapter = WorkspaceAdapter(config, http, converter)
    jobs = await adapter.fetch_listing()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.external_id == "19152"
    assert job.budget_min == 50000
    assert job.budget_max == 120000
    assert job.proposals_count is None  # Must NOT match 27 from 27.11.2026!


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
