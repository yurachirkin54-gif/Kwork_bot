"""FL.ru adapter: official public RSS feed (cp1251) with budget in the title."""

from __future__ import annotations

from app.config import SourceConfig
from app.core.currency import CurrencyConverter
from app.sources.base import extract_budget
from app.sources.generic_rss import GenericRSSAdapter
from app.sources.http_client import HttpClient


class FlRuAdapter(GenericRSSAdapter):
    kind = "fl_ru"

    def __init__(
        self,
        config: SourceConfig,
        http: HttpClient,
        converter: CurrencyConverter,
        *,
        browser=None,
    ) -> None:
        super().__init__(config, http, converter, browser=browser)
        self.feed_url = str(
            config.params.get("rss_url") or config.params.get("url") or "https://www.fl.ru/rss/all.xml"
        )

    def parse_entry(self, entry, *, base_url: str = ""):
        job = super().parse_entry(entry, base_url=base_url)
        if job is None:
            return None
        # FL.ru puts the budget into the title: "... (Бюджет: 1 000 ₽)"
        if job.budget_min is None:
            low, high, currency = extract_budget(job.title, default_currency="RUB")
            if low is not None:
                job.budget_min = low
                job.budget_max = high
                job.currency = currency
                job.budget_type = "fixed"
        job.status = "open"
        job.extra["feed"] = "fl.ru rss"
        return job
