"""Base adapter contract: listing (phase 1) -> details (phase 2) -> normalization."""

from __future__ import annotations

import html as html_lib
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any

from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from loguru import logger

from app.config import SourceConfig
from app.core.currency import CurrencyConverter
from app.models import JobStatus, UnifiedJob, as_utc, resolve_timezone, utcnow
from app.sources.browser_client import BrowserClient, BrowserFetchError, BrowserUnavailable
from app.sources.http_client import FetchResult, HttpClient, HttpError

ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
SPACE_RE = re.compile(r"\s+")
RELATIVE_RE = re.compile(
    r"(\d+)\s*(минут\w*|мин\.?|час\w*|ч\.?|д(?:ень|ня|ней|н\.?)|недел\w*|месяц\w*|мес\.?)",
    re.IGNORECASE,
)
RU_DOTTED_DATE_RE = re.compile(r"^\s*\d{1,2}\.\d{1,2}\.\d{2,4}")
RU_MONTHS: tuple[tuple[str, str], ...] = (
    (r"январ[ья]|янв\.?", "January"),
    (r"феврал[ья]|фев\.?", "February"),
    (r"март[а]?|мар\.?", "March"),
    (r"апрел[ья]|апр\.?", "April"),
    (r"ма[йя]", "May"),
    (r"июн[ья]", "June"),
    (r"июл[ья]", "July"),
    (r"август[а]?|авг\.?", "August"),
    (r"сентябр[ья]|сен\.?", "September"),
    (r"октябр[ья]|окт\.?", "October"),
    (r"ноябр[ья]|нояб?\.?", "November"),
    (r"декабр[ья]|дек\.?", "December"),
)

BLOCK_MARKERS = (
    "just a moment",
    "attention required! | cloudflare",
    "cf-challenge",
    "checking your browser",
    "ddos-guard",
    "access denied",
    "请求过于频繁",
)
CAPTCHA_MARKERS = ("captcha", "recaptcha", "hcaptcha", "turnstile", "я не робот")

CURRENCY_PATTERNS: list[tuple[str, str]] = [
    ("RUB", r"(?:₽|руб(?:лей|ля|ль|\.)?|rub|р\.)"),
    ("USD", r"(?:usd|\$|доллар(?:ов|а|ы)?)"),
    ("EUR", r"(?:eur|€|евро)"),
    ("UAH", r"(?:uah|грн\.?|₴|гривен)"),
    ("KZT", r"(?:kzt|тенге|₸)"),
    ("BYN", r"(?:byn|бел\.?\s*руб)"),
    ("GBP", r"(?:gbp|£|фунт)"),
]

STATUS_PATTERNS: list[tuple[str, str]] = [
    ("closed", r"закрыт|завершен|выполнен|отменен|архив|closed|completed|cancelled|expired"),
    ("in_progress", r"в работе|выполняется|in progress|hired|взят"),
    ("open", r"открыт|актив|ищет исполнителя|open|active|new"),
]


class SourceError(Exception):
    """Base adapter failure carrying a machine readable reason."""

    reason = "error"

    def __init__(self, message: str, *, source: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.source = source
        self.status = status


class SourceBlocked(SourceError):
    """403 / Cloudflare / captcha / auth rejection."""

    reason = "blocked"


class SourceRateLimited(SourceError):
    """429 or explicit rate limit."""

    reason = "rate_limited"


class SourceParseError(SourceError):
    """Markup changed or empty result while the page loaded fine."""

    reason = "parse_error"


class SourceUnavailable(SourceError):
    """Network / 5xx / timeout."""

    reason = "error"


def looks_like_block_page(text: str) -> bool:
    if not text:
        return False
    head = text[:6000].lower()
    if any(marker in head for marker in BLOCK_MARKERS):
        return True
    # Captcha pages are tiny; real marketplace pages are large.
    if len(text) < 20000 and any(marker in head for marker in CAPTCHA_MARKERS):
        return True
    return False


def parse_amount(raw: str | None) -> float | None:
    """Parse '5 000', '5.000', '1 500,50', '1,5k', '2 000 000'."""

    if not raw:
        return None
    text = ZERO_WIDTH_RE.sub("", str(raw)).replace("\u00a0", " ").strip().lower()
    text = text.replace(" ", "")
    multiplier = 1.0
    if text.endswith("k") or text.endswith("к"):
        multiplier = 1000.0
        text = text[:-1]
    elif text.endswith("m") or text.endswith("м"):
        multiplier = 1_000_000.0
        text = text[:-1]
    text = text.replace("₽", "").replace("$", "").replace("€", "").replace("£", "")
    if "," in text and "." in text:
        text = text.replace(",", "")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        if len(tail) == 3 and head:
            text = head + tail
        else:
            text = head + "." + tail if head else "0." + tail
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch == ".")
    if cleaned.count(".") > 1:
        head, _, tail = cleaned.rpartition(".")
        cleaned = head.replace(".", "") + "." + tail
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value * multiplier if value > 0 else None


def extract_currency(text: str | None, default: str = "RUB") -> str:
    if not text:
        return default
    lowered = html_lib.unescape(str(text)).lower()
    for code, pattern in CURRENCY_PATTERNS:
        if re.search(pattern, lowered):
            return code
    return default


def extract_budget(text: str | None, default_currency: str = "RUB") -> tuple[float | None, float | None, str]:
    """Extract (min, max, currency) from any free-form text."""

    if not text:
        return None, None, default_currency
    # RSS feeds often contain numeric entities such as &#8381; for ₽
    cleaned = html_lib.unescape(ZERO_WIDTH_RE.sub(" ", str(text)))
    currency = extract_currency(cleaned, default_currency)
    amounts: list[float] = []
    for code, pattern in CURRENCY_PATTERNS:
        for match in re.finditer(
            rf"(?P<num>\d[\d\s.,]{{0,15}})\s*(?:{pattern})",
            cleaned,
            re.IGNORECASE,
        ):
            value = parse_amount(match.group("num"))
            if value:
                amounts.append(value)
        for match in re.finditer(
            rf"(?:{pattern})\s*(?P<num>\d[\d\s.,]{{0,15}})",
            cleaned,
            re.IGNORECASE,
        ):
            value = parse_amount(match.group("num"))
            if value:
                amounts.append(value)
        if amounts:
            currency = code
            break
    if not amounts:
        return None, None, currency
    low = min(amounts)
    high = max(amounts)
    return low, high, currency


def extract_int(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"\d+", str(text).replace("\u00a0", " "))
    return int(match.group()) if match else None


class BaseSourceAdapter(ABC):
    """Two-phase adapter: cheap listing, then details only for new jobs."""

    kind: str = "base"

    def __init__(
        self,
        config: SourceConfig,
        http: HttpClient,
        converter: CurrencyConverter,
        *,
        browser: BrowserClient | None = None,
    ) -> None:
        self.config = config
        self.http = http
        self.converter = converter
        self.browser = browser

    # ----------------------------------------------------------------- metadata
    @property
    def name(self) -> str:
        return self.config.name

    @property
    def interval(self) -> int:
        return self.config.interval_seconds

    @property
    def currency(self) -> str:
        return self.config.currency or "RUB"

    # ----------------------------------------------------------------- fetching
    async def fetch_page(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        data: dict[str, Any] | None = None,
        wait_selector: str | None = None,
    ) -> FetchResult:
        if self.config.use_browser:
            return await self._fetch_with_browser(url, wait_selector)
        return await self._fetch_with_http(url, method=method, headers=headers, json_body=json_body, data=data)

    async def _fetch_with_browser(self, url: str, wait_selector: str | None) -> FetchResult:
        if self.browser is None:
            raise SourceUnavailable(
                "Источник требует use_browser, но браузерный клиент не инициализирован",
                source=self.name,
            )
        try:
            html = await self.browser.fetch_html(url, wait_selector=wait_selector)
        except BrowserUnavailable as exc:
            raise SourceUnavailable(str(exc), source=self.name) from exc
        except BrowserFetchError as exc:
            raise SourceUnavailable(f"Браузер не смог загрузить страницу: {exc}", source=self.name) from exc
        if looks_like_block_page(html):
            raise SourceBlocked("Страница похожа на защиту Cloudflare/капчу", source=self.name)
        return FetchResult(text=html, status=200, url=url, raw=html.encode("utf-8", errors="ignore"))

    async def _fetch_with_http(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        data: dict[str, Any] | None = None,
    ) -> FetchResult:
        merged_headers = {**self.config.headers, **(headers or {})}
        try:
            result = await self.http.get(
                url,
                method=method,
                headers=merged_headers,
                proxy=self.config.proxy_url,
                timeout=self.config.timeout,
                json_body=json_body,
                data=data,
                min_interval=float(self.config.params.get("min_request_interval", 1.0)),
            )
        except HttpError as exc:
            raise self._classify_http_error(exc) from exc
        if looks_like_block_page(result.text) and not result.from_cache:
            raise SourceBlocked("Получена страница защиты (Cloudflare/капча)", source=self.name)
        return result

    def _classify_http_error(self, exc: HttpError) -> SourceError:
        status = exc.status
        body = (exc.body or "")[:400]
        if status in (401, 403):
            return SourceBlocked(f"HTTP {status}: доступ запрещён ({body[:120]})", source=self.name, status=status)
        if status == 429:
            return SourceRateLimited(f"HTTP 429: превышен лимит запросов", source=self.name, status=status)
        if status in (404, 410):
            return SourceParseError(f"HTTP {status}: ресурс не найден (возможно, изменился URL)", source=self.name, status=status)
        return SourceUnavailable(f"HTTP {status}: {body[:120]}", source=self.name, status=status)

    async def get_soup(self, url: str, *, wait_selector: str | None = None) -> tuple[BeautifulSoup, str]:
        result = await self.fetch_page(url, wait_selector=wait_selector)
        # Passing bytes lets BeautifulSoup honour <meta charset> (many RU sites are cp1251)
        payload: str | bytes = result.raw if result.raw else result.text
        return BeautifulSoup(payload, "lxml"), result.url

    # ------------------------------------------------------------------- phases
    @abstractmethod
    async def fetch_listing(self) -> list[UnifiedJob]:
        """Phase 1: light listing request returning normalized jobs."""

    async def fetch_details(self, job: UnifiedJob) -> UnifiedJob:
        """Phase 2: enrich a single new job. Default: nothing to enrich."""

        return job

    def needs_details(self, job: UnifiedJob) -> bool:
        """Whether phase 2 should run for this job."""

        return bool(self.config.params.get("fetch_details", False)) and not job.description.strip()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def clean_html(value: str | None) -> str:
        if not value:
            return ""
        soup = BeautifulSoup(html_lib.unescape(str(value)), "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
        text = ZERO_WIDTH_RE.sub("", text)
        return SPACE_RE.sub(" ", text).strip()

    @staticmethod
    def parse_datetime(
        value: Any, *, default: datetime | None = None, tz_name: str | None = None
    ) -> datetime | None:
        if value is None:
            return default
        if isinstance(value, datetime):
            if value.tzinfo is None and tz_name:
                return value.replace(tzinfo=resolve_timezone(tz_name)).astimezone(timezone.utc)
            return as_utc(value)
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(float(value), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return default
        if isinstance(value, (tuple, list)) and len(value) >= 6:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                return default
        text = str(value).strip()
        if not text:
            return default
        relative = BaseSourceAdapter.parse_relative_datetime(text)
        if relative is not None:
            return relative
        normalized = re.sub(r"\s+в\s+", " ", text, flags=re.IGNORECASE)
        for pattern, replacement in RU_MONTHS:
            normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
        dayfirst = bool(RU_DOTTED_DATE_RE.match(normalized))
        try:
            parsed = date_parser.parse(normalized, fuzzy=True, dayfirst=dayfirst)
        except (ValueError, OverflowError, TypeError):
            return default
        if parsed.tzinfo is None and tz_name:
            return parsed.replace(tzinfo=resolve_timezone(tz_name)).astimezone(timezone.utc)
        return as_utc(parsed)

    @staticmethod
    def parse_relative_datetime(text: str, now: datetime | None = None) -> datetime | None:
        if not text:
            return None
        reference = now or utcnow()
        lowered = text.strip().lower()
        if not lowered:
            return None
        if "только что" in lowered or "just now" in lowered or "меньше минуты" in lowered:
            return reference
        if "полчаса" in lowered:
            return reference - timedelta(minutes=30)
        if "вчера" in lowered or "yesterday" in lowered:
            return reference - timedelta(days=1)
        if "сегодня" in lowered or "today" in lowered:
            return reference - timedelta(hours=1)
        matches = list(RELATIVE_RE.finditer(lowered))
        if not matches:
            return None
        total_delta = timedelta()
        for match in matches:
            amount = int(match.group(1))
            unit = match.group(2)
            if unit.startswith(("минут", "мин")):
                total_delta += timedelta(minutes=amount)
            elif unit.startswith(("час", "ч")):
                total_delta += timedelta(hours=amount)
            elif unit.startswith("недел"):
                total_delta += timedelta(weeks=amount)
            elif unit.startswith(("месяц", "мес")):
                total_delta += timedelta(days=30 * amount)
            else:
                total_delta += timedelta(days=amount)
        return reference - total_delta

    @staticmethod
    def extract_status(text: str | None, default: JobStatus = "open") -> JobStatus:
        if not text:
            return default
        lowered = str(text).lower()
        for status, pattern in STATUS_PATTERNS:
            if re.search(pattern, lowered):
                return status  # type: ignore[return-value]
        return default

    @staticmethod
    def extract_proposals(text: str | None, pattern: str | None = None) -> int | None:
        if not text:
            return None
        raw_text = str(text)
        if pattern:
            match = re.search(pattern, raw_text, re.IGNORECASE)
            if match:
                groups = [g for g in match.groups() if g and str(g).isdigit()]
                if groups:
                    return int(groups[0])
        if re.search(
            r"(?:нет\s+(?:ответов|откликов|предложений|заявок)|без\s+(?:ответов|откликов)|"
            r"(?:откликов|ответов|предложений)\s+нет|no\s+(?:bids|proposals|responses))",
            raw_text,
            re.IGNORECASE,
        ):
            return 0
        match = re.search(
            r"(?<!\d)(\d+)(?![.,:/-]\d)\s*(?:отклик\w*|заяв\w*|предложен\w*|ответ\w*|bids?|proposals?|responses?)",
            raw_text,
            re.IGNORECASE,
        )
        if match:
            return int(match.group(1))
        # Reversed wording: "Откликов: 3", "Заявок — 5" (never match dates like "заявок: 27.11.2026")
        match = re.search(
            r"(?<!приема\s)(?:отклик\w*|заяв\w*|предложен\w*|ответ\w*|bids?|proposals?|responses?)\s*[:\-—]?\s*(\d+)(?![.,:/-]\d)",
            raw_text,
            re.IGNORECASE,
        )
        if match:
            return int(match.group(1))
        return None

    def build_job(
        self,
        *,
        external_id: str,
        url: str,
        title: str,
        description: str = "",
        budget_min: float | None = None,
        budget_max: float | None = None,
        currency: str | None = None,
        budget_type: str | None = None,
        proposals_count: int | None = None,
        status: JobStatus | str = "open",
        published_at: datetime | None = None,
        client_rating: float | None = None,
        client_reviews: int | None = None,
        client_hire_rate: float | None = None,
        client_verified: bool | None = None,
        skills: list[str] | None = None,
        category: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> UnifiedJob:
        """Normalize and validate a job produced by any adapter."""

        resolved_currency = (currency or self.currency).upper()
        resolved_type = budget_type
        if resolved_type is None:
            resolved_type = "negotiable" if budget_min is None and budget_max is None else "fixed"
        if resolved_type not in ("fixed", "hourly", "negotiable"):
            resolved_type = "fixed"
        status_value = str(status or "open").lower()
        if status_value not in ("open", "in_progress", "closed", "unknown"):
            status_value = self.extract_status(status_value)
        job = UnifiedJob(
            source=self.name,
            external_id=str(external_id),
            url=url,
            title=SPACE_RE.sub(" ", self.clean_html(title))[:500],
            description=self.clean_html(description)[:12000],
            budget_min=budget_min,
            budget_max=budget_max,
            currency=resolved_currency,
            budget_type=resolved_type,  # type: ignore[arg-type]
            proposals_count=proposals_count,
            status=status_value,  # type: ignore[arg-type]
            published_at=as_utc(published_at),
            client_rating=client_rating,
            client_reviews=client_reviews,
            client_hire_rate=client_hire_rate,
            client_verified=client_verified,
            skills=[s.strip() for s in (skills or []) if str(s).strip()][:30],
            category=category or self.config.category,
            extra=extra or {},
        )
        return job

    async def finalize(self, jobs: list[UnifiedJob]) -> list[UnifiedJob]:
        """Convert budgets into RUB/USD and cap the batch."""

        results: list[UnifiedJob] = []
        for job in jobs[: self.config.max_items]:
            if job.budget_value is not None and job.budget_rub is None:
                await self.converter.enrich(job)
            results.append(job)
        return results

    # Diagnostic helper used by /sources "проверить сейчас"
    async def health_probe(self) -> int:
        jobs = await self.fetch_listing()
        logger.debug(f"{self.name}: пробный прогон вернул {len(jobs)} заказов")
        return len(jobs)
