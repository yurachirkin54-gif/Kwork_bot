"""Shared test fixtures: offline HTTP stub, fixed-rate converter, builders."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from app.config import ConfigManager, SourceConfig
from app.core.currency import CurrencyConverter
from app.models import UnifiedJob, utcnow
from app.sources.http_client import FetchResult, HttpError

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


class StubHttp:
    """Drop-in replacement for HttpClient used by adapters in tests."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = dict(routes)
        self.calls: list[str] = []

    def _match(self, url: str) -> str | None:
        # Longest prefix wins, so /projects/101 beats /projects
        candidates = [key for key in self.routes if url.startswith(key)]
        return max(candidates, key=len) if candidates else None

    async def get(self, url: str, **kwargs: Any) -> FetchResult:
        self.calls.append(url)
        key = self._match(url)
        if key is None:
            raise HttpError(404, url, "no stub route")
        value = self.routes[key]
        if callable(value):
            value = value(url, kwargs)
        if isinstance(value, HttpError):
            raise value
        if isinstance(value, bytes):
            raw = value
            text = raw.decode("utf-8", errors="replace")
        else:
            text = str(value)
            raw = text.encode("utf-8")
        return FetchResult(text=text, status=200, url=url, raw=raw)

    async def aclose(self) -> None:  # pragma: no cover - stub
        return None


@pytest.fixture
def fixture_bytes() -> Any:
    return load_fixture


@pytest.fixture
def stub_factory() -> Any:
    return lambda routes: StubHttp(routes)


@pytest.fixture
def converter() -> CurrencyConverter:
    instance = CurrencyConverter()
    instance.rates = {
        "RUB": 1.0,
        "USD": 90.0,
        "EUR": 100.0,
        "UAH": 2.2,
        "KZT": 0.2,
        "INR": 1.1,
        "GBP": 115.0,
    }
    instance.updated_at = utcnow()
    instance._fetched_monotonic = time.monotonic()
    instance.source = "test"
    return instance


@pytest.fixture
def make_config() -> Any:
    def _factory(name: str = "test", kind: str = "rss", **overrides: Any) -> SourceConfig:
        payload: dict[str, Any] = {
            "name": name,
            "kind": kind,
            "enabled": True,
            "interval_seconds": 60,
            "timeout": 5,
            "max_items": 30,
        }
        payload.update(overrides)
        return SourceConfig.model_validate(payload)

    return _factory


@pytest.fixture
def make_job() -> Any:
    def _factory(**overrides: Any) -> UnifiedJob:
        payload: dict[str, Any] = {
            "source": "test",
            "external_id": "1",
            "url": "https://example.com/jobs/1",
            "title": "Нужен парсер каталога на Python",
            "description": (
                "Нужен парсер каталога на Python. Требования: requests, CSV, обработка ошибок. "
                "Срок 2 дня, бюджет обсуждаем."
            ),
            "budget_min": 5000,
            "budget_max": 5000,
            "currency": "RUB",
            "budget_type": "fixed",
            "budget_rub": 5000,
            "budget_usd": 55.0,
            "proposals_count": 0,
            "status": "open",
            "published_at": utcnow(),
            "client_verified": True,
            "client_rating": 4.9,
            "client_reviews": 25,
            "client_hire_rate": 0.8,
        }
        payload.update(overrides)
        return UnifiedJob(**payload)

    return _factory


@pytest.fixture
def config_manager() -> ConfigManager:
    return ConfigManager()
