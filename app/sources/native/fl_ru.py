"""FL.ru adapter: HTML project feed (with exact bid count) + RSS fallback."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from loguru import logger

from app.config import SourceConfig
from app.core.currency import CurrencyConverter
from app.models import UnifiedJob
from app.sources.base import SourceError, SourceParseError, extract_budget
from app.sources.generic_rss import GenericRSSAdapter
from app.sources.http_client import HttpClient

PROJECT_ID_RE = re.compile(r"/projects/(\d+)")


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
        self.listing_url = str(config.params.get("listing_url") or "https://www.fl.ru/projects/").strip()
        self.feed_url = str(
            config.params.get("rss_url") or config.params.get("url") or "https://www.fl.ru/rss/all.xml"
        ).strip()
        self.fetch_details_enabled = bool(config.params.get("fetch_details", True))

    async def fetch_listing(self) -> list[UnifiedJob]:
        if self.listing_url:
            try:
                jobs = await self._fetch_html_listing()
                if jobs:
                    return await self.finalize(jobs)
            except SourceError as exc:
                logger.debug(f"fl_ru: HTML-лента недоступна ({exc}), пробую RSS")
        return await super().fetch_listing()

    async def _fetch_html_listing(self) -> list[UnifiedJob]:
        soup, final_url = await self.get_soup(self.listing_url)
        cards = soup.select("[id^='project-item'], .b-post")
        if not cards:
            raise SourceParseError("FL.ru: карточки project-item не найдены", source=self.name)
        jobs: list[UnifiedJob] = []
        seen: set[str] = set()
        for card in cards[: self.config.max_items]:
            job = self._parse_html_card(card, final_url)
            if job is None or job.external_id in seen:
                continue
            seen.add(job.external_id)
            jobs.append(job)
        if not jobs:
            raise SourceParseError("FL.ru: карточки найдены, но не разобраны", source=self.name)
        return jobs

    def _parse_html_card(self, card, base_url: str) -> UnifiedJob | None:
        title_link = card.select_one("h2 a, a[id^='prj_name_'], a[href*='/projects/']")
        if title_link is None:
            return None
        title = title_link.get_text(" ", strip=True)
        href = title_link.get("href") or ""
        if not title or not href:
            return None
        url = urljoin(base_url, href)

        desc_node = card.select_one(".b-post__txt, .b-post__body")
        description = self.clean_html(desc_node.get_text(" ", strip=True) if desc_node else "")

        price_node = card.select_one(".b-post__price")
        price_text = price_node.get_text(" ", strip=True) if price_node else ""
        budget_min, budget_max, currency = extract_budget(price_text, default_currency="RUB")
        if budget_min is None and "по результатам" not in price_text.lower():
            budget_min, budget_max, currency = extract_budget(f"{title} {description}", default_currency="RUB")

        foot = card.select_one(".b-post__foot")
        foot_text = foot.get_text(" ", strip=True) if foot else ""
        proposals = self.extract_proposals(foot_text)
        if proposals is None:
            vc_node = card.select_one("[data-id='fl-view-count-href']")
            if vc_node is not None:
                proposals = self.extract_proposals(vc_node.get_text(" ", strip=True))

        time_node = card.select_one(".b-post__foot .text-gray-opacity-4")
        published_at = self.parse_datetime(
            time_node.get_text(" ", strip=True) if time_node else None,
            tz_name="Europe/Moscow",
        )

        status_node = card.select_one("[data-id='qa-order-status']")
        status_text = status_node.get_text(" ", strip=True) if status_node else ""
        status = self.extract_status(status_text, default="open")

        kind_label = ""
        kind_node = card.select_one(".b-post__foot .b-post__bold")
        if kind_node is not None:
            kind_label = kind_node.get_text(" ", strip=True)

        return self.build_job(
            external_id=self._external_id_from_url(url, card.get("id")),
            url=url,
            title=title,
            description=description,
            budget_min=budget_min,
            budget_max=budget_max,
            currency=currency,
            proposals_count=proposals,
            status=status,
            published_at=published_at,
            extra={"feed": "html", "post_kind": kind_label},
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

    async def fetch_details(self, job: UnifiedJob) -> UnifiedJob:
        if not job.url:
            return job
        try:
            soup, _ = await self.get_soup(job.url)
        except SourceError as exc:
            logger.debug(f"fl_ru: детальная страница недоступна ({exc})")
            return job

        body = soup.select_one(".fl-project-content__description-text, [id^='projectp'], .b-layout__txt")
        if body is not None:
            text = self.clean_html(body.get_text(" ", strip=True))
            if len(text) > len(job.description):
                job.description = text[:12000]

        page_text = soup.get_text(" ", strip=True)
        lowered = page_text.lower()
        if any(marker in lowered for marker in ("исполнитель выбран", "проект закрыт", "снят с публикации", "заказ завершён")):
            job.status = "closed"

        proposals = self.extract_proposals(page_text)
        if proposals is not None:
            job.proposals_count = proposals
        return job

    def needs_details(self, job: UnifiedJob) -> bool:
        if not self.fetch_details_enabled or not job.url:
            return False
        return not job.description.strip()

    @staticmethod
    def _external_id_from_url(url: str, card_id: Any = None) -> str:
        match = PROJECT_ID_RE.search(url)
        if match:
            return match.group(1)
        if card_id:
            digits = "".join(ch for ch in str(card_id) if ch.isdigit())
            if digits:
                return digits
        return url.rstrip("/").rsplit("/", 1)[-1]
