"""YouDo adapter.

YouDo protects the public catalogue with a JS challenge (ServicePipe) and a
captcha frame. Bypassing it is out of scope, so the adapter detects the
challenge and raises SourceBlocked, letting the circuit breaker and the health
alert handle it. When the page is served normally, standard cards are parsed.
"""

from __future__ import annotations

from app.models import UnifiedJob
from app.sources.base import (
    BaseSourceAdapter,
    SourceBlocked,
    SourceError,
    SourceParseError,
    extract_budget,
)

CHALLENGE_MARKERS = ("js-challenge-loader", "servicepipe", "id_captcha_frame_div", "spsn=")


class YouDoAdapter(BaseSourceAdapter):
    kind = "youdo"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.listing_url = str(self.config.params.get("url") or "https://youdo.com/tasks-all-opened-all/")

    async def fetch_listing(self) -> list[UnifiedJob]:
        result = await self.fetch_page(self.listing_url)
        probe = result.text.lower()
        if any(marker in probe for marker in CHALLENGE_MARKERS):
            raise SourceBlocked(
                "YouDo отдаёт JS-антибот (ServicePipe). Обход не поддерживается - источник приостановлен.",
                source=self.name,
            )
        soup, final_url = await self.get_soup(self.listing_url)
        cards = soup.select("div.task-card, div[data-task-id], article[class*='task']")
        if not cards:
            raise SourceParseError(
                "YouDo: карточки не найдены - публичный каталог изменился или требует JS",
                source=self.name,
            )
        jobs: list[UnifiedJob] = []
        for card in cards[: self.config.max_items]:
            job = self._parse_card(card, final_url)
            if job is not None:
                jobs.append(job)
        if not jobs:
            raise SourceParseError("YouDo: не удалось разобрать карточки", source=self.name)
        return await self.finalize(jobs)

    def _parse_card(self, card, base_url: str) -> UnifiedJob | None:
        link = card.select_one("a[href*='/tasks/']") or card.find("a", href=True)
        if link is None:
            return None
        title = link.get_text(" ", strip=True) or str(link.get("title") or "").strip()
        href = link.get("href") or ""
        if not title or not href:
            return None
        url = href if href.startswith("http") else f"https://youdo.com{href}"
        text = card.get_text(" ", strip=True)
        description = self.clean_html(
            (card.select_one("[class*='description'], [class*='desc'], p") or card).get_text(" ", strip=True)
        )
        price_text = (card.select_one("[class*='price'], [class*='budget']") or card).get_text(" ", strip=True)
        budget_min, budget_max, currency = extract_budget(f"{price_text} {text}", default_currency="RUB")
        proposals = self.extract_proposals(text)
        return self.build_job(
            external_id=str(card.get("data-task-id") or url.rstrip("/").rsplit("/", 1)[-1]),
            url=url,
            title=title,
            description=description,
            budget_min=budget_min,
            budget_max=budget_max,
            currency=currency,
            proposals_count=proposals,
            status=self.extract_status(text),
            published_at=self.parse_datetime(
                (card.select_one("time[datetime]") or {}).get("datetime") if card.select_one("time[datetime]") else None
            ),
            extra={"feed": "html"},
        )

    async def fetch_details(self, job: UnifiedJob) -> UnifiedJob:
        try:
            soup, _ = await self.get_soup(job.url)
        except SourceError:
            return job
        body = soup.select_one("[class*='task-description'], article")
        if body is not None:
            text = self.clean_html(body.get_text(" ", strip=True))
            if len(text) > len(job.description):
                job.description = text[:12000]
        return job
