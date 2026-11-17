"""Workspace.ru tenders adapter (official RSS primary + HTML catalogue fallback)."""

from __future__ import annotations

import re
from urllib.parse import urljoin

import feedparser
from bs4 import BeautifulSoup
from loguru import logger

from app.models import UnifiedJob
from app.sources.base import (
    BaseSourceAdapter,
    SourceBlocked,
    SourceError,
    SourceParseError,
    extract_budget,
    parse_amount,
)

BLOCK_MARKERS = ("проверка безопасности", "js-challenge", "captcha", "access denied")
TENDER_ID_RE = re.compile(r"-(\d{3,})/?(?:\?|$)")


class WorkspaceAdapter(BaseSourceAdapter):
    kind = "workspace"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.rss_url = str(self.config.params.get("rss_url") or "https://workspace.ru/tenders/rss/").strip()
        self.listing_url = str(self.config.params.get("url") or "https://workspace.ru/tenders/").strip()

    async def fetch_listing(self) -> list[UnifiedJob]:
        errors: list[SourceError] = []
        if self.rss_url:
            try:
                jobs = await self._fetch_rss()
                if jobs:
                    return await self.finalize(jobs)
            except SourceBlocked:
                raise
            except SourceError as exc:
                errors.append(exc)
                logger.debug(f"workspace: RSS недоступен ({exc}), пробую HTML-каталог")

        try:
            jobs = await self._fetch_html()
            if jobs:
                return await self.finalize(jobs)
        except SourceError as exc:
            errors.append(exc)

        if errors:
            raise errors[-1]
        raise SourceParseError("Workspace: тендеры не найдены", source=self.name)

    async def _fetch_rss(self) -> list[UnifiedJob]:
        result = await self.fetch_page(self.rss_url)
        probe = result.text.lower()
        if any(marker in probe for marker in BLOCK_MARKERS):
            raise SourceBlocked(
                "Workspace закрыл публичную ленту антибот-проверкой (JS). Обход не поддерживается.",
                source=self.name,
            )
        parsed = feedparser.parse(result.raw if result.raw else result.text)
        if not parsed.entries:
            raise SourceParseError("Workspace RSS пуст", source=self.name)
        jobs: list[UnifiedJob] = []
        for entry in parsed.entries[: self.config.max_items]:
            title = str(entry.get("title") or "").strip().rstrip(".")
            link = str(entry.get("link") or "").strip()
            if not title or not link:
                continue
            summary = self.clean_html(str(entry.get("summary") or ""))
            budget_min, budget_max, currency = self._extract_money(f"{title} {summary}")
            published = self.parse_datetime(entry.get("published") or entry.get("published_parsed"))
            jobs.append(
                self.build_job(
                    external_id=self._external_id(link),
                    url=link,
                    title=title,
                    description=summary,
                    budget_min=budget_min,
                    budget_max=budget_max,
                    currency=currency,
                    proposals_count=self.extract_proposals(summary),
                    status="open",
                    published_at=published,
                    extra={"feed": "rss"},
                )
            )
        return jobs

    async def _fetch_html(self) -> list[UnifiedJob]:
        result = await self.fetch_page(self.listing_url)
        probe = result.text.lower()
        if any(marker in probe for marker in BLOCK_MARKERS):
            raise SourceBlocked(
                "Workspace закрыл публичный каталог антибот-проверкой (JS). Обход не поддерживается.",
                source=self.name,
            )
        soup = BeautifulSoup(result.raw if result.raw else result.text, "lxml")
        cards = soup.select(".vacancies__card._tender, div[data-tender-card]")
        jobs: list[UnifiedJob] = []
        if cards:
            for card in cards[: self.config.max_items]:
                title_node = card.select_one(".b-tender__title a, a[href*='/tenders/']")
                if title_node is None:
                    continue
                href = title_node.get("href") or ""
                title = title_node.get_text(" ", strip=True)
                if not href or not title:
                    continue
                url = urljoin("https://workspace.ru", href)
                text = self.clean_html(card.get_text(" ", strip=True))
                budget_min, budget_max, currency = self._extract_money(text)
                status_node = card.select_one(".tendercart-type__status-public")
                status = self.extract_status(status_node.get_text(" ", strip=True) if status_node else text)
                pub_text = None
                for item in card.select(".b-tender__info-item"):
                    lbl = item.select_one(".b-tender__info-item-title")
                    val = item.select_one(".b-tender__info-item-text")
                    if lbl and val and "опубликован" in lbl.get_text(strip=True).lower():
                        pub_text = val.get_text(" ", strip=True)
                        break
                jobs.append(
                    self.build_job(
                        external_id=str(title_node.get("data-analytics-id") or self._external_id(url)),
                        url=url,
                        title=title,
                        description=text[:4000],
                        budget_min=budget_min,
                        budget_max=budget_max,
                        currency=currency,
                        proposals_count=self.extract_proposals(text),
                        status=status,
                        published_at=self.parse_datetime(pub_text, tz_name="Europe/Moscow"),
                        extra={"feed": "html"},
                    )
                )
        else:
            titles = soup.select("a.b-tender__title, a[href*='/tenders/'][class*='title']")
            for title_node in titles[: self.config.max_items]:
                href = title_node.get("href") or ""
                title = title_node.get_text(" ", strip=True)
                if not href or not title:
                    continue
                container = title_node.find_parent(class_=re.compile("card|tender")) or title_node.parent
                text = container.get_text(" ", strip=True) if container is not None else title
                url = urljoin("https://workspace.ru", href)
                budget_min, budget_max, currency = self._extract_money(text)
                jobs.append(
                    self.build_job(
                        external_id=str(title_node.get("data-id") or self._external_id(url)),
                        url=url,
                        title=title,
                        description=text[:4000],
                        budget_min=budget_min,
                        budget_max=budget_max,
                        currency=currency,
                        proposals_count=self.extract_proposals(text),
                        status="open",
                        extra={"feed": "html"},
                    )
                )
        if not jobs:
            raise SourceParseError("Workspace: тендеры не найдены - разметка изменилась", source=self.name)
        return jobs

    @staticmethod
    def _external_id(url: str) -> str:
        match = TENDER_ID_RE.search(url)
        if match:
            return match.group(1)
        return url.rstrip("/").rsplit("/", 1)[-1]

    def _extract_money(self, text: str) -> tuple[float | None, float | None, str]:
        range_match = re.search(
            r"(?i)(?:нмцк|стоимость|бюджет|цена)[^\d]{0,25}([\d\s\u00a0.,]{3,20})\s*(?:[-–—]|до)\s*([\d\s\u00a0.,]{3,20})\s*(руб|₽|rub|usd|\$|eur|€)?",
            text,
        )
        if range_match:
            a1 = parse_amount(range_match.group(1))
            a2 = parse_amount(range_match.group(2))
            if a1 and a2:
                cur = "USD" if (range_match.group(3) or "").lower() in {"usd", "$"} else "EUR" if (range_match.group(3) or "").lower() in {"eur", "€"} else "RUB"
                return min(a1, a2), max(a1, a2), cur
        low, high, currency = extract_budget(text, default_currency="RUB")
        if low is not None:
            return low, high, currency
        match = re.search(
            r"(?i)(?:нмцк|стоимость|бюджет|цена)[^\d]{0,25}([\d\s.,]{4,20})",
            text,
        )
        if match:
            a1 = parse_amount(match.group(1))
            if a1:
                return a1, a1, "RUB"
        return None, None, "RUB"
