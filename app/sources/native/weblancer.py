"""Weblancer adapter.

Weblancer renders its catalogue via Next.js App Router SSR (`self.__next_f.push(...)`).
The full JSON array of jobs, exact proposal counts (`total_bids_count`), USD budgets,
timestamps (`bidding_time`), client stats and even full description chunks are
embedded right inside the HTTP 200 HTML response — no headless browser required.

If the Next.js RSC stream format changes, it cleanly falls back to GenericHTMLAdapter
(with optional `use_browser: true`).
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from loguru import logger

from app.models import UnifiedJob
from app.sources.base import SourceParseError
from app.sources.generic_html import GenericHTMLAdapter

NEXT_F_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.S)
RSC_TEXT_CHUNK_RE = re.compile(r"(?:^|\n)([0-9a-f]+):T[0-9a-f]+,(.*)$", re.M)


class WeblancerAdapter(GenericHTMLAdapter):
    kind = "weblancer"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if not self.listing_url:
            self.listing_url = "https://www.weblancer.net/freelance/"
        selectors = self.config.selectors
        if not selectors.item:
            selectors.item = "article, div[class*='job-card'], div[class*='job-item'], li[class*='job']"
        if not selectors.title:
            selectors.title = "a[class*='title'], h2 a, h3 a"
        if not selectors.url:
            selectors.url = "a[class*='title']@href, h2 a@href, h3 a@href"
        if not selectors.description:
            selectors.description = "[class*='desc'], p"
        if not selectors.price:
            selectors.price = "[class*='price'], [class*='budget'], [class*='amount']"
        if not selectors.proposals:
            selectors.proposals = "[class*='response'], [class*='proposal'], [class*='bid']"
        if not selectors.published_at:
            selectors.published_at = "time@datetime, [class*='date'], [class*='time']"
        if not selectors.status:
            selectors.status = "[class*='status'], [class*='badge']"
        self.selectors = selectors

    async def fetch_listing(self) -> list[UnifiedJob]:
        if not self.listing_url:
            raise SourceParseError("Не задан url страницы Weblancer", source=self.name)

        result = await self.fetch_page(self.listing_url)
        html_text = result.text
        rsc_jobs = self.parse_rsc_html(html_text, base_url=result.url or "https://www.weblancer.net")
        if rsc_jobs:
            return await self.finalize(rsc_jobs)

        # Fallback to CSS selector parsing if RSC stream wasn't present (e.g. HTML fixture / browser)
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(result.raw if result.raw else html_text, "lxml")
        nodes = soup.select(self.selectors.item)
        jobs: list[UnifiedJob] = []
        for node in nodes[: self.config.max_items]:
            job = self.parse_node(node, result.url or self.listing_url)
            if job is not None:
                jobs.append(job)
        if jobs:
            return await self.finalize(jobs)

        raise SourceParseError(
            "Weblancer: не удалось извлечь заказы ни из Next.js RSC потока, ни через CSS-селекторы",
            source=self.name,
        )

    def parse_rsc_html(self, html_text: str, *, base_url: str = "https://www.weblancer.net") -> list[UnifiedJob]:
        raw_chunks = NEXT_F_PUSH_RE.findall(html_text)
        if not raw_chunks:
            return []
        decoded_parts: list[str] = []
        for chunk in raw_chunks:
            try:
                decoded_parts.append(json.loads(f'"{chunk}"'))
            except json.JSONDecodeError:
                continue
        stream = "".join(decoded_parts)
        if not stream:
            return []

        text_refs: dict[str, str] = {}
        for match in RSC_TEXT_CHUNK_RE.finditer(stream):
            text_refs[match.group(1)] = match.group(2)

        marker = '"jobs":['
        idx = stream.find(marker)
        if idx == -1:
            return []
        decoder = json.JSONDecoder()
        try:
            items, _ = decoder.raw_decode(stream, idx + len('"jobs":'))
        except json.JSONDecodeError as exc:
            logger.debug(f"weblancer: ошибка декодирования jobs из RSC: {exc}")
            return []
        if not isinstance(items, list):
            return []

        jobs: list[UnifiedJob] = []
        for item in items[: self.config.max_items]:
            if not isinstance(item, dict):
                continue
            job = self._parse_rsc_item(item, text_refs=text_refs, base_url=base_url)
            if job is not None:
                jobs.append(job)
        return jobs

    def _parse_rsc_item(
        self,
        item: dict[str, Any],
        *,
        text_refs: dict[str, str],
        base_url: str,
    ) -> UnifiedJob | None:
        if item.get("hidden") or item.get("blocked"):
            return None
        job_id = item.get("id")
        title = str(item.get("localizedName") or item.get("name") or "").strip()
        rel_url = str(item.get("url") or "").strip()
        if not job_id or not title or not rel_url:
            return None
        url = urljoin(base_url, rel_url)

        raw_desc = str(item.get("description") or "").strip()
        if raw_desc.startswith("$") and raw_desc[1:] in text_refs:
            raw_desc = text_refs[raw_desc[1:]]
        elif raw_desc.startswith("$"):
            raw_desc = ""
        snippet = str(item.get("localizedSnippet") or item.get("seo_snippet") or item.get("snippet") or "")
        description = self.clean_html(raw_desc or snippet)

        raw_budget = item.get("budget")
        budget_val: float | None = None
        if isinstance(raw_budget, (int, float)) and raw_budget > 0:
            budget_val = float(raw_budget)

        bids_raw = item.get("total_bids_count")
        proposals = int(bids_raw) if isinstance(bids_raw, (int, float)) else None

        raw_status = str(item.get("status") or "").strip().lower()
        if item.get("closed_time") or raw_status in {"closed", "finished", "completed", "cancelled"}:
            status = "closed"
        elif raw_status in {"in_progress", "assigned"}:
            status = "in_progress"
        elif raw_status in {"opened", "open", "active"}:
            status = "open"
        else:
            status = "unknown"

        published_at = self.parse_datetime(item.get("bidding_time"))

        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        client_verified = bool(user.get("verified")) if user.get("verified") is not None else None
        client_hire_rate = None
        client_reviews = None
        stats_raw = user.get("customer_stats")
        if isinstance(stats_raw, str) and stats_raw.strip().startswith("{"):
            try:
                stats = json.loads(stats_raw)
                pct = stats.get("hired_jobs_percent")
                if isinstance(pct, (int, float)):
                    client_hire_rate = max(0.0, min(1.0, float(pct) / 100.0))
                rev = stats.get("total_reviews_count")
                if isinstance(rev, (int, float)):
                    client_reviews = int(rev)
            except json.JSONDecodeError:
                pass

        category_obj = item.get("category") if isinstance(item.get("category"), dict) else {}
        category = str(category_obj.get("name") or self.config.category or "") or None
        skills = [
            str(tag["name"]).strip()
            for tag in (item.get("tagList") or [])
            if isinstance(tag, dict) and tag.get("name")
        ]

        return self.build_job(
            external_id=str(job_id),
            url=url,
            title=title,
            description=description,
            budget_min=budget_val,
            budget_max=budget_val,
            currency="USD",  # Verified live: Weblancer renders budget numbers in $
            budget_type="fixed" if budget_val is not None else "negotiable",
            proposals_count=proposals,
            status=status,
            published_at=published_at,
            client_reviews=client_reviews,
            client_hire_rate=client_hire_rate,
            client_verified=client_verified,
            skills=skills,
            category=category,
            extra={
                "feed": "rsc",
                "pinned": bool(item.get("isPinned")),
                "views": item.get("views_count"),
            },
        )
