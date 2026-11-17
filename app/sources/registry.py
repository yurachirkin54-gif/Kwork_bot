"""Adapter registry: maps a YAML `kind` to an adapter class and builds instances."""

from __future__ import annotations

from collections.abc import Iterable

from loguru import logger

from app.config import SourceConfig
from app.core.currency import CurrencyConverter
from app.sources.base import BaseSourceAdapter
from app.sources.browser_client import BrowserClient
from app.sources.generic_html import GenericHTMLAdapter
from app.sources.generic_json import GenericJSONAdapter
from app.sources.generic_rss import GenericRSSAdapter
from app.sources.http_client import HttpClient
from app.sources.native.fl_ru import FlRuAdapter
from app.sources.native.freelance_ru import FreelanceRuAdapter
from app.sources.native.freelancehunt import FreelanceHuntAdapter
from app.sources.native.freelancer_com import FreelancerComAdapter
from app.sources.native.habr_freelance import HabrFreelanceAdapter
from app.sources.native.kwork import KworkAdapter
from app.sources.native.remote_boards import RemoteBoardsAdapter
from app.sources.native.weblancer import WeblancerAdapter
from app.sources.native.workspace import WorkspaceAdapter
from app.sources.native.youdo import YouDoAdapter

GENERIC_ADAPTERS: dict[str, type[BaseSourceAdapter]] = {
    "rss": GenericRSSAdapter,
    "atom": GenericRSSAdapter,
    "html": GenericHTMLAdapter,
    "css": GenericHTMLAdapter,
    "json": GenericJSONAdapter,
    "api": GenericJSONAdapter,
    "graphql": GenericJSONAdapter,
}

NATIVE_ADAPTERS: dict[str, type[BaseSourceAdapter]] = {
    "kwork": KworkAdapter,
    "fl_ru": FlRuAdapter,
    "freelance_ru": FreelanceRuAdapter,
    "habr_freelance": HabrFreelanceAdapter,
    "freelancehunt": FreelanceHuntAdapter,
    "weblancer": WeblancerAdapter,
    "youdo": YouDoAdapter,
    "workspace": WorkspaceAdapter,
    "freelancer_com": FreelancerComAdapter,
    "remote_boards": RemoteBoardsAdapter,
}

ADAPTER_REGISTRY: dict[str, type[BaseSourceAdapter]] = {**NATIVE_ADAPTERS, **GENERIC_ADAPTERS}


def available_kinds() -> list[str]:
    return sorted(ADAPTER_REGISTRY)


def resolve_adapter(kind: str) -> type[BaseSourceAdapter] | None:
    return ADAPTER_REGISTRY.get(str(kind).strip().lower())


def create_adapter(
    config: SourceConfig,
    http: HttpClient,
    converter: CurrencyConverter,
    browser: BrowserClient | None = None,
) -> BaseSourceAdapter | None:
    adapter_cls = resolve_adapter(config.kind)
    if adapter_cls is None:
        logger.error(f"Неизвестный тип источника {config.kind!r} ({config.name}) - пропущен")
        return None
    try:
        return adapter_cls(config, http, converter, browser=browser)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(f"Не удалось создать адаптер {config.name}: {exc}")
        return None


def build_adapters(
    configs: Iterable[SourceConfig],
    http: HttpClient,
    converter: CurrencyConverter,
    browser: BrowserClient | None = None,
) -> list[BaseSourceAdapter]:
    adapters: list[BaseSourceAdapter] = []
    for config in configs:
        adapter = create_adapter(config, http, converter, browser)
        if adapter is not None:
            adapters.append(adapter)
    return adapters
