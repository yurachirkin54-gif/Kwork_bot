"""Freelancer.com adapter over the official public REST API (no token required)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from loguru import logger

from app.models import UnifiedJob
from app.sources.base import BaseSourceAdapter, SourceParseError
from app.sources.generic_json import normalize_api_status


class FreelancerComAdapter(BaseSourceAdapter):
    kind = "freelancer_com"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.api_url = str(
            self.config.params.get("api_url")
            or "https://www.freelancer.com/api/projects/0.1/projects/active/"
        )
        self.query = str(self.config.params.get("query") or "")
        self.limit = int(self.config.params.get("limit") or 40)

    def _build_url(self) -> str:
        params: dict[str, Any] = {
            "limit": min(self.limit, 100),
            "job_details": "true",
            "full_description": "true",
            "compact": "true",
        }
        if self.query:
            params["query"] = self.query
        return f"{self.api_url}?{urlencode(params)}"

    async def fetch_listing(self) -> list[UnifiedJob]:
        result = await self.fetch_page(
            self._build_url(),
            headers={"Accept": "application/json"},
        )
        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise SourceParseError(f"API вернул не JSON: {exc}", source=self.name) from exc
        if isinstance(payload, dict) and payload.get("status") not in (None, "success"):
            logger.debug(f"freelancer.com: статус ответа {payload.get('status')}")
        projects = ((payload or {}).get("result") or {}).get("projects") or []
        if not isinstance(projects, list):
            raise SourceParseError("Неожиданная структура ответа result.projects", source=self.name)
        if not projects:
            raise SourceParseError("API не вернул активных проектов", source=self.name)
        jobs: list[UnifiedJob] = []
        for project in projects[: self.config.max_items]:
            job = self.parse_project(project)
            if job is not None:
                jobs.append(job)
        if not jobs:
            raise SourceParseError("Не удалось разобрать проекты", source=self.name)
        return await self.finalize(jobs)

    def parse_project(self, project: dict[str, Any]) -> UnifiedJob | None:
        title = str(project.get("title") or "").strip()
        project_id = project.get("id")
        if not title or project_id is None:
            return None
        seo_url = str(project.get("seo_url") or "").strip()
        url = f"https://www.freelancer.com/projects/{seo_url}" if seo_url else f"https://www.freelancer.com/project/{project_id}"
        description = str(project.get("description") or project.get("preview_description") or "")

        budget = project.get("budget") or {}
        minimum = budget.get("minimum") if isinstance(budget, dict) else None
        maximum = budget.get("maximum") if isinstance(budget, dict) else None
        currency = ((project.get("currency") or {}).get("code") or "USD") if isinstance(project.get("currency"), dict) else "USD"
        budget_min = float(minimum) if isinstance(minimum, (int, float)) and minimum > 0 else None
        budget_max = float(maximum) if isinstance(maximum, (int, float)) and maximum > 0 else None

        bid_stats = project.get("bid_stats") or {}
        bids = bid_stats.get("bid_count") if isinstance(bid_stats, dict) else None

        skills = []
        for job_tag in project.get("jobs") or []:
            if isinstance(job_tag, dict) and job_tag.get("name"):
                skills.append(str(job_tag["name"]))

        project_type = str(project.get("type") or "fixed").lower()
        budget_type = "hourly" if project_type == "hourly" else "fixed"
        if budget_min is None and budget_max is None:
            budget_type = "negotiable"

        owner = project.get("owner_info") or {}
        rating = None
        if isinstance(owner, dict):
            raw_rating = owner.get("rating") or owner.get("reputation") or owner.get("entire_history", {}).get("overall")
            if isinstance(raw_rating, (int, float)):
                rating = float(raw_rating)
                if rating > 5:  # some endpoints expose 0..10 scale
                    rating = round(rating / 2, 2)

        return self.build_job(
            external_id=str(project_id),
            url=url,
            title=title,
            description=description,
            budget_min=budget_min,
            budget_max=budget_max,
            currency=str(currency),
            budget_type=budget_type,
            proposals_count=int(bids) if isinstance(bids, (int, float)) else None,
            status=normalize_api_status(project.get("status")),  # type: ignore[arg-type]
            published_at=self.parse_datetime(project.get("time_submitted")),
            client_rating=rating,
            skills=skills,
            category=self.config.category,
            extra={"seo_url": seo_url, "project_type": project_type},
        )
