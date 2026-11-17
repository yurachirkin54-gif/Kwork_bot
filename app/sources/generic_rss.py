"""Universal RSS/Atom adapter driven entirely by sources.yaml."""

from __future__ import annotations

import re
from urllib.parse import urljoin

import feedparser
from loguru import logger

from app.models import UnifiedJob
from app.sources.base import BaseSourceAdapter, SourceParseError, extract_budget, extract_int


class GenericRSSAdapter(BaseSourceAdapter):
    """Any RSS/Atom feed can be monitored without writing Python code."""

    kind = "rss"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.feed_url = str(self.config.params.get("url") or self.config.url or "").strip()
        self.budget_pattern = self.config.regex.budget
        self.proposals_pattern = self.config.regex.proposals
        self.default_status = str(self.config.params.get("default_status", "open"))

    async def fetch_listing(self) -> list[UnifiedJob]:
        if not self.feed_url:
            raise SourceParseError("Не задан url RSS-ленты", source=self.name)
        result = await self.fetch_page(self.feed_url)
        jobs = self.parse_feed(result.raw if result.raw else result.text, base_url=result.url)
        if not jobs:
            raise SourceParseError("RSS-лента не содержит заказов", source=self.name)
        return await self.finalize(jobs)

    def parse_feed(self, text: str | bytes, *, base_url: str = "") -> list[UnifiedJob]:
        # bytes keep the XML encoding declaration intact (FL.ru serves cp1251)
        parsed = feedparser.parse(text)
        entries = getattr(parsed, "entries", [])
        if not entries:
            if getattr(parsed, "bozo", 0):
                raise SourceParseError("Не удалось разобрать RSS/Atom", source=self.name)
            return []
        jobs: list[UnifiedJob] = []
        for entry in entries[: self.config.max_items]:
            job = self.parse_entry(entry, base_url=base_url)
            if job is not None:
                jobs.append(job)
        return jobs

    def parse_entry(self, entry, *, base_url: str = "") -> UnifiedJob | None:
        link = str(entry.get("link") or entry.get("id") or "").strip()
        title = str(entry.get("title") or "").strip()
        if not title or not link:
            return None
        link = urljoin(base_url or self.feed_url, link)
        summary = str(entry.get("summary") or "")
        content_list = entry.get("content") or []
        if content_list and isinstance(content_list, list):
            summary = f"{summary} {content_list[0].get('value', '')}".strip()
        description = self.clean_html(summary)
        budget_min, budget_max, currency = extract_budget(
            f"{title} {description}", default_currency=self.currency
        )
        if self.budget_pattern:
            match = re.search(self.budget_pattern, f"{title} {description}", re.IGNORECASE)
            if match:
                raw = next((g for g in match.groups() if g), None)
                from app.sources.base import parse_amount

                amount = parse_amount(raw)
                if amount:
                    budget_min = amount
                    budget_max = amount
        proposals = self.extract_proposals(description, self.proposals_pattern)
        published = self.parse_datetime(
            entry.get("published") or entry.get("updated") or entry.get("published_parsed")
        )
        external_id = self._external_id(entry, link)
        skills = [tag.get("term", "") for tag in entry.get("tags", []) if isinstance(tag, dict)]
        return self.build_job(
            external_id=external_id,
            url=link,
            title=title,
            description=description,
            budget_min=budget_min,
            budget_max=budget_max,
            currency=currency,
            proposals_count=proposals,
            status=self.extract_status(f"{title} {description}", default=self.default_status),  # type: ignore[arg-type]
            published_at=published,
            skills=skills,
            extra={"feed": self.feed_url},
        )

    def _external_id(self, entry, link: str) -> str:
        raw = str(entry.get("id") or entry.get("guid") or link).strip()
        if raw.startswith("http"):
            slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw).strip("-")
            return slug[-120:] or str(abs(hash(raw)))
        return raw[:160]

    async def fetch_details(self, job: UnifiedJob) -> UnifiedJob:
        """RSS entries usually carry the full text; detail fetch is opt-in only."""

        if not self.config.params.get("fetch_details") or not job.url:
            return job
        try:
            soup, _ = await self.get_soup(job.url)
        except Exception as exc:
            logger.debug(f"{self.name}: не удалось догрузить карточку {job.url}: {exc}")
            return job
        body = soup.select_one("article, .project, .task, .content, main")
        if body is not None:
            text = self.clean_html(str(body))
            if len(text) > len(job.description):
                job.description = text[:12000]
        proposals = self.extract_proposals(soup.get_text(" ", strip=True), self.proposals_pattern)
        if proposals is not None:
            job.proposals_count = proposals
        return job

    def needs_details(self, job: UnifiedJob) -> bool:
        return bool(self.config.params.get("fetch_details")) and bool(job.url)
