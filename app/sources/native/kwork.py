"""Kwork adapter (real buyer projects exchange).

Primary transport: parses `window.stateData.wantsListData` embedded in
https://kwork.ru/projects (and category pages like ?c=41), which works
without login and carries exact proposal counts (`kwork_count`), budgets,
client hire rate, and timestamps.

Note: https://kwork.ru/rss is Kwork's catalogue of *freelancer services*
(gigs), NOT client projects, so it is never used as a fallback for orders.
Legacy `.wants-card` HTML parsing is kept as a secondary fallback.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from loguru import logger

from app.models import UnifiedJob
from app.sources.base import (
    BaseSourceAdapter,
    SourceError,
    SourceParseError,
    extract_budget,
)

PROJECT_ID_RE = re.compile(r"/(\d{5,})(?:[/?#]|$)")
STATE_DATA_RE = re.compile(r"window\.stateData\s*=\s*")

KWORK_CATEGORIES: dict[str, str] = {
    "5": "Тексты и переводы",
    "7": "Аудио, видео, съемка",
    "11": "Разработка и IT",
    "15": "Дизайн",
    "17": "SEO и трафик",
    "24": "Дизайн / Веб и мобильный дизайн",
    "25": "Дизайн / Логотип и брендинг",
    "27": "Дизайн / Полиграфия",
    "28": "Дизайн / Арт и иллюстрации",
    "37": "Разработка и IT / Создание сайта",
    "38": "Разработка и IT / Доработка и настройка сайта",
    "41": "Разработка и IT / Скрипты, боты и mini apps",
    "45": "Соцсети и маркетинг",
    "76": "Аудио, видео, съемка / Видеоролики",
    "77": "Аудио, видео, съемка / Интро и анимация логотипа",
    "78": "Аудио, видео, съемка / Видеосъемка и монтаж",
    "79": "Разработка и IT / Верстка",
    "80": "Разработка и IT / Десктоп программирование",
    "83": "Бизнес и жизнь",
    "106": "Аудио, видео, съемка / Редактирование аудио",
}


class KworkAdapter(BaseSourceAdapter):
    kind = "kwork"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        raw_urls = self.config.params.get("listing_urls")
        if isinstance(raw_urls, list) and raw_urls:
            self.listing_urls = [str(u).strip() for u in raw_urls if str(u).strip()]
        else:
            single = str(
                self.config.params.get("listing_url")
                or self.config.url
                or "https://kwork.ru/projects"
            ).strip()
            self.listing_urls = [single] if single else ["https://kwork.ru/projects"]
        self.fetch_details_enabled = bool(self.config.params.get("fetch_details", True))
        self.category_filter = str(self.config.params.get("category_filter") or "").strip().lower()

    async def fetch_listing(self) -> list[UnifiedJob]:
        jobs: list[UnifiedJob] = []
        seen: set[str] = set()
        last_error: SourceError | None = None

        for url in self.listing_urls:
            try:
                result = await self.fetch_page(url)
            except SourceError as exc:
                last_error = exc
                logger.debug(f"kwork: не удалось загрузить {url}: {exc}")
                continue
            parsed = self.parse_projects_page(result.raw if result.raw else result.text, base_url=result.url)
            for job in parsed:
                if job.external_id in seen:
                    continue
                seen.add(job.external_id)
                jobs.append(job)

        if not jobs:
            if last_error is not None:
                raise last_error
            raise SourceParseError(
                "Kwork не вернул проектов на бирже (возможно, изменилась структура stateData/HTML)",
                source=self.name,
            )
        return await self.finalize(jobs)

    # ------------------------------------------------------------- stateData
    @staticmethod
    def extract_state_data(html_text: str) -> dict[str, Any] | None:
        match = STATE_DATA_RE.search(html_text)
        if not match:
            return None
        decoder = json.JSONDecoder()
        try:
            payload, _ = decoder.raw_decode(html_text, match.end())
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def parse_projects_page(self, payload: str | bytes, *, base_url: str = "https://kwork.ru") -> list[UnifiedJob]:
        text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
        state = self.extract_state_data(text)
        if state is not None:
            wants_list = (state.get("wantsListData") or {}).get("pagination") or {}
            items = wants_list.get("data")
            if isinstance(items, list) and items:
                cat_map = self._extract_category_map(state)
                jobs: list[UnifiedJob] = []
                for item in items[: self.config.max_items]:
                    if not isinstance(item, dict):
                        continue
                    job = self._parse_want_dict(item, cat_map=cat_map)
                    if job is not None:
                        jobs.append(job)
                if jobs:
                    return jobs
        return self._parse_html_cards(text, base_url=base_url)

    @staticmethod
    def _extract_category_map(state: dict[str, Any]) -> dict[str, str]:
        mapping = dict(KWORK_CATEGORIES)
        for key in ("categories", "categoryList", "categoriesWithFavoritesList"):
            raw = state.get(key)
            entries = raw.values() if isinstance(raw, dict) else raw if isinstance(raw, list) else []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                parent_id = entry.get("id") or entry.get("CATID")
                parent_name = str(entry.get("name") or "").strip()
                if parent_id and parent_name:
                    mapping[str(parent_id)] = parent_name
                for sub in entry.get("cats") or []:
                    if not isinstance(sub, dict):
                        continue
                    sub_id = sub.get("id") or sub.get("CATID")
                    sub_name = str(sub.get("name") or "").strip()
                    if sub_id and sub_name:
                        mapping[str(sub_id)] = f"{parent_name} / {sub_name}" if parent_name else sub_name
        return mapping

    def _parse_want_dict(self, item: dict[str, Any], *, cat_map: dict[str, str]) -> UnifiedJob | None:
        want_id = item.get("id")
        title = str(item.get("name") or "").strip()
        if not want_id or not title:
            return None
        external_id = str(want_id)
        url = f"https://kwork.ru/projects/{external_id}/view"
        description = str(item.get("description") or "").strip()

        price_limit = item.get("priceLimit")
        possible_price = item.get("possiblePriceLimit")
        budget_min: float | None = None
        budget_max: float | None = None
        try:
            if price_limit is not None and float(price_limit) > 0:
                budget_min = float(price_limit)
                budget_max = budget_min
        except (TypeError, ValueError):
            pass
        try:
            if possible_price is not None and float(possible_price) > 0:
                possible = float(possible_price)
                if budget_min is None:
                    budget_min = budget_max = possible
                elif item.get("isHigherPrice") and possible > budget_min:
                    budget_max = possible
        except (TypeError, ValueError):
            pass

        raw_bids = item.get("kwork_count")
        try:
            proposals = int(raw_bids) if raw_bids is not None else None
        except (TypeError, ValueError):
            proposals = None

        status = self._resolve_want_status(item)
        published_at = self.parse_datetime(
            item.get("date_active") or item.get("date_create"),
            tz_name="Europe/Moscow",
        )

        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        user_data = user.get("data") if isinstance(user.get("data"), dict) else {}
        hire_rate = None
        raw_hire = user_data.get("wants_hired_percent")
        try:
            if raw_hire is not None and str(raw_hire).strip() != "":
                hire_rate = max(0.0, min(1.0, float(raw_hire) / 100.0))
        except (TypeError, ValueError):
            hire_rate = None

        cat_id = str(item.get("category_id") or "")
        category = cat_map.get(cat_id) or self.config.category
        if self.category_filter and category and self.category_filter not in category.lower():
            return None

        return self.build_job(
            external_id=external_id,
            url=url,
            title=title,
            description=description,
            budget_min=budget_min,
            budget_max=budget_max,
            currency="RUB",
            budget_type="fixed" if budget_min is not None else "negotiable",
            proposals_count=proposals,
            status=status,
            published_at=published_at,
            client_hire_rate=hire_rate,
            category=category,
            extra={
                "feed": "stateData",
                "category_id": cat_id,
                "client_wants_count": user_data.get("wants_count"),
                "views": item.get("views_dirty"),
            },
        )

    @staticmethod
    def _resolve_want_status(item: dict[str, Any]) -> str:
        if item.get("isWantArchive"):
            return "closed"
        raw_status = str(item.get("status") or "").strip().lower()
        if raw_status in {"archive", "closed", "cancel", "cancelled", "done", "rejected"}:
            return "closed"
        if raw_status in {"process", "in_progress"}:
            return "in_progress"
        if item.get("isWantActive") or raw_status == "active":
            return "open"
        return "open"

    # ----------------------------------------------------------- HTML fallback
    def _parse_html_cards(self, payload: str | bytes, *, base_url: str) -> list[UnifiedJob]:
        soup = BeautifulSoup(payload, "lxml")
        jobs: list[UnifiedJob] = []
        seen: set[str] = set()
        cards = soup.select("div.wants-card, article.wants-card, section.wants-card, div[class*='wants-card']")
        for card in cards[: self.config.max_items]:
            job = self._parse_card(card, base_url)
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
            (card.select_one(".wants-card__description-text, .wants-card__description") or card).get_text(
                " ", strip=True
            )
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
            published_at=self.parse_datetime(date_text, tz_name="Europe/Moscow"),
            extra={"feed": "html"},
        )

    # ------------------------------------------------------------------ details
    async def fetch_details(self, job: UnifiedJob) -> UnifiedJob:
        if not job.url:
            return job
        try:
            result = await self.fetch_page(job.url)
        except SourceError as exc:
            logger.debug(f"kwork: детальная страница недоступна ({exc})")
            return job

        state = self.extract_state_data(result.text)
        if state is not None:
            want = state.get("wantData") or state.get("want")
            if isinstance(want, dict) and want.get("id"):
                updated = self._parse_want_dict(want, cat_map=self._extract_category_map(state))
                if updated is not None:
                    if updated.description and len(updated.description) >= len(job.description):
                        job.description = updated.description
                    if updated.proposals_count is not None:
                        job.proposals_count = updated.proposals_count
                    job.status = updated.status
                    if updated.budget_min is not None:
                        job.budget_min = updated.budget_min
                        job.budget_max = updated.budget_max
                        job.budget_type = "fixed"
                        await self.converter.enrich(job)
                    if updated.client_hire_rate is not None:
                        job.client_hire_rate = updated.client_hire_rate
                    return job

        soup = BeautifulSoup(result.raw if result.raw else result.text, "lxml")
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
        # stateData listing already carries full description and kwork_count.
        if job.extra.get("feed") == "stateData" and job.description.strip():
            return False
        return self.fetch_details_enabled and bool(job.url)

    @staticmethod
    def _external_id(url: str) -> str:
        match = PROJECT_ID_RE.search(url)
        if match:
            return match.group(1)
        return url.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or url
