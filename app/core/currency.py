"""Currency conversion with in-memory (and optional DB) caching plus fallbacks."""

from __future__ import annotations

import asyncio
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger

from app.models import UnifiedJob

CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
OPEN_ER_URL = "https://open.er-api.com/v6/latest/USD"

# Last-resort static rates (to RUB) used when every API is unreachable.
FALLBACK_RATES_TO_RUB: dict[str, float] = {
    "RUB": 1.0,
    "USD": 92.0,
    "EUR": 100.0,
    "GBP": 117.0,
    "UAH": 2.2,
    "KZT": 0.19,
    "BYN": 28.0,
    "TRY": 2.7,
    "PLN": 23.0,
    "CNY": 12.8,
    "INR": 1.1,
    "AMD": 0.24,
    "GEL": 34.0,
    "UZS": 0.0073,
    "AZN": 54.0,
    "CAD": 67.0,
    "AUD": 60.0,
    "JPY": 0.6,
    "CHF": 104.0,
    "BRL": 18.0,
    "MXN": 5.0,
    "RON": 20.0,
    "CZK": 4.0,
    "SEK": 8.8,
    "AED": 25.0,
    "ILS": 25.0,
    "KRW": 0.068,
    "SGD": 69.0,
    "HKD": 11.8,
    "NZD": 55.0,
    "ZAR": 5.0,
    "NGN": 0.06,
    "EGP": 1.9,
    "PKR": 0.33,
    "BDT": 0.78,
    "VND": 0.0037,
    "THB": 2.6,
    "IDR": 0.0058,
    "MYR": 20.0,
    "PHP": 1.6,
    "ARS": 0.1,
    "CLP": 0.095,
    "COP": 0.023,
    "PEN": 24.0,
    "SAR": 24.5,
    "QAR": 25.0,
    "KWD": 300.0,
    "BHD": 244.0,
    "OMR": 239.0,
    "JOD": 130.0,
    "LBP": 0.001,
    "IQD": 0.07,
    "IRR": 0.0022,
    "AFN": 1.3,
    "KGS": 1.05,
    "TJS": 8.4,
    "TMT": 26.0,
    "MDL": 5.2,
    "RSD": 0.85,
    "BGN": 51.0,
    "HRK": 13.0,
    "HUF": 0.25,
    "DKK": 13.4,
    "NOK": 8.6,
    "ISK": 0.66,
    "BRL2": 18.0,
}


def _normalize_currency(currency: str | None) -> str:
    if not currency:
        return "RUB"
    code = str(currency).strip().upper()
    aliases = {
        "RUR": "RUB",
        "РУБ": "RUB",
        "РУБ.": "RUB",
        "₽": "RUB",
        "$": "USD",
        "US$": "USD",
        "€": "EUR",
        "£": "GBP",
        "ГРН": "UAH",
        "₴": "UAH",
        "ТГ": "KZT",
        "₸": "KZT",
        "БЕЛ.РУБ": "BYN",
    }
    return aliases.get(code, code)


def parse_cbr_xml(xml_text: str | bytes) -> dict[str, float]:
    """Parse the CBR daily XML feed into {currency: rubles_per_unit}."""

    rates: dict[str, float] = {"RUB": 1.0}
    if isinstance(xml_text, str):
        # ElementTree rejects Unicode strings carrying an encoding declaration.
        cleaned = re.sub(r"<\?xml[^>]*\?>", "", xml_text, count=1)
        root = ET.fromstring(cleaned)
    else:
        root = ET.fromstring(xml_text)
    for valute in root.findall("Valute"):
        code = (valute.findtext("CharCode") or "").strip().upper()
        nominal_text = (valute.findtext("Nominal") or "1").strip().replace(",", ".")
        value_text = (valute.findtext("Value") or "").strip().replace(",", ".").replace("\u00a0", "")
        if not code or not value_text:
            continue
        try:
            nominal = float(nominal_text) or 1.0
            value = float(value_text)
        except ValueError:
            continue
        if value <= 0:
            continue
        rates[code] = value / nominal
    return rates


def parse_open_er(payload: dict[str, Any]) -> dict[str, float]:
    """Convert open.er-api.com USD-based table into rubles_per_unit."""

    raw = payload.get("rates") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    rub_per_usd = raw.get("RUB")
    if not isinstance(rub_per_usd, (int, float)) or rub_per_usd <= 0:
        return {}
    rates: dict[str, float] = {"RUB": 1.0, "USD": float(rub_per_usd)}
    for code, value in raw.items():
        if not isinstance(value, (int, float)) or value <= 0:
            continue
        code = str(code).upper()
        rates[code] = float(rub_per_usd) / float(value)
    return rates


def convert_with_rates(amount: float, currency: str, target: str, rates: dict[str, float]) -> float | None:
    """Convert amount between currencies using rubles_per_unit table."""

    if amount is None:
        return None
    source = _normalize_currency(currency)
    destination = _normalize_currency(target)
    if source == destination:
        return float(amount)
    source_rate = rates.get(source)
    destination_rate = rates.get(destination)
    if not source_rate or not destination_rate:
        return None
    return float(amount) * source_rate / destination_rate


class CurrencyConverter:
    """Async converter with 6h TTL cache and graceful degradation."""

    def __init__(
        self,
        ttl_seconds: int = 6 * 3600,
        timeout: float = 12.0,
        cache_loader: Callable[[], Awaitable[dict[str, Any] | None]] | None = None,
        cache_saver: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.timeout = timeout
        self.rates: dict[str, float] = dict(FALLBACK_RATES_TO_RUB)
        self.updated_at: datetime | None = None
        self.source: str = "fallback"
        self._fetched_monotonic: float = 0.0
        self._lock = asyncio.Lock()
        self._cache_loader = cache_loader
        self._cache_saver = cache_saver
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------- lifecycle
    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
            self._owns_client = True
        return self._client

    # ----------------------------------------------------------------- rates
    def _is_fresh(self) -> bool:
        if not self._fetched_monotonic:
            return False
        return (time.monotonic() - self._fetched_monotonic) < self.ttl_seconds

    async def get_rates(self, force: bool = False) -> dict[str, float]:
        if not force and self._is_fresh():
            return self.rates
        async with self._lock:
            if not force and self._is_fresh():
                return self.rates
            await self._load_from_db()
            if not force and self._is_fresh():
                return self.rates
            if await self._refresh_from_cbr():
                return self.rates
            if await self._refresh_from_open_er():
                return self.rates
            logger.warning("Не удалось обновить курсы валют, используются резервные значения")
            self.rates = dict(FALLBACK_RATES_TO_RUB)
            self.source = "fallback"
            self.updated_at = datetime.now(timezone.utc)
            self._fetched_monotonic = time.monotonic()
            return self.rates

    async def _load_from_db(self) -> None:
        if self._cache_loader is None:
            return
        try:
            data = await self._cache_loader()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"Кэш курсов недоступен: {exc}")
            return
        if not data:
            return
        rates = data.get("rates")
        if not isinstance(rates, dict) or "USD" not in rates:
            return
        self.rates = {str(k).upper(): float(v) for k, v in rates.items() if isinstance(v, (int, float)) and v > 0}
        self.rates.setdefault("RUB", 1.0)
        self.source = str(data.get("source", "cache"))
        try:
            self.updated_at = datetime.fromisoformat(str(data.get("updated_at")))
        except ValueError:
            self.updated_at = datetime.now(timezone.utc)
        # DB cache is authoritative for ttl purposes only if fresh
        try:
            age = (datetime.now(timezone.utc) - self.updated_at).total_seconds()
        except TypeError:
            age = self.ttl_seconds + 1
        if age < self.ttl_seconds:
            self._fetched_monotonic = time.monotonic() - age

    async def _store(self, rates: dict[str, float], source: str) -> None:
        self.rates = rates
        self.source = source
        self.updated_at = datetime.now(timezone.utc)
        self._fetched_monotonic = time.monotonic()
        if self._cache_saver is not None:
            payload = {
                "rates": rates,
                "source": source,
                "updated_at": self.updated_at.isoformat(),
            }
            try:
                await self._cache_saver(payload)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(f"Не удалось сохранить курсы в БД: {exc}")

    async def _refresh_from_cbr(self) -> bool:
        try:
            response = await self.client.get(CBR_URL, headers={"User-Agent": "KworkBot/1.0"})
            response.raise_for_status()
            text = response.content.decode("windows-1251", errors="ignore")
            rates = parse_cbr_xml(text)
        except (httpx.HTTPError, ET.ParseError, UnicodeDecodeError, ValueError) as exc:
            logger.debug(f"ЦБ РФ недоступен: {exc}")
            return False
        if len(rates) < 3:
            return False
        merged = dict(FALLBACK_RATES_TO_RUB)
        merged.update(rates)
        await self._store(merged, "cbr.ru")
        return True

    async def _refresh_from_open_er(self) -> bool:
        try:
            response = await self.client.get(OPEN_ER_URL, headers={"User-Agent": "KworkBot/1.0"})
            response.raise_for_status()
            rates = parse_open_er(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug(f"open.er-api недоступен: {exc}")
            return False
        if len(rates) < 3:
            return False
        merged = dict(FALLBACK_RATES_TO_RUB)
        merged.update(rates)
        await self._store(merged, "open.er-api.com")
        return True

    # ------------------------------------------------------------- conversion
    async def convert(self, amount: float | None, currency: str, target: str) -> float | None:
        if amount is None:
            return None
        rates = await self.get_rates()
        return convert_with_rates(amount, currency, target, rates)

    async def to_rub(self, amount: float | None, currency: str) -> float | None:
        return await self.convert(amount, currency, "RUB")

    async def to_usd(self, amount: float | None, currency: str) -> float | None:
        return await self.convert(amount, currency, "USD")

    async def enrich(self, job: UnifiedJob) -> UnifiedJob:
        """Fill budget_rub / budget_usd for a normalized job."""

        amount = job.budget_value
        if amount is None:
            job.budget_rub = None
            job.budget_usd = None
            return job
        rates = await self.get_rates()
        rub = convert_with_rates(amount, job.currency, "RUB", rates)
        usd = convert_with_rates(amount, job.currency, "USD", rates)
        job.budget_rub = round(rub, 2) if rub is not None else None
        job.budget_usd = round(usd, 2) if usd is not None else None
        return job
