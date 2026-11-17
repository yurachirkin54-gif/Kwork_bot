"""Freelancehunt adapter.

Primary transport: official API v2 (Bearer token from https://freelancehunt.com/my/api).
Fallback: the public RSS feed https://freelancehunt.com/projects.rss (verified live).
Cloudflare may answer 403 on some datacenter IPs - the circuit breaker handles it.
"""

from __future__ import annotations

import json
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

PROJECT_ID_RE = re.compile(r"/(\d{4,})(?:\.html|/)?(?:\?|$)")


class FreelanceHuntAdapter(BaseSourceAdapter):
    kind = "freelancehunt"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.api_url = str(self.config.params.get("api_url") or "https://api.freelancehunt.com/v2/projects")
        self.fallback_rss = str(
            self.config.params.get("fallback_rss") or "https://freelancehunt.com/projects.rss"
        )

    async def fetch_listing(self) -> list[UnifiedJob]:
        token = self.config.token
        errors: list[SourceError] = []
        if token:
            try:
                jobs = await self._fetch_api(token)
                if jobs:
                    return await self.finalize(jobs)
            except SourceError as exc:
                errors.append(exc)
                logger.warning(f"freelancehunt: API недоступно ({exc}), переключаюсь на RSS")
        try:
            jobs = await self._fetch_rss()
            if jobs:
                return await self.finalize(jobs)
        except SourceError as exc:
            errors.append(exc)

        blocked = next((e for e in errors if isinstance(e, SourceBlocked)), None)
        if blocked is not None:
            hint = "" if token else " Добавьте FREELANCEHUNT_TOKEN в .env для официального API."
            raise SourceBlocked(f"{blocked}{hint}", source=self.name, status=blocked.status)
        if errors:
            raise errors[-1]
        raise SourceParseError("Freelancehunt не вернул заказов", source=self.name)

    # --------------------------------------------------------------------- API
    async def _fetch_api(self, token: str) -> list[UnifiedJob]:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        result = await self.fetch_page(self.api_url, headers=headers)
        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise SourceParseError(f"API вернул не JSON: {exc}", source=self.name) from exc
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise SourceParseError("Неожиданный формат ответа API", source=self.name)
        jobs: list[UnifiedJob] = []
        for item in items[: self.config.max_items]:
            if not isinstance(item, dict):
                continue
            attributes = item.get("attributes") or {}
            name = str(attributes.get("name") or "").strip()
            if not name:
                continue
            budget = attributes.get("budget") or {}
            amount = budget.get("amount") if isinstance(budget, dict) else None
            currency = (budget.get("currency") if isinstance(budget, dict) else None) or "UAH"
            description = self.clean_html(str(attributes.get("description") or ""))
            low, high, detected_currency = extract_budget(description, default_currency=str(currency))
            if amount:
                try:
                    low = high = float(amount)
                    detected_currency = str(currency).upper()
                except (TypeError, ValueError):
                    pass
            proposals = (
                attributes.get("bid_count")
                or attributes.get("bids_count")
                or attributes.get("proposals_count")
            )
            published = self.parse_datetime(
                attributes.get("published_at") or attributes.get("created_at")
            )
            url = str(attributes.get("link") or f"https://freelancehunt.com/project/{item.get('id')}")
            jobs.append(
                self.build_job(
                    external_id=str(item.get("id")),
                    url=url,
                    title=name,
                    description=description,
                    budget_min=low,
                    budget_max=high,
                    currency=detected_currency,
                    proposals_count=int(proposals) if isinstance(proposals, (int, float)) else None,
                    status=self.extract_status(str(attributes.get("status") or "open")),
                    published_at=published,
                    skills=[str(s) for s in (attributes.get("skills") or []) if isinstance(s, (str, dict))],
                    extra={"transport": "api"},
                )
            )
        return jobs

    # --------------------------------------------------------------------- RSS
    async def _fetch_rss(self) -> list[UnifiedJob]:
        result = await self.fetch_page(self.fallback_rss)
        parsed = feedparser.parse(result.raw if result.raw else result.text)
        if not parsed.entries:
            raise SourceParseError("RSS Freelancehunt пуст", source=self.name)
        jobs: list[UnifiedJob] = []
        for entry in parsed.entries[: self.config.max_items]:
            title = str(entry.get("title") or "").strip()
            link = str(entry.get("link") or "").strip()
            if not title or not link:
                continue
            description = self.clean_html(str(entry.get("summary") or entry.get("description") or ""))
            low, high, currency = extract_budget(f"{title} {description}", default_currency="UAH")
            proposals = entry.get("slash_comments")
            if proposals is None:
                proposals = self.extract_proposals(description)
            try:
                proposals_int = int(proposals) if proposals is not None else None
            except (TypeError, ValueError):
                proposals_int = None
            tags = [str(tag.get("term", "")) for tag in entry.get("tags", []) if isinstance(tag, dict)]
            jobs.append(
                self.build_job(
                    external_id=self._external_id(link),
                    url=link,
                    title=title,
                    description=description,
                    budget_min=low,
                    budget_max=high,
                    currency=currency,
                    proposals_count=proposals_int,
                    status="open",
                    published_at=self.parse_datetime(entry.get("published_parsed") or entry.get("published")),
                    skills=tags,
                    extra={"transport": "rss"},
                )
            )
        return jobs

    @staticmethod
    def _external_id(url: str) -> str:
        match = PROJECT_ID_RE.search(url)
        if match:
            return match.group(1)
        return url.rstrip("/").rsplit("/", 1)[-1]
