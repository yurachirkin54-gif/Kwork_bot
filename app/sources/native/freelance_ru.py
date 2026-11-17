"""Freelance.ru adapter.

The site has no public RSS anymore; the project catalogue is a server-rendered
HTML list of `article.task-card` blocks (verified against live markup).
"""

from __future__ import annotations

import re

from loguru import logger

from app.models import UnifiedJob
from app.sources.base import BaseSourceAdapter, SourceError, SourceParseError, extract_budget, extract_int

TASK_ID_RE = re.compile(r"/task/view/(\d+)")


class FreelanceRuAdapter(BaseSourceAdapter):
    kind = "freelance_ru"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.listing_url = str(self.config.params.get("url") or "https://freelance.ru/projects/")
        self.fetch_details_enabled = bool(self.config.params.get("fetch_details", False))

    async def fetch_listing(self) -> list[UnifiedJob]:
        soup, final_url = await self.get_soup(self.listing_url)
        cards = soup.select("article.task-card, div.task-card")
        if not cards:
            raise SourceParseError(
                "Не найдены карточки task-card - вероятно, изменилась разметка сайта", source=self.name
            )
        jobs: list[UnifiedJob] = []
        for card in cards[: self.config.max_items]:
            job = self.parse_card(card, final_url)
            if job is not None:
                jobs.append(job)
        if not jobs:
            raise SourceParseError("Карточки найдены, но заголовки не распознаны", source=self.name)
        return await self.finalize(jobs)

    def parse_card(self, card, base_url: str) -> UnifiedJob | None:
        title_link = card.select_one("a.task-card__title-link")
        if title_link is None:
            return None
        title = title_link.get_text(" ", strip=True) or str(title_link.get("title") or "").strip()
        href = title_link.get("href") or ""
        if not title or not href:
            return None
        url = href if href.startswith("http") else f"https://freelance.ru{href}"

        description = self.clean_html(
            (card.select_one("p.task-card__desc, .task-card__desc") or card).get_text(" ", strip=True)
        )
        price_text = self.clean_html(
            (card.select_one(".task-card__budget") or card).get_text(" ", strip=True)
        )
        budget_min, budget_max, currency = extract_budget(price_text, default_currency="RUB")
        if budget_min is None:
            budget_min, budget_max, currency = extract_budget(f"{title} {description}", default_currency="RUB")

        proposals = self._proposals_from_foot(card)
        published = self._published_from_foot(card)

        category_node = card.select_one(".task-chip--cat, .task-card__chips .task-chip")
        category = category_node.get_text(" ", strip=True) if category_node else None

        skills = [chip.get_text(" ", strip=True) for chip in card.select(".task-chip") if chip.get_text(strip=True)]

        return self.build_job(
            external_id=self._external_id(url),
            url=url,
            title=title,
            description=description,
            budget_min=budget_min,
            budget_max=budget_max,
            currency=currency,
            proposals_count=proposals,
            status="open",
            published_at=published,
            skills=skills,
            category=category,
            extra={"feed": "html"},
        )

    def _proposals_from_foot(self, card) -> int | None:
        for item in card.select(".task-card__foot-item"):
            icon = item.find("i")
            classes = " ".join(icon.get("class", [])) if icon else ""
            if "comments" in classes or "comment" in classes:
                strong = item.find("strong")
                value = extract_int(strong.get_text(" ", strip=True) if strong else item.get_text(" ", strip=True))
                if value is not None:
                    return value
        return None

    def _published_from_foot(self, card):
        for item in card.select(".task-card__foot-item[title]"):
            parsed = self.parse_datetime(item.get("title"))
            if parsed is not None:
                return parsed
        return self.parse_datetime(card.select_one(".task-card__foot-item").get_text(" ", strip=True) if card.select_one(".task-card__foot-item") else None)

    async def fetch_details(self, job: UnifiedJob) -> UnifiedJob:
        if not self.fetch_details_enabled or not job.url:
            return job
        try:
            soup, _ = await self.get_soup(job.url)
        except SourceError as exc:
            logger.debug(f"freelance.ru: детальная страница недоступна ({exc})")
            return job
        body = soup.select_one(".task-view__description, .task__description, article")
        if body is not None:
            text = self.clean_html(body.get_text(" ", strip=True))
            if len(text) > len(job.description):
                job.description = text[:12000]
        job.proposals_count = self.extract_proposals(soup.get_text(" ", strip=True)) or job.proposals_count
        return job

    def needs_details(self, job: UnifiedJob) -> bool:
        return self.fetch_details_enabled and bool(job.url)

    @staticmethod
    def _external_id(url: str) -> str:
        match = TASK_ID_RE.search(url)
        return match.group(1) if match else url.rstrip("/").rsplit("/", 1)[-1]
