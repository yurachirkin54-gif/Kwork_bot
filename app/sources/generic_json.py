"""Universal JSON/GraphQL adapter with dotted field mapping from YAML."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin

from loguru import logger

from app.models import UnifiedJob
from app.sources.base import BaseSourceAdapter, SourceParseError, extract_budget


def dig(data: Any, path: str | None, default: Any = None) -> Any:
    """Resolve a dotted path (`data.jobs`, `items.0.name`) in nested dicts/lists."""

    if not path:
        return data
    current = data
    for part in str(path).split("."):
        if current is None:
            return default
        if isinstance(current, dict):
            current = current.get(part, default)
        elif isinstance(current, list):
            try:
                index = int(part)
            except ValueError:
                return default
            if -len(current) <= index < len(current):
                current = current[index]
            else:
                return default
        else:
            return default
    return current


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits) if digits else None


def normalize_api_status(value: Any) -> str:
    if value is None:
        return "open"
    if isinstance(value, bool):
        return "open" if value else "closed"
    text = str(value).strip().lower()
    if text in {"open", "active", "1", "true", "new", "published"}:
        return "open"
    if text in {"closed", "completed", "finished", "archived", "0", "false", "cancelled"}:
        return "closed"
    if text in {"in_progress", "in progress", "hired", "working", "assigned"}:
        return "in_progress"
    return "unknown"


class GenericJSONAdapter(BaseSourceAdapter):
    """Configured with `params.url`, `params.method/payload` and `mapping`."""

    kind = "json"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.api_url = str(self.config.params.get("url") or self.config.url or "").strip()
        self.method = str(self.config.params.get("method", "GET")).upper()
        self.payload = self.config.params.get("payload")
        self.mapping = self.config.mapping

    async def fetch_listing(self) -> list[UnifiedJob]:
        if not self.api_url:
            raise SourceParseError("Не задан url API", source=self.name)
        headers = {"Accept": "application/json", **self.config.headers}
        json_body = None
        data = None
        if self.method in ("POST", "PUT", "PATCH") and self.payload is not None:
            if isinstance(self.payload, str):
                data = {"query": self.payload}
            else:
                json_body = self.payload
        result = await self.fetch_page(self.api_url, method=self.method, headers=headers, json_body=json_body, data=data)
        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise SourceParseError(f"Ответ не является JSON: {exc}", source=self.name) from exc
        if isinstance(payload, dict) and payload.get("errors"):
            logger.debug(f"{self.name}: API вернул errors: {payload['errors']}")
        items = dig(payload, self.mapping.items_path, None)
        if items is None and not self.mapping.items_path and isinstance(payload, list):
            items = payload
        if not isinstance(items, list):
            raise SourceParseError(
                f"Путь {self.mapping.items_path!r} не содержит массив", source=self.name
            )
        jobs: list[UnifiedJob] = []
        for item in items[: self.config.max_items]:
            if not isinstance(item, dict):
                continue
            job = self.parse_item(item, base_url=result.url)
            if job is not None:
                jobs.append(job)
        if not jobs:
            raise SourceParseError("API вернул пустой список заказов", source=self.name)
        return await self.finalize(jobs)

    def parse_item(self, item: dict[str, Any], *, base_url: str = "") -> UnifiedJob | None:
        mapping = self.mapping
        title = str(dig(item, mapping.title, "") or "").strip()
        external_id = dig(item, mapping.external_id)
        if external_id is None or str(external_id).strip() == "":
            external_id = dig(item, "id") or dig(item, "uuid") or dig(item, "slug")
        if not title or external_id is None:
            return None

        url = str(dig(item, mapping.url, "") or "").strip()
        if not url and mapping.url_template:
            url = mapping.url_template.format(external_id=external_id, id=external_id)
        if not url:
            url = base_url
        url = urljoin(base_url, url)

        description = str(dig(item, mapping.description, "") or "")
        budget_min = as_float(dig(item, mapping.budget_min)) if mapping.budget_min else None
        budget_max = as_float(dig(item, mapping.budget_max)) if mapping.budget_max else None
        currency = str(dig(item, mapping.currency, "") or self.currency)
        if budget_min is None and budget_max is None:
            # Common shapes: {"budget": 5000} or text with an amount
            scalar_budget = dig(item, "budget") or dig(item, "price") or dig(item, "amount")
            if isinstance(scalar_budget, (int, float, str)):
                budget_min = as_float(scalar_budget)
                budget_max = budget_min
            elif isinstance(scalar_budget, dict):
                budget_min = as_float(scalar_budget.get("min") or scalar_budget.get("from"))
                budget_max = as_float(scalar_budget.get("max") or scalar_budget.get("to"))
                currency = str(scalar_budget.get("currency") or currency)
        if budget_min is None and budget_max is None and description:
            budget_min, budget_max, detected_currency = extract_budget(description, default_currency=currency)
            currency = detected_currency

        proposals = as_int(dig(item, mapping.proposals)) if mapping.proposals else None
        published = self.parse_datetime(dig(item, mapping.published_at) if mapping.published_at else None)
        raw_status = dig(item, mapping.status) if mapping.status else None
        status = normalize_api_status(raw_status)

        skills_raw = dig(item, mapping.skills) if mapping.skills else None
        if isinstance(skills_raw, list):
            skills = [str(s.get("name") if isinstance(s, dict) else s) for s in skills_raw]
        elif isinstance(skills_raw, str):
            skills = [chunk.strip() for chunk in skills_raw.split(",")]
        else:
            skills = []

        verified_raw = dig(item, mapping.client_verified) if mapping.client_verified else None
        client_verified = None
        if isinstance(verified_raw, bool):
            client_verified = verified_raw
        elif verified_raw is not None:
            client_verified = str(verified_raw).strip().lower() in {"1", "true", "yes", "verified"}

        return self.build_job(
            external_id=str(external_id),
            url=url,
            title=title,
            description=description,
            budget_min=budget_min,
            budget_max=budget_max,
            currency=currency,
            proposals_count=proposals,
            status=status,  # type: ignore[arg-type]
            published_at=published,
            client_rating=as_float(dig(item, mapping.client_rating)) if mapping.client_rating else None,
            client_reviews=as_int(dig(item, mapping.client_reviews)) if mapping.client_reviews else None,
            client_hire_rate=as_float(dig(item, mapping.client_hire_rate)) if mapping.client_hire_rate else None,
            client_verified=client_verified,
            skills=skills,
            category=str(dig(item, mapping.category, "") or "") or None if mapping.category else None,
        )

    async def fetch_details(self, job: UnifiedJob) -> UnifiedJob:
        return job
