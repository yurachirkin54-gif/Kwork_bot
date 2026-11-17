"""Kwork adapter.

Kwork's project catalogue is no longer server-rendered for anonymous visitors,
but the marketplace publishes an official public RSS feed (https://kwork.ru/rss)
with the latest projects. HTML parsing is kept as a fallback for markup
variations and for optional detail enrichment.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

import feedparser
from bs4 import BeautifulSoup
from loguru import logger

from app.models import UnifiedJob
from app.sources.base import (
    BaseSourceAdapter,
    SourceError,
    SourceParseError,
    extract_budget,
    extract_int,
)

PROJECT_ID_RE = re.compile(r"/(\d{6,})(?:[/?#]|$)")


class KworkAdapter(BaseSourceAdapter):
    kind = "kwork"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.rss_url = str(self.config.params.get("rss_url") or "https://kwork.ru/rss")
        self.listing_url = str(self.config.params.get("listing_url") or "https://kwork.ru/projects?c=41")
        self.fetch_details_enabled = bool(self.config.params.get("fetch_details", False))
        self.category_filter = str(self.config.params.get("category_filter") or "").strip().lower()

    async def fetch_listing(self) -> list[UnifiedJob]:
        jobs: list[UnifiedJob] = []
        try:
            result = await self.fetch_page(self.rss_url)
            jobs = self.parse_rss(result.raw if result.raw else result.text, base_url=result.url)
        except SourceError as exc:
            logger.debug(f"kwork: RSS недоступен ({exc}), пробую HTML-ленту")
        if not jobs:
            jobs = await self._fetch_html_listing()
        if not jobs:
            raise SourceParseError(
                "Kwork не вернул заказов ни через RSS, ни через HTML (возможно, требуется вход)",
                source=self.name,
            )
        return await self.finalize(jobs)

    # --------------------------------------------------------------------- RSS
    def parse_rss(self, payload: str | bytes, *, base_url: str = "") -> list[UnifiedJob]:
        parsed = feedparser.parse(payload)
        jobs: list[UnifiedJob] = []
        for entry in parsed.entries[: self.config.max_items]:
            title = str(entry.get("title") or "").strip()
            link = str(entry.get("link") or "").strip()
            if not title or not link:
                continue
            link = urljoin(base_url or "https://kwork.ru", link)
            description = self.clean_html(str(entry.get("summary") or entry.get("description") or ""))
            budget_min, budget_max, currency = extract_budget(f"{title} {description}", default_currency="RUB")
            tags = [str(tag.get("term", "")) for tag in entry.get("tags", []) if isinstance(tag, dict)]
            category = self.config.category or (tags[0].replace("_", " ") if tags else None)
            if self.category_filter and tags:
                haystack = " ".join(tags).lower()
                if self.category_filter not in haystack:
                    continue
            job = self.build_job(
                external_id=self._external_id(link),
                url=link,
                title=title,
                description=description,
                budget_min=budget_min,
                budget_max=budget_max,
                currency=currency,
                proposals_count=None,  # RSS does not expose the proposal counter
                status="open",
                published_at=self.parse_datetime(entry.get("published_parsed") or entry.get("published")),
                skills=tags,
                category=category,
                extra={"feed": "rss"},
            )
            jobs.append(job)
        return jobs

    # -------------------------------------------------------------------- HTML
    async def _fetch_html_listing(self) -> list[UnifiedJob]:
        try:
            soup, final_url = await self.get_soup(self.listing_url)
        except SourceError:
            raise
        jobs: list[UnifiedJob] = []
        seen: set[str] = set()
        cards = soup.select("div.wants-card, article.wants-card, section.wants-card")
        if not cards:
            cards = soup.select("div[class*='wants-card']")
        for card in cards[: self.config.max_items]:
            job = self._parse_card(card, final_url)
            if job is None or job.external_id in seen:
                continue
            seen.add(job.external_id)
            jobs.append(job)
        return jobs

    def _parse_card(self, card, base_url: str) -> UnifiedJob | None:
        title_node = card.select_one(
            ".wants-card__header-title a, a.wants-card__header-title, .wants-card__title a, h2 a"
        )
        if title_node is None:
            return None
        title = title_node.get_text(" ", strip=True)
        href = title_node.get("href") or ""
        if not title or not href:
            return None
        url = urljoin(base_url, href)
        description = self.clean_html(
            (card.select_one(".wants-card__description-text, .wants-card__description") or card).get_text(" ", strip=True)
        )
        price_text = self.clean_html(
            (card.select_one(".wants-card__price, [class*='price']") or card).get_text(" ", strip=True)
        )
        budget_min, budget_max, currency = extract_budget(price_text, default_currency="RUB")
        if budget_min is None:
            budget_min, budget_max, currency = extract_budget(title, default_currency="RUB")
        proposals = self.extract_proposals(
            (card.select_one("[class*='proposals'], [class*='response']") or card).get_text(" ", strip=True)
        )
        date_text = self.clean_html(
            (card.select_one(".wants-card__header-date, [class*='date']") or card).get_text(" ", strip=True)
        )
        return self.build_job(
            external_id=self._external_id(url),
            url=url,
            title=title,
            description=description,
            budget_min=budget_min,
            budget_max=budget_max,
            currency=currency,
            proposals_count=proposals,
            status=self.extract_status(card.get_text(" ", strip=True)),
            published_at=self.parse_datetime(date_text),
            extra={"feed": "html"},
        )

    # ------------------------------------------------------------------ details
    async def fetch_details(self, job: UnifiedJob) -> UnifiedJob:
        if not self.fetch_details_enabled or not job.url:
            return job
        try:
            soup, _ = await self.get_soup(job.url)
        except SourceError as exc:
            logger.debug(f"kwork: детальная страница недоступна ({exc})")
            return job
        description_node = soup.select_one(
            ".wants-card__description-text, [class*='description-text'], .wants-card__description"
        )
        if description_node is not None:
            text = self.clean_html(description_node.get_text(" ", strip=True))
            if len(text) > len(job.description):
                job.description = text[:12000]
        page_text = soup.get_text(" ", strip=True)
        proposals = self.extract_proposals(page_text)
        if proposals is not None:
            job.proposals_count = proposals
        job.status = self.extract_status(page_text, default=job.status)  # type: ignore[assignment]
        price_text = self.clean_html(
            (soup.select_one(".wants-card__price, [class*='price']") or soup).get_text(" ", strip=True)
        )
        low, high, currency = extract_budget(price_text, default_currency=job.currency)
        if low is not None:
            job.budget_min, job.budget_max, job.currency = low, high, currency
            job.budget_type = "fixed"
            await self.converter.enrich(job)
        rating_node = soup.select_one("[class*='rating'], [class*='client-info']")
        if rating_node is not None:
            match = re.search(r"\d+[.,]\d+", rating_node.get_text(" ", strip=True))
            if match:
                job.client_rating = float(match.group().replace(",", "."))
        return job

    def needs_details(self, job: UnifiedJob) -> bool:
        return self.fetch_details_enabled and bool(job.url)

    @staticmethod
    def _external_id(url: str) -> str:
        match = PROJECT_ID_RE.search(url)
        if match:
            return match.group(1)
        return url.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or url
