"""Universal engines: RSS, HTML/CSS and JSON mapping."""

from __future__ import annotations

import pytest

from app.sources.base import SourceBlocked, SourceParseError
from app.sources.generic_html import GenericHTMLAdapter, split_selector
from app.sources.generic_json import GenericJSONAdapter, dig, normalize_api_status
from app.sources.generic_rss import GenericRSSAdapter


async def test_rss_adapter_parses_feed(fixture_bytes, make_config, stub_factory, converter) -> None:
    config = make_config(kind="rss", params={"url": "https://feed.test/rss"})
    http = stub_factory({"https://feed.test/rss": fixture_bytes("generic_rss.xml")})
    adapter = GenericRSSAdapter(config, http, converter)
    jobs = await adapter.fetch_listing()

    assert len(jobs) == 2
    first = jobs[0]
    assert first.external_id == "feed-1"
    assert "парсер" in first.title.lower()
    assert first.url == "https://feed.test/jobs/1"
    assert first.budget_min == 9000
    assert first.currency == "RUB"
    assert first.proposals_count == 3
    assert first.published_at is not None
    assert "<p>" not in first.description
    assert first.status == "open"

    second = jobs[1]
    assert second.budget_min is None
    assert second.budget_type == "negotiable"


async def test_rss_adapter_raises_on_garbage(make_config, stub_factory, converter) -> None:
    config = make_config(kind="rss", params={"url": "https://feed.test/rss"})
    http = stub_factory({"https://feed.test/rss": "<html><body>nothing</body></html>"})
    adapter = GenericRSSAdapter(config, http, converter)
    with pytest.raises(SourceParseError):
        await adapter.fetch_listing()


async def test_rss_adapter_detects_block_page(make_config, stub_factory, converter, fixture_bytes) -> None:
    config = make_config(kind="rss", params={"url": "https://blocked.test"})
    http = stub_factory({"https://blocked.test": fixture_bytes("cloudflare.html")})
    adapter = GenericRSSAdapter(config, http, converter)
    with pytest.raises(SourceBlocked):
        await adapter.fetch_listing()


def test_split_selector() -> None:
    assert split_selector("a.title@href") == ("a.title", "href")
    assert split_selector("div.card") == ("div.card", None)


async def test_html_adapter_parses_cards(fixture_bytes, make_config, stub_factory, converter) -> None:
    config = make_config(
        kind="html",
        params={"url": "https://board.test/projects"},
        selectors={
            "item": "div.card",
            "title": "a.title",
            "url": "a.title@href",
            "price": "span.price",
            "proposals": "span.proposals",
            "description": "div.desc",
            "published_at": "time@datetime",
            "client_rating": "span.rating",
            "skills": "span.tag",
        },
    )
    http = stub_factory({"https://board.test/projects": fixture_bytes("generic_listing.html")})
    adapter = GenericHTMLAdapter(config, http, converter)
    jobs = await adapter.fetch_listing()

    assert len(jobs) == 2
    first = jobs[0]
    assert first.external_id == "101"
    assert first.url == "https://board.test/projects/101"
    assert first.budget_min == 7500
    assert first.proposals_count == 2
    assert first.client_rating == 4.8
    assert first.skills == ["Python", "Боты"]
    assert first.published_at is not None
    assert "aiogram" in first.description

    second = jobs[1]
    assert second.budget_min == 1000
    assert second.proposals_count == 0


async def test_html_adapter_detail_enrichment(
    fixture_bytes, make_config, stub_factory, converter
) -> None:
    config = make_config(
        kind="html",
        params={"url": "https://board.test/projects", "fetch_details": True},
        selectors={
            "item": "div.card",
            "title": "a.title",
            "url": "a.title@href",
            "description": "div.desc",
            "proposals": "span.proposals",
            "detail": {"container": "article", "description": "#full", "proposals": ".bids", "status": ".status"},
        },
    )
    http = stub_factory(
        {
            "https://board.test/projects": fixture_bytes("generic_listing.html"),
            "https://board.test/projects/101": fixture_bytes("generic_detail.html"),
        }
    )
    adapter = GenericHTMLAdapter(config, http, converter)
    jobs = await adapter.fetch_listing()
    enriched = await adapter.fetch_details(jobs[0])
    assert "Полное описание" in enriched.description
    assert enriched.proposals_count == 5
    assert enriched.status == "open"


async def test_html_adapter_empty_page(make_config, stub_factory, converter) -> None:
    config = make_config(
        kind="html", params={"url": "https://board.test"}, selectors={"item": "div.card", "title": "a"}
    )
    http = stub_factory({"https://board.test": "<html><body><p>empty</p></body></html>"})
    adapter = GenericHTMLAdapter(config, http, converter)
    with pytest.raises(SourceParseError):
        await adapter.fetch_listing()


def test_dig_paths() -> None:
    payload = {"data": {"jobs": [{"id": 1, "client": {"rating": 4.5}}]}}
    assert dig(payload, "data.jobs.0.client.rating") == 4.5
    assert dig(payload, "data.missing", "fallback") == "fallback"
    assert dig(payload, "") is payload


def test_normalize_api_status() -> None:
    assert normalize_api_status("active") == "open"
    assert normalize_api_status(True) == "open"
    assert normalize_api_status("closed") == "closed"
    assert normalize_api_status("in_progress") == "in_progress"
    assert normalize_api_status("weird") == "unknown"


async def test_json_adapter_mapping(fixture_bytes, make_config, stub_factory, converter) -> None:
    config = make_config(
        kind="json",
        params={"url": "https://api.test/v1/jobs"},
        mapping={
            "items_path": "data.jobs",
            "external_id": "id",
            "title": "title",
            "url": "url",
            "description": "description",
            "budget_min": "budget.from",
            "budget_max": "budget.to",
            "currency": "budget.currency",
            "proposals": "proposals_count",
            "published_at": "created_at",
            "status": "status",
            "client_rating": "client.rating",
            "client_reviews": "client.reviews",
            "skills": "skills",
            "category": "category",
            "url_template": "https://api.test/jobs/{external_id}",
        },
    )
    http = stub_factory({"https://api.test/v1/jobs": fixture_bytes("generic_api.json")})
    adapter = GenericJSONAdapter(config, http, converter)
    jobs = await adapter.fetch_listing()

    assert len(jobs) == 2
    first = jobs[0]
    assert first.external_id == "a1"
    assert first.url == "https://api.test/jobs/a1"
    assert first.budget_min == 3000
    assert first.budget_max == 5000
    assert first.proposals_count == 1
    assert first.client_rating == 4.7
    assert first.client_reviews == 12
    assert first.skills == ["Python", "Excel"]
    assert first.category == "Автоматизация"
    assert first.status == "open"

    second = jobs[1]
    assert second.url == "https://api.test/jobs/a2"  # built from url_template
    assert second.status == "closed"


async def test_json_adapter_invalid_payload(make_config, stub_factory, converter) -> None:
    config = make_config(kind="json", params={"url": "https://api.test"}, mapping={"items_path": "data.jobs"})
    http = stub_factory({"https://api.test": "<html>not json</html>"})
    adapter = GenericJSONAdapter(config, http, converter)
    with pytest.raises(SourceParseError):
        await adapter.fetch_listing()
