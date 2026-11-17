"""Workspace.ru tenders adapter.

The public tender catalogue sometimes answers with a JS/anti-bot "Проверка
безопасности" page. That is reported as SourceBlocked (no bypass attempts); when
the catalogue is served, tender cards are parsed.
"""

from __future__ import annotations

import re

from app.models import UnifiedJob
from app.sources.base import (
    BaseSourceAdapter,
    SourceBlocked,
    SourceParseError,
    extract_budget,
    parse_amount,
)

BLOCK_MARKERS = ("проверка безопасности", "js-challenge", "captcha", "access denied")


class WorkspaceAdapter(BaseSourceAdapter):
    kind = "workspace"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.listing_url = str(self.config.params.get("url") or "https://workspace.ru/tenders/")

    async def fetch_listing(self) -> list[UnifiedJob]:
        result = await self.fetch_page(self.listing_url)
        probe = result.text.lower()
        if any(marker in probe for marker in BLOCK_MARKERS):
            raise SourceBlocked(
                "Workspace закрыл публичный каталог антибот-проверкой (JS). Обход не поддерживается.",
                source=self.name,
            )
        soup, final_url = await self.get_soup(self.listing_url)
        titles = soup.select("a.b-tender__title, a[href*='/tenders/'][class*='title']")
        jobs: list[UnifiedJob] = []
        for title_node in titles[: self.config.max_items]:
            href = title_node.get("href") or ""
            title = title_node.get_text(" ", strip=True)
            if not href or not title:
                continue
            container = title_node.find_parent(class_=re.compile("card|tender")) or title_node.parent
            text = container.get_text(" ", strip=True) if container is not None else title
            url = href if href.startswith("http") else f"https://workspace.ru{href}"
            budget_min, budget_max, currency = self._extract_money(text)
            proposals = self.extract_proposals(text)
            jobs.append(
                self.build_job(
                    external_id=str(title_node.get("data-id") or url.rstrip("/").rsplit("/", 2)[-2:] or url),
                    url=url,
                    title=title,
                    description=text[:4000],
                    budget_min=budget_min,
                    budget_max=budget_max,
                    currency=currency,
                    proposals_count=proposals,
                    status="open",
                    extra={"feed": "html"},
                )
            )
        if not jobs:
            raise SourceParseError("Workspace: тендеры не найдены - разметка изменилась", source=self.name)
        return await self.finalize(jobs)

    def _extract_money(self, text: str) -> tuple[float | None, float | None, str]:
        low, high, currency = extract_budget(text, default_currency="RUB")
        if low is not None:
            return low, high, currency
        match = re.search(r"(?i)(?:нмцк|стоимость|бюджет|цена)[^\d]{0,25}([\d\s.,]{4,20})", text)
        if match:
            amount = parse_amount(match.group(1))
            if amount:
                return amount, amount, "RUB"
        return None, None, "RUB"
