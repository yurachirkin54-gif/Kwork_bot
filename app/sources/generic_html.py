"""Universal HTML/CSS adapter: any niche marketplace through YAML selectors."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from loguru import logger

from app.models import UnifiedJob
from app.sources.base import (
    BaseSourceAdapter,
    SourceParseError,
    extract_budget,
    extract_int,
    parse_amount,
)


def split_selector(selector: str) -> tuple[str, str | None]:
    """`a.title@href` -> ("a.title", "href")."""

    if "@" in selector:
        css, _, attr = selector.rpartition("@")
        return css.strip(), attr.strip()
    return selector.strip(), None


class GenericHTMLAdapter(BaseSourceAdapter):
    """Configured with `selectors.item` and per-field CSS selectors."""

    kind = "html"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.listing_url = str(self.config.params.get("url") or self.config.url or "").strip()
        self.selectors = self.config.selectors
        self.default_status = str(self.config.params.get("default_status", "open"))

    async def fetch_listing(self) -> list[UnifiedJob]:
        if not self.listing_url:
            raise SourceParseError("Не задан url страницы", source=self.name)
        if not self.selectors.item:
            raise SourceParseError("Не задан selectors.item", source=self.name)
        soup, final_url = await self.get_soup(self.listing_url)
        nodes = soup.select(self.selectors.item)
        if not nodes:
            raise SourceParseError(
                f"Селектор {self.selectors.item!r} не нашёл ни одного элемента", source=self.name
            )
        jobs: list[UnifiedJob] = []
        for node in nodes[: self.config.max_items]:
            job = self.parse_node(node, final_url)
            if job is not None:
                jobs.append(job)
        if not jobs:
            raise SourceParseError("Элементы найдены, но ни один заголовок не разобран", source=self.name)
        return await self.finalize(jobs)

    # ------------------------------------------------------------------ parsing
    def _text(self, node: Tag, selector: str | None) -> str | None:
        if not selector:
            return None
        css, attr = split_selector(selector)
        if not css:
            return None
        target = node if css in (":self", "self") else node.select_one(css)
        if target is None:
            return None
        if attr:
            value = target.get(attr)
            if isinstance(value, list):
                return " ".join(str(item) for item in value)
            return str(value).strip() if value is not None else None
        return target.get_text(" ", strip=True) or None

    def _text_list(self, node: Tag, selector: str | None) -> list[str]:
        if not selector:
            return []
        css, attr = split_selector(selector)
        values: list[str] = []
        for target in node.select(css):
            if attr:
                raw = target.get(attr)
                if raw:
                    values.append(str(raw).strip())
            else:
                text = target.get_text(" ", strip=True)
                if text:
                    values.append(text)
        return [v for v in values if v]

    def parse_node(self, node: Tag, base_url: str) -> UnifiedJob | None:
        title = self._text(node, self.selectors.title)
        if not title:
            heading = node.find(["h1", "h2", "h3", "h4"])
            title = heading.get_text(" ", strip=True) if heading else ""
        if not title:
            return None

        link = self._text(node, self.selectors.url)
        if not link:
            link = self._text(node, self.selectors.title)
            if link and link == title:
                link = None
        if not link:
            anchor = node.find("a", href=True)
            link = anchor["href"] if anchor else ""
        if not link:
            return None
        url = urljoin(base_url, link)

        description = self.clean_html(self._text(node, self.selectors.description))
        price_text = self._text(node, self.selectors.price)
        budget_min: float | None = None
        budget_max: float | None = None
        currency = self.currency
        if price_text:
            budget_min, budget_max, currency = extract_budget(price_text, default_currency=self.currency)
            if budget_min is None and self.config.regex.budget:
                match = re.search(self.config.regex.budget, price_text, re.IGNORECASE)
                if match:
                    amount = parse_amount(next((g for g in match.groups() if g), match.group(0)))
                    if amount:
                        budget_min = budget_max = amount
        if budget_min is None and description:
            budget_min, budget_max, currency = extract_budget(description, default_currency=self.currency)

        proposals_text = self._text(node, self.selectors.proposals)
        proposals = self.extract_proposals(proposals_text, self.config.regex.proposals)
        if proposals is None and proposals_text:
            proposals = extract_int(proposals_text)

        published = self.parse_datetime(
            self._text(node, self.selectors.published_at),
            tz_name=self.config.params.get("timezone"),
        )
        status_text = self._text(node, self.selectors.status)
        status = self.extract_status(status_text, default=self.default_status)  # type: ignore[arg-type]
        if descriptions_indicate_closed(description):
            status = "closed"
        rating_text = self._text(node, self.selectors.client_rating)
        rating = None
        if rating_text:
            match = re.search(r"\d+[.,]?\d*", rating_text.replace(",", "."))
            if match:
                try:
                    rating = float(match.group().replace(",", "."))
                except ValueError:
                    rating = None
        reviews = extract_int(self._text(node, self.selectors.client_reviews))
        skills = self._text_list(node, self.selectors.skills)

        external_id = self._external_id(node, url)
        return self.build_job(
            external_id=external_id,
            url=url,
            title=title,
            description=description,
            budget_min=budget_min,
            budget_max=budget_max,
            currency=currency,
            proposals_count=proposals,
            status=status,
            published_at=published,
            client_rating=rating,
            client_reviews=reviews,
            skills=skills,
        )

    def _external_id(self, node: Tag, url: str) -> str:
        for attribute in ("data-id", "data-project-id", "data-task-id", "id"):
            value = node.get(attribute)
            if value:
                return str(value).strip()
        match = re.search(r"[/-](\d{3,})(?:[/?#]|$)", url)
        if match:
            return match.group(1)
        return url

    async def fetch_details(self, job: UnifiedJob) -> UnifiedJob:
        if not self.selectors.detail or not job.url:
            return job
        try:
            soup, final_url = await self.get_soup(job.url)
        except Exception as exc:
            logger.debug(f"{self.name}: детальная страница недоступна ({exc})")
            return job
        container = soup.select_one(self.selectors.detail.get("container", "body")) or soup
        description = self._text(container, self.selectors.detail.get("description"))
        if description:
            job.description = self.clean_html(description)[:12000]
        proposals = self.extract_proposals(
            self._text(container, self.selectors.detail.get("proposals")), self.config.regex.proposals
        )
        if proposals is not None:
            job.proposals_count = proposals
        status_text = self._text(container, self.selectors.detail.get("status"))
        if status_text:
            job.status = self.extract_status(status_text, default=job.status)  # type: ignore[assignment]
        price_text = self._text(container, self.selectors.detail.get("price"))
        if price_text:
            low, high, currency = extract_budget(price_text, default_currency=job.currency)
            if low is not None:
                job.budget_min, job.budget_max, job.currency = low, high, currency
                job.budget_type = "fixed"
                await self.converter.enrich(job)
        job.extra["detail_url"] = final_url
        return job

    def needs_details(self, job: UnifiedJob) -> bool:
        return bool(self.selectors.detail) and bool(job.url) and not job.description


def descriptions_indicate_closed(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return bool(re.search(r"(заказ|проект)\s+(закрыт|завершён|завершен|отменён|отменен)", lowered))
