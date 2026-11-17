"""Monitoring orchestrator: per-source scheduling, two-phase scans, scoring, dedupe."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from loguru import logger

from app.config import ConfigManager
from app.core.currency import CurrencyConverter
from app.core.deduplicator import Deduplicator
from app.core.rate_limiter import CircuitBreaker
from app.models import ScoringResult, UnifiedJob, UserFilters, as_utc, local_now, utcnow
from app.processing.filters import HardFilter
from app.processing.llm_analyzer import LLMAnalyzer
from app.processing.scorer import JobScorer
from app.sources.base import BaseSourceAdapter, SourceError
from app.sources.browser_client import BrowserClient
from app.sources.http_client import HttpClient
from app.sources.registry import build_adapters
from app.storage.orm import Job
from app.storage.repository import Repository

if TYPE_CHECKING:  # pragma: no cover
    from app.telegram.notifier import Notifier


@dataclass
class ScanReport:
    """Outcome of one source scan (also used by /scan and --dry-run)."""

    source: str
    found: int = 0
    new: int = 0
    filtered: int = 0
    scored: int = 0
    duplicates: int = 0
    below_threshold: int = 0
    notified: int = 0
    deferred: int = 0
    seeded: int = 0
    errors: int = 0
    skipped: str | None = None
    error: str | None = None
    duration: float = 0.0
    details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.errors == 0 and not self.skipped

    def summary(self) -> str:
        if self.skipped:
            return f"{self.source}: пропущен ({self.skipped})"
        if self.error:
            return f"{self.source}: ошибка - {self.error}"
        parts = [f"найдено {self.found}", f"новых {self.new}"]
        if self.seeded:
            parts.append(f"в базу {self.seeded}")
        parts.append(f"отфильтровано {self.filtered}")
        parts.append(f"оценено {self.scored}")
        if self.duplicates:
            parts.append(f"дубли {self.duplicates}")
        if self.below_threshold:
            parts.append(f"ниже порога {self.below_threshold}")
        parts.append(f"отправлено {self.notified}")
        if self.deferred:
            parts.append(f"отложено {self.deferred}")
        return f"{self.source}: " + ", ".join(parts) + f" ({self.duration:.1f} с)"


class Engine:
    """Coordinates adapters, filters, scoring, storage and notifications."""

    def __init__(
        self,
        config: ConfigManager,
        repository: Repository,
        converter: CurrencyConverter,
        notifier: Notifier | None = None,
        llm: LLMAnalyzer | None = None,
        *,
        http: HttpClient | None = None,
        browser: BrowserClient | None = None,
        dry_run: bool = False,
        chat_ids: set[int] | None = None,
        timezone: str = "Europe/Moscow",
    ) -> None:
        self.config = config
        self.repo = repository
        self.converter = converter
        self.notifier = notifier
        self.llm = llm or LLMAnalyzer(config.app.llm)
        self.dry_run = dry_run
        self.chat_ids = chat_ids or set()
        self.timezone = timezone or "Europe/Moscow"
        self.http = http or HttpClient()
        self._browser = browser
        self.adapters = build_adapters(config.sources, self.http, converter, browser)
        self._adapters: dict[str, BaseSourceAdapter] = {a.name: a for a in self.adapters}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._next_run: dict[str, float] = {}
        self._stop = asyncio.Event()
        self._semaphore = asyncio.Semaphore(max(1, config.app.engine.max_concurrency))
        self._dedupe = Deduplicator(
            similarity_threshold=config.app.engine.dedupe_similarity,
            window_hours=config.app.engine.dedupe_window_hours,
        )
        self._paused = False
        self._needs_seed = False
        self._last_alert: dict[str, datetime] = {}
        self.last_reports: list[ScanReport] = []

    # ------------------------------------------------------------------ lifecycle
    async def initialize(self) -> None:
        overrides = await self.repo.get_setting("filters_overrides", {}) or {}
        if isinstance(overrides, dict):
            self.config.set_runtime_overrides(overrides)
        self._paused = bool(await self.repo.get_setting("paused", False))
        mode = self.config.effective_filters().cold_start_mode
        self._needs_seed = (await self.repo.count_jobs()) == 0 and mode == "silent_seed"
        if self._needs_seed:
            logger.info("Холодный старт: текущие заказы будут проиндексированы без рассылки")
        if self._paused:
            logger.info("Рассылка на паузе (сохранено в настройках)")

    def rebuild_adapters(self) -> None:
        self.adapters = build_adapters(self.config.sources, self.http, self.converter, self._browser)
        self._adapters = {adapter.name: adapter for adapter in self.adapters}
        for adapter in self.adapters:
            self._next_run.setdefault(adapter.name, 0.0)

    @property
    def paused(self) -> bool:
        return self._paused

    async def set_paused(self, paused: bool) -> None:
        self._paused = paused
        await self.repo.set_setting("paused", paused)

    def filters(self) -> UserFilters:
        return self.config.effective_filters()

    async def update_filters(self, patch: dict[str, Any]) -> UserFilters:
        """Merge a filter patch, persist it and apply instantly (no restart)."""

        current = self.config.effective_filters()
        merged = current.model_dump()
        for key, value in patch.items():
            if key not in merged:
                continue
            if key == "quiet_hours" and isinstance(value, dict):
                merged["quiet_hours"] = {**merged["quiet_hours"], **value}
            else:
                merged[key] = value
        updated = UserFilters.model_validate(merged)
        overrides = self.config.runtime_overrides
        for key, value in patch.items():
            if key == "quiet_hours" and isinstance(value, dict):
                overrides["quiet_hours"] = {**(overrides.get("quiet_hours") or {}), **value}
            elif key in UserFilters.model_fields:
                overrides[key] = value
        self.config.set_runtime_overrides(overrides)
        await self.repo.set_setting("filters_overrides", overrides)
        return updated

    async def reset_filters(self) -> UserFilters:
        self.config.set_runtime_overrides({})
        await self.repo.delete_setting("filters_overrides")
        return self.config.effective_filters()

    # -------------------------------------------------------------------- access
    def get_adapter(self, name: str) -> BaseSourceAdapter | None:
        return self._adapters.get(name)

    def adapter_names(self) -> list[str]:
        return [adapter.name for adapter in self.adapters]

    def _breaker_for(self, name: str) -> CircuitBreaker:
        breaker = self._breakers.get(name)
        if breaker is None:
            breaker = CircuitBreaker()
            self._breakers[name] = breaker
        return breaker

    # ------------------------------------------------------------------- scanning
    async def scan_once(self, names: list[str] | None = None, force: bool = False) -> list[ScanReport]:
        if self.config.maybe_reload():
            self.rebuild_adapters()
        selected: list[BaseSourceAdapter] = []
        for adapter in self.adapters:
            if names is not None and adapter.name not in names:
                continue
            if not adapter.config.enabled:
                continue
            selected.append(adapter)
        if not selected:
            return []

        cold_start = self._needs_seed and not self.dry_run
        started = time.perf_counter()
        results = await asyncio.gather(
            *(self._scan_adapter(adapter, cold_start=cold_start, force=force) for adapter in selected),
            return_exceptions=True,
        )
        reports: list[ScanReport] = []
        for adapter, result in zip(selected, results):
            if isinstance(result, ScanReport):
                reports.append(result)
            else:
                logger.exception(f"{adapter.name}: непредвиденная ошибка сканирования: {result}")
                reports.append(
                    ScanReport(source=adapter.name, errors=1, error=str(result), duration=0.0)
                )
        self._needs_seed = False
        self.last_reports = reports
        total = sum(len(report.details) for report in reports)
        logger.info(
            f"Цикл сканирования завершён за {time.perf_counter() - started:.1f} с: "
            f"{len(reports)} источников, деталей {total}"
        )
        return reports

    async def _scan_adapter(
        self, adapter: BaseSourceAdapter, *, cold_start: bool = False, force: bool = False
    ) -> ScanReport:
        report = ScanReport(source=adapter.name)
        started = time.perf_counter()
        breaker = self._breaker_for(adapter.name)
        if not force and not breaker.can_attempt():
            remaining = breaker.cooldown_remaining()
            report.skipped = f"cooldown ещё {remaining} с"
            report.duration = time.perf_counter() - started
            return report

        filters = self.filters()
        try:
            async with self._semaphore:
                listing = await adapter.fetch_listing()
        except SourceError as exc:
            report.duration = time.perf_counter() - started
            await self._handle_source_failure(adapter, breaker, exc, report)
            return report
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(f"{adapter.name}: сбой адаптера")
            report.duration = time.perf_counter() - started
            report.errors = 1
            report.error = str(exc)
            return report

        breaker.record_success()
        await self.repo.record_source_success(adapter.name, adapter.kind, adapter.config.enabled)
        report.found = len(listing)

        if cold_start:
            await self._seed_listing(listing, filters, report)
            report.duration = time.perf_counter() - started
            logger.info(report.summary())
            return report

        try:
            await self._process_listing(adapter, listing, filters, report)
        finally:
            report.duration = time.perf_counter() - started
        logger.info(report.summary())
        return report

    async def _handle_source_failure(
        self,
        adapter: BaseSourceAdapter,
        breaker: CircuitBreaker,
        exc: SourceError,
        report: ScanReport,
    ) -> None:
        cooldown = breaker.record_failure(str(exc))
        state = exc.reason if exc.reason in {"blocked", "rate_limited", "parse_error"} else "error"
        cooldown_until = breaker.opened_until
        await self.repo.record_source_failure(
            adapter.name,
            adapter.kind,
            state,
            str(exc),
            cooldown_until=cooldown_until,
            enabled=adapter.config.enabled,
        )
        report.errors = 1
        report.error = str(exc)
        logger.warning(f"{adapter.name}: {exc} (state={state}, cooldown={cooldown})")
        if cooldown or state == "blocked":
            await self._alert_source_problem(adapter, state, str(exc), cooldown)

    async def _alert_source_problem(
        self, adapter: BaseSourceAdapter, state: str, error: str, cooldown: int | None
    ) -> None:
        if self.notifier is None or self.dry_run:
            return
        last = self._last_alert.get(adapter.name)
        now = utcnow()
        if last is not None and (now - last) < timedelta(minutes=30):
            return
        self._last_alert[adapter.name] = now
        readable = {
            "blocked": "заблокирован (403/Cloudflare/капча)",
            "rate_limited": "ограничение частоты запросов (429)",
            "parse_error": "изменилась разметка или пустая выдача",
            "error": "сетевая ошибка или 5xx",
        }.get(state, state)
        text = (
            f"⚠️ <b>Источник {adapter.name}</b>: {readable}\n"
            f"Причина: {error[:400]}\n"
            + (f"Пауза: {cooldown // 60} мин.\n" if cooldown else "")
            + "Сканирование остальных источников продолжается."
        )
        await self.notifier.send_health_alert(text)

    # ------------------------------------------------------------------ processing
    async def _seed_listing(
        self, listing: list[UnifiedJob], filters: UserFilters, report: ScanReport
    ) -> None:
        report.new = len(listing)
        if self.dry_run:
            report.seeded = len(listing)
            return
        hard_filter = HardFilter(filters)
        scorer = JobScorer(self.config.app.scoring, filters)
        candidates: list[tuple[UnifiedJob, ScoringResult, int]] = []
        for job in listing:
            if job.budget_value is not None and job.budget_rub is None:
                await self.converter.enrich(job)
            score = scorer.score(job)
            row, created = await self.repo.upsert_job(job, score)
            if job.status in ("closed", "in_progress"):
                await self.repo.set_job_hidden(row.id, True)
            elif created:
                # Silently mark as seen: no push, no morning-digest spam.
                await self.repo.mark_seeded(row.id)
            if hard_filter.check(job).passed and score.total_score >= filters.min_notify_score:
                candidates.append((job, score, row.id))
        report.seeded = len(listing)

        top_n = max(0, filters.send_top_n_on_start)
        if top_n and self.notifier is not None and not self._paused and not filters.quiet_hours.is_active(tz_name=self.timezone):
            fresh = sorted(
                candidates,
                key=lambda item: (item[0].published_at or utcnow(), item[1].total_score),
                reverse=True,
            )[:top_n]
            for job, score, row_id in fresh:
                await self.notifier.submit(job, score, row_id, self.chat_ids)
                report.notified += 1

    async def _process_listing(
        self,
        adapter: BaseSourceAdapter,
        listing: list[UnifiedJob],
        filters: UserFilters,
        report: ScanReport,
    ) -> None:
        known = await self.repo.known_external_ids(adapter.name, [job.external_id for job in listing])
        new_jobs = [job for job in listing if job.external_id not in known]
        report.new = len(new_jobs)
        if not new_jobs:
            return

        hard_filter = HardFilter(filters)
        scorer = JobScorer(self.config.app.scoring, filters)
        recent_rows = [] if self.dry_run else await self.repo.recent_jobs(hours=self._dedupe.window_hours)
        candidates = [row.to_unified() for row in recent_rows]
        rows_by_key = {(row.source, row.external_id): row for row in recent_rows}

        for job in new_jobs:
            # Cheap listing pre-check: skip Phase 2 detail HTTP calls when listing
            # already clearly fails by status, age, proposals, budget, or exclude words.
            if adapter.needs_details(job) and not self._clearly_rejected_in_listing(hard_filter, job):
                try:
                    async with self._semaphore:
                        job = await adapter.fetch_details(job)
                except SourceError as exc:
                    logger.debug(f"{adapter.name}: детали недоступны для {job.external_id}: {exc}")
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug(f"{adapter.name}: детали упали для {job.external_id}: {exc}")

            if job.budget_value is not None and job.budget_rub is None:
                await self.converter.enrich(job)

            decision = hard_filter.check(job)
            if not decision.passed:
                report.filtered += 1
                if self.dry_run:
                    report.details.append(self._detail(job, None, decision.reason))
                else:
                    row, created = await self.repo.upsert_job(job)
                    if created:
                        await self.repo.set_job_hidden(row.id, True)
                continue

            score = scorer.score(job)
            if self.llm.enabled:
                score = await self.llm.enrich(job, score)
            report.scored += 1

            duplicate = self._dedupe.find_duplicate(job, candidates)
            if duplicate is not None:
                report.duplicates += 1
                if self.dry_run:
                    report.details.append(
                        self._detail(job, score, f"дубль {duplicate.source} ({duplicate.similarity:.2f})")
                    )
                    continue
                row, created = await self.repo.upsert_job(job, score)
                if created:
                    await self.repo.set_job_hidden(row.id, True)
                existing = rows_by_key.get((duplicate.source, duplicate.external_id))
                if existing is not None:
                    await self.repo.add_cross_post(
                        existing.id, job.source, job.external_id, job.url, duplicate.similarity
                    )
                    if (
                        existing.notified_at is not None
                        and self.notifier is not None
                        and hasattr(self.notifier, "refresh_card")
                    ):
                        posts = await self.repo.cross_posts_for(existing.id)
                        notifications = await self.repo.notifications_for_job(existing.id)
                        cross_names = [p.source for p in posts]
                        for notification in notifications:
                            await self.notifier.refresh_card(
                                notification, existing, cross_posts=cross_names
                            )
                logger.info(
                    f"{job.source}/{job.external_id}: кросс-дубль {duplicate.source} "
                    f"({duplicate.similarity:.2f}) - без уведомления"
                )
                continue

            if score.total_score < filters.min_notify_score:
                report.below_threshold += 1
                if self.dry_run:
                    report.details.append(
                        self._detail(job, score, f"скор {score.total_score} < {filters.min_notify_score}")
                    )
                else:
                    await self.repo.upsert_job(job, score)
                continue

            if self.dry_run:
                report.details.append(self._detail(job, score, "к отправке"))
                continue

            row, created = await self.repo.upsert_job(job, score)
            candidates.append(job)
            rows_by_key[(job.source, job.external_id)] = row
            if not created:
                continue
            if self._paused or filters.quiet_hours.is_active(tz_name=self.timezone):
                report.deferred += 1
                continue
            if self.notifier is not None:
                await self.notifier.submit(job, score, row.id, self.chat_ids)
                report.notified += 1

    @staticmethod
    def _clearly_rejected_in_listing(hard_filter: HardFilter, job: UnifiedJob) -> bool:
        """Fast pre-check before Phase 2 HTTP detail requests."""

        decision = hard_filter.check(job)
        if decision.passed:
            return False
        reason = decision.reason
        # Only allow Phase 2 to rescue jobs that failed due to missing description/include
        # or missing budget/proposals that detail page might populate.
        if reason.startswith("нет ни одного ключевого слова"):
            return False
        if "неизвестно" in reason or "договорная" in reason:
            return False
        return True

    def _detail(
        self, job: UnifiedJob, score: ScoringResult | None, note: str
    ) -> dict[str, Any]:
        return {
            "source": job.source,
            "title": job.title,
            "url": job.url,
            "score": None if score is None else score.total_score,
            "priority": None if score is None else score.priority,
            "budget": job.budget_rub,
            "currency": job.currency,
            "proposals": job.proposals_count,
            "status": job.status,
            "note": note,
            "reasons": [] if score is None else score.reasons,
        }

    # --------------------------------------------------------------------- loops
    async def run_forever(self) -> None:
        await self.initialize()
        loop = asyncio.get_running_loop()
        for adapter in self.adapters:
            self._next_run[adapter.name] = loop.time()
        logger.info(f"Движок запущен: {len(self.adapters)} источников")
        while not self._stop.is_set():
            try:
                if self.config.maybe_reload():
                    self.rebuild_adapters()
                    logger.info("Конфигурация перечитана")
                now = loop.time()
                due: list[str] = []
                for adapter in self.adapters:
                    if not adapter.config.enabled:
                        continue
                    if self._next_run.get(adapter.name, 0.0) <= now:
                        due.append(adapter.name)
                if due:
                    await self.scan_once(due)
                    now = loop.time()
                    for adapter in self.adapters:
                        if adapter.name in due:
                            self._next_run[adapter.name] = now + max(15, adapter.interval)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - keep the loop alive
                logger.exception(f"Ошибка основного цикла движка: {exc}")
                await asyncio.sleep(5)

    async def live_updates_once(self) -> int:
        """Re-check recently sent cards and quiet-edit stale ones."""

        settings = self.config.app.engine
        offsets = settings.live_update_offsets_minutes
        if not offsets or self._paused:
            return 0
        due = await self.repo.due_notifications(offsets, limit=30)
        if not due:
            return 0
        updated = 0
        filters = self.filters()
        now = utcnow()
        for notification, job_row in due:
            stage = notification.check_stage
            if stage >= len(offsets):
                continue
            sent_at = as_utc(notification.sent_at) or now
            if (now - sent_at) < timedelta(minutes=offsets[stage]):
                continue
            reason = await self._stale_reason(job_row, filters, settings)
            if reason and self.notifier is not None and notification.message_id:
                posts = await self.repo.cross_posts_for(job_row.id)
                await self.notifier.mark_stale(
                    notification, job_row, reason, cross_posts=[p.source for p in posts]
                )
                await self.repo.advance_notification(
                    notification.id, stage + 1, status="stale", stale_reason=reason
                )
            else:
                await self.repo.advance_notification(notification.id, stage + 1)
            updated += 1
        return updated

    async def _stale_reason(
        self,
        job_row: Job,
        filters: UserFilters,
        settings: Any,
    ) -> str | None:
        adapter = self.get_adapter(job_row.source)
        if adapter is not None:
            unified = job_row.to_unified()
            if adapter.needs_details(unified) or adapter.kind in {"kwork", "fl_ru", "habr_freelance", "freelance_ru"}:
                try:
                    refreshed = await adapter.fetch_details(unified)
                    changed = False
                    if refreshed.proposals_count is not None and refreshed.proposals_count != job_row.proposals_count:
                        job_row = await self.repo.update_job(job_row.id, proposals_count=refreshed.proposals_count) or job_row
                        changed = True
                    if refreshed.status != job_row.status:
                        job_row = await self.repo.update_job(job_row.id, status=refreshed.status) or job_row
                        changed = True
                    if changed:
                        logger.debug(f"Live: {job_row.source}/{job_row.external_id} обновлён")
                except SourceError as exc:
                    logger.debug(f"Live: не удалось проверить {job_row.source}/{job_row.external_id}: {exc}")

        if job_row.status in ("closed", "in_progress"):
            return "заказ закрыт" if job_row.status == "closed" else "заказ уже в работе"
        proposals = job_row.proposals_count
        if proposals is not None:
            threshold = max(
                int(filters.max_proposals * settings.live_stale_proposals_factor),
                int(settings.live_stale_proposals_absolute),
            )
            if proposals >= threshold:
                return f"много откликов ({proposals})"
        return None

    async def maybe_cleanup_old_jobs(self, days: int = 30) -> int:
        """Once per local day prune old hidden noise jobs that left active feeds."""

        today = local_now(self.timezone).date().isoformat()
        if await self.repo.get_setting("cleanup_date") == today:
            return 0
        removed = await self.repo.cleanup_old_jobs(days=days)
        await self.repo.set_setting("cleanup_date", today)
        if removed:
            logger.info(f"Очистка БД: удалено {removed} старых скрытых заказов (>{days} дн.)")
        return removed

    async def digest_tick(self) -> int:
        """Send the morning digest once after quiet hours end (in user's timezone)."""

        await self.maybe_cleanup_old_jobs()
        if self._paused:
            return 0
        filters = self.filters()
        quiet = filters.quiet_hours
        local = local_now(self.timezone)
        if not quiet.enabled or quiet.is_active(now=local, tz_name=self.timezone):
            return 0
        end_hour, end_minute = (int(part) for part in quiet.end.split(":"))
        window_start = local.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
        if local < window_start or (local - window_start) > timedelta(hours=2):
            return 0
        today = local.date().isoformat()
        if await self.repo.get_setting("digest_sent_date") == today:
            return 0
        jobs = await self.repo.digest_candidates(
            limit=10,
            max_proposals=filters.max_proposals,
            min_score=filters.min_notify_score,
            exclude_sources=filters.exclude_sources,
        )
        await self.repo.set_setting("digest_sent_date", today)
        if not jobs or self.notifier is None:
            return 0
        await self.notifier.send_digest(jobs)
        logger.info(f"Дайджест отправлен: {len(jobs)} заказов")
        return len(jobs)

    async def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        await self.http.aclose()
        if self._browser is not None:
            await self._browser.aclose()
        await self.llm.aclose()
        await self.converter.aclose()

    # ------------------------------------------------------------------- actions
    async def refresh_job(self, job_id: int) -> tuple[Job | None, str]:
        row = await self.repo.get_job(job_id)
        if row is None:
            return None, "Заказ не найден в базе"
        adapter = self.get_adapter(row.source)
        if adapter is None:
            return row, "Адаптер источника недоступен"
        unified = row.to_unified()
        before = (row.proposals_count, row.status)
        try:
            refreshed = await adapter.fetch_details(unified)
        except SourceError as exc:
            return row, f"Не удалось обновить: {exc}"
        except Exception as exc:  # pragma: no cover - defensive
            return row, f"Ошибка обновления: {exc}"
        new_desc = (
            refreshed.description
            if refreshed.description and len(refreshed.description) > len(row.description or "")
            else None
        )
        row = await self.repo.update_job(
            job_id,
            proposals_count=refreshed.proposals_count,
            status=refreshed.status if refreshed.status != "unknown" else None,
            description=new_desc,
        ) or row
        if (row.proposals_count, row.status) != before:
            return row, "Данные обновлены"
        if not adapter.config.params.get("fetch_details", True) and adapter.kind not in {"kwork", "fl_ru", "freelance_ru"}:
            return row, "Источник не отдаёт детальные данные"
        return row, "Актуально - изменений нет"

    async def probe_source(self, name: str) -> tuple[bool, str]:
        adapter = self.get_adapter(name)
        if adapter is None:
            return False, "Источник не найден"
        try:
            count = await adapter.health_probe()
        except SourceError as exc:
            await self.repo.record_source_failure(
                name, adapter.kind, exc.reason if exc.reason in {"blocked", "rate_limited", "parse_error"} else "error",
                str(exc), enabled=adapter.config.enabled,
            )
            return False, str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return False, f"Ошибка проверки: {exc}"
        await self.repo.record_source_success(name, adapter.kind, adapter.config.enabled)
        return True, f"OK, получено {count} заказов"

