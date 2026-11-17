"""Aggregator for open remote-work APIs and RSS feeds (Remotive, RemoteOK, WWR)."""

from __future__ import annotations

import json

import feedparser
from loguru import logger

from app.models import UnifiedJob
from app.sources.base import BaseSourceAdapter, SourceError, SourceParseError

DEFAULT_BOARDS = [
    {"name": "remotive", "kind": "remotive", "url": "https://remotive.com/api/remote-jobs?category=software-dev&limit=30"},
    {"name": "remoteok", "kind": "remoteok", "url": "https://remoteok.com/api", "limit": 30},
    {"name": "weworkremotely", "kind": "rss", "url": "https://weworkremotely.com/categories/remote-programming-jobs.rss"},
]


class RemoteBoardsAdapter(BaseSourceAdapter):
    """Merges several public remote-job sources into one adapter."""

    kind = "remote_boards"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        configured = self.config.params.get("boards")
        self.boards = configured if isinstance(configured, list) and configured else DEFAULT_BOARDS

    async def fetch_listing(self) -> list[UnifiedJob]:
        jobs: list[UnifiedJob] = []
        errors: list[SourceError] = []
        for board in self.boards:
            if not isinstance(board, dict):
                continue
            try:
                fetched = await self._fetch_board(board)
                jobs.extend(fetched)
            except SourceError as exc:
                errors.append(exc)
                logger.warning(f"remote_boards/{board.get('name')}: {exc}")
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception(f"remote_boards/{board.get('name')}: непредвиденная ошибка {exc}")
        if not jobs:
            if errors:
                raise errors[0]
            raise SourceParseError("Ни одна remote-лента не вернула заказов", source=self.name)
        return await self.finalize(jobs)

    async def _fetch_board(self, board: dict) -> list[UnifiedJob]:
        kind = str(board.get("kind") or "rss").lower()
        url = str(board.get("url") or "")
        if not url:
            raise SourceParseError("У ленты не задан url", source=self.name)
        if kind == "remotive":
            return await self._fetch_remotive(board, url)
        if kind == "remoteok":
            return await self._fetch_remoteok(board, url)
        return await self._fetch_rss(board, url)

    async def _fetch_remotive(self, board: dict, url: str) -> list[UnifiedJob]:
        result = await self.fetch_page(url, headers={"Accept": "application/json"})
        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise SourceParseError(f"Remotive вернул не JSON: {exc}", source=self.name) from exc
        raw_jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(raw_jobs, list):
            raise SourceParseError("Remotive: нет поля jobs", source=self.name)
        jobs: list[UnifiedJob] = []
        for item in raw_jobs:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            job_url = str(item.get("url") or "").strip()
            if not title or not job_url:
                continue
            description = self.clean_html(str(item.get("description") or ""))
            salary = str(item.get("salary") or "")
            if salary:
                description = f"{description}\n\nЗарплата: {salary}".strip()
            jobs.append(
                self.build_job(
                    external_id=str(item.get("id") or job_url),
                    url=job_url,
                    title=title,
                    description=description,
                    currency="USD",
                    budget_type="negotiable",
                    proposals_count=None,
                    status="open",
                    published_at=self.parse_datetime(item.get("publication_date")),
                    skills=[str(t) for t in (item.get("tags") or []) if isinstance(t, (str, int))],
                    category=str(item.get("category") or self.config.category or "Remote"),
                    extra={
                        "board": board.get("name", "remotive"),
                        "company": item.get("company_name"),
                        "location": item.get("candidate_required_location"),
                        "job_type": item.get("job_type"),
                        "salary": salary,
                    },
                )
            )
        return jobs

    async def _fetch_remoteok(self, board: dict, url: str) -> list[UnifiedJob]:
        result = await self.fetch_page(url, headers={"Accept": "application/json"})
        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise SourceParseError(f"RemoteOK вернул не JSON: {exc}", source=self.name) from exc
        if not isinstance(payload, list):
            raise SourceParseError("RemoteOK: ожидался массив", source=self.name)
        limit = int(board.get("limit") or 50)
        jobs: list[UnifiedJob] = []
        for item in payload[:limit]:
            if not isinstance(item, dict) or not item.get("position"):
                continue  # first element is a legal notice
            title = str(item.get("position") or "").strip()
            job_url = str(item.get("url") or "")
            description = self.clean_html(str(item.get("description") or ""))
            salary_min = item.get("salary_min")
            salary_max = item.get("salary_max")
            salary_text = ""
            if isinstance(salary_min, (int, float)) and salary_min > 0:
                salary_text = f"{int(salary_min)}-{int(salary_max) if isinstance(salary_max, (int, float)) else salary_min} USD/год"
                description = f"{description}\n\nЗарплата: {salary_text}".strip()
            jobs.append(
                self.build_job(
                    external_id=str(item.get("id") or item.get("slug") or job_url),
                    url=job_url,
                    title=title,
                    description=description,
                    currency="USD",
                    budget_type="negotiable",
                    status="open",
                    published_at=self.parse_datetime(item.get("date")),
                    skills=[str(t) for t in (item.get("tags") or []) if isinstance(t, str)],
                    category=self.config.category or "Remote",
                    extra={
                        "board": board.get("name", "remoteok"),
                        "company": item.get("company"),
                        "location": item.get("location"),
                        "salary": salary_text,
                    },
                )
            )
        return jobs

    async def _fetch_rss(self, board: dict, url: str) -> list[UnifiedJob]:
        result = await self.fetch_page(url)
        parsed = feedparser.parse(result.raw if result.raw else result.text)
        if not parsed.entries:
            raise SourceParseError("RSS-лента пуста", source=self.name)
        jobs: list[UnifiedJob] = []
        for entry in parsed.entries:
            title = str(entry.get("title") or "").strip()
            job_url = str(entry.get("link") or "").strip()
            if not title or not job_url:
                continue
            description = self.clean_html(str(entry.get("summary") or ""))
            jobs.append(
                self.build_job(
                    external_id=str(entry.get("id") or job_url),
                    url=job_url,
                    title=title,
                    description=description,
                    currency="USD",
                    budget_type="negotiable",
                    status="open",
                    published_at=self.parse_datetime(entry.get("published_parsed") or entry.get("published")),
                    category=self.config.category or "Remote",
                    extra={"board": board.get("name", "rss")},
                )
            )
        return jobs
