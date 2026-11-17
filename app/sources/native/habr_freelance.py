"""Habr Freelance adapter (RSS + detail page).

Note: the service announced permanent closure ("Сервис Хабр Фриланс закрылся
навсегда"), and /tasks.rss now returns an HTML notice. The adapter detects this
and raises a clear SourceBlocked so the circuit breaker and the health alert
work instead of crashing the engine.
"""

from __future__ import annotations

import re

import feedparser
from loguru import logger

from app.models import UnifiedJob
from app.sources.base import (
    BaseSourceAdapter,
    SourceBlocked,
    SourceError,
    SourceParseError,
    extract_budget,
)

SHUTDOWN_MARKERS = ("закрылся навсегда", "сервис закрыт", "проект закрыт", "habr freelance закрыт")
TASK_ID_RE = re.compile(r"/tasks/(\d+)")


class HabrFreelanceAdapter(BaseSourceAdapter):
    kind = "habr_freelance"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.rss_url = str(self.config.params.get("rss_url") or "https://freelance.habr.com/tasks.rss")
        self.fetch_details_enabled = bool(self.config.params.get("fetch_details", True))

    async def fetch_listing(self) -> list[UnifiedJob]:
        result = await self.fetch_page(self.rss_url)
        payload = result.raw if result.raw else result.text
        text_probe = result.text.lower()
        if any(marker in text_probe for marker in SHUTDOWN_MARKERS):
            raise SourceBlocked(
                "Хабр Фриланс закрыт навсегда - источник отключён администрацией сервиса", source=self.name
            )
        parsed = feedparser.parse(payload)
        if not parsed.entries:
            if "<html" in text_probe:
                raise SourceBlocked("Хабр Фриланс отдаёт HTML вместо RSS (сервис недоступен)", source=self.name)
            raise SourceParseError("RSS Хабр Фриланс пуст", source=self.name)
        jobs: list[UnifiedJob] = []
        for entry in parsed.entries[: self.config.max_items]:
            title = str(entry.get("title") or "").strip()
            link = str(entry.get("link") or "").strip()
            if not title or not link:
                continue
            description = self.clean_html(str(entry.get("summary") or entry.get("description") or ""))
            budget_min, budget_max, currency = extract_budget(f"{title} {description}", default_currency="RUB")
            tags = [str(tag.get("term", "")) for tag in entry.get("tags", []) if isinstance(tag, dict)]
            jobs.append(
                self.build_job(
                    external_id=self._external_id(link),
                    url=link,
                    title=title,
                    description=description,
                    budget_min=budget_min,
                    budget_max=budget_max,
                    currency=currency,
                    proposals_count=self.extract_proposals(description),
                    status="open",
                    published_at=self.parse_datetime(entry.get("published_parsed") or entry.get("published")),
                    skills=tags,
                    extra={"feed": "rss"},
                )
            )
        if not jobs:
            raise SourceParseError("Не удалось разобрать ни одного заказа", source=self.name)
        return await self.finalize(jobs)

    async def fetch_details(self, job: UnifiedJob) -> UnifiedJob:
        if not self.fetch_details_enabled or not job.url:
            return job
        try:
            soup, _ = await self.get_soup(job.url)
        except SourceError as exc:
            logger.debug(f"habr: карточка недоступна ({exc})")
            return job
        body = soup.select_one(".task__description, .task__body, article, .content")
        if body is not None:
            text = self.clean_html(body.get_text(" ", strip=True))
            if len(text) > len(job.description):
                job.description = text[:12000]
        price_text = self.clean_html(
            (soup.select_one(".task__price, [class*='price']") or soup).get_text(" ", strip=True)
        )
        low, high, currency = extract_budget(price_text, default_currency=job.currency)
        if low is not None:
            job.budget_min, job.budget_max, job.currency = low, high, currency
            job.budget_type = "fixed"
            await self.converter.enrich(job)
        proposals = self.extract_proposals(soup.get_text(" ", strip=True))
        if proposals is not None:
            job.proposals_count = proposals
        return job

    def needs_details(self, job: UnifiedJob) -> bool:
        return self.fetch_details_enabled and bool(job.url)

    @staticmethod
    def _external_id(url: str) -> str:
        match = TASK_ID_RE.search(url)
        return match.group(1) if match else url.rstrip("/").rsplit("/", 1)[-1]
