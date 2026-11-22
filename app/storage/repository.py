"""Repository: all database access for jobs, cross posts, states, settings, feedback."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    ScoringResult,
    SourceStatus,
    UnifiedJob,
    as_utc,
    utcnow,
)
from app.storage.orm import CrossPost, Feedback, Job, Notification, Setting, SourceState


class Repository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def _session(self) -> AsyncSession:
        return self._session_factory()

    # -------------------------------------------------------------------- jobs
    async def known_external_ids(self, source: str, ids: Iterable[str]) -> set[str]:
        values = [str(item) for item in ids]
        if not values:
            return set()
        found: set[str] = set()
        async with self._session() as session:
            for chunk_start in range(0, len(values), 400):
                chunk = values[chunk_start : chunk_start + 400]
                rows = await session.execute(
                    select(Job.external_id).where(Job.source == source, Job.external_id.in_(chunk))
                )
                found.update(row[0] for row in rows)
        return found

    async def job_exists(self, source: str, external_id: str) -> bool:
        async with self._session() as session:
            row = await session.execute(
                select(Job.id).where(Job.source == source, Job.external_id == str(external_id)).limit(1)
            )
            return row.scalar_one_or_none() is not None

    async def get_job(self, job_id: int) -> Job | None:
        async with self._session() as session:
            return await session.get(Job, job_id)

    async def get_job_by_key(self, source: str, external_id: str) -> Job | None:
        async with self._session() as session:
            row = await session.execute(
                select(Job).where(Job.source == source, Job.external_id == str(external_id)).limit(1)
            )
            return row.scalar_one_or_none()

    async def upsert_job(self, job: UnifiedJob, score: ScoringResult | None = None) -> tuple[Job, bool]:
        now = utcnow()
        async with self._session() as session:
            row = await session.execute(
                select(Job).where(Job.source == job.source, Job.external_id == job.external_id).limit(1)
            )
            existing = row.scalar_one_or_none()
            if existing is not None:
                existing.proposals_count = (
                    job.proposals_count if job.proposals_count is not None else existing.proposals_count
                )
                if job.status != "unknown":
                    existing.status = job.status
                existing.last_seen_at = now
                if job.description and len(job.description) > len(existing.description or ""):
                    existing.description = job.description
                if job.budget_rub is not None:
                    existing.budget_rub = job.budget_rub
                    existing.budget_usd = job.budget_usd
                if score is not None:
                    self._apply_score(existing, score)
                await session.commit()
                return existing, False

            existing = Job(
                source=job.source,
                external_id=job.external_id,
                url=job.url,
                title=job.title,
                description=job.description,
                budget_min=job.budget_min,
                budget_max=job.budget_max,
                currency=job.currency,
                budget_type=job.budget_type,
                budget_rub=job.budget_rub,
                budget_usd=job.budget_usd,
                proposals_count=job.proposals_count,
                status=job.status,
                published_at=job.published_at,
                fetched_at=job.fetched_at or now,
                first_seen_at=now,
                last_seen_at=now,
                client_rating=job.client_rating,
                client_reviews=job.client_reviews,
                client_hire_rate=job.client_hire_rate,
                client_verified=job.client_verified,
                skills=list(job.skills),
                category=job.category,
                extra=dict(job.extra),
            )
            if score is not None:
                self._apply_score(existing, score)
            session.add(existing)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                row = await session.execute(
                    select(Job).where(Job.source == job.source, Job.external_id == job.external_id).limit(1)
                )
                duplicate = row.scalar_one()
                return duplicate, False
            await session.refresh(existing)
            return existing, True

    @staticmethod
    def _apply_score(row: Job, score: ScoringResult) -> None:
        row.score = score.total_score
        row.base_score = score.base_score
        row.llm_score = score.llm_score
        row.priority = score.priority
        row.score_components = score.components.model_dump()
        row.score_reasons = list(score.reasons)
        if score.llm is not None:
            row.summary_ru = score.llm.summary_ru or row.summary_ru
            if score.llm.draft_proposal:
                row.draft_proposal = score.llm.draft_proposal

    async def update_job(
        self,
        job_id: int,
        *,
        proposals_count: int | None = None,
        status: str | None = None,
        description: str | None = None,
    ) -> Job | None:
        async with self._session() as session:
            row = await session.get(Job, job_id)
            if row is None:
                return None
            if proposals_count is not None:
                row.proposals_count = proposals_count
            if status is not None:
                row.status = status
            if description:
                row.description = description[:12000]
            row.last_seen_at = utcnow()
            await session.commit()
            return row

    async def recent_jobs(self, hours: int = 48, limit: int = 500) -> list[Job]:
        since = utcnow() - timedelta(hours=hours)
        async with self._session() as session:
            rows = await session.execute(
                select(Job).where(Job.first_seen_at >= since).order_by(Job.first_seen_at.desc()).limit(limit)
            )
            return list(rows.scalars())

    async def count_jobs(self) -> int:
        async with self._session() as session:
            result = await session.execute(select(func.count(Job.id)))
            return int(result.scalar_one())

    async def mark_notified(
        self, job_id: int, chat_id: int, message_id: int | None, score: float
    ) -> Notification:
        async with self._session() as session:
            row = await session.get(Job, job_id)
            if row is not None:
                row.notified_at = utcnow()
            notification = Notification(
                job_id=job_id,
                chat_id=chat_id,
                message_id=message_id,
                score=score,
                status="sent",
            )
            session.add(notification)
            await session.commit()
            await session.refresh(notification)
            return notification

    async def mark_seeded(self, job_id: int) -> None:
        """Mark a job as already known without creating a notification (cold start)."""

        async with self._session() as session:
            row = await session.get(Job, job_id)
            if row is not None and row.notified_at is None:
                row.notified_at = utcnow()
                await session.commit()

    async def set_job_hidden(self, job_id: int, hidden: bool = True) -> bool:
        async with self._session() as session:
            row = await session.get(Job, job_id)
            if row is None:
                return False
            row.is_hidden = hidden
            await session.commit()
            return True

    async def set_draft_proposal(self, job_id: int, proposal: str) -> bool:
        async with self._session() as session:
            row = await session.get(Job, job_id)
            if row is None:
                return False
            row.draft_proposal = proposal
            await session.commit()
            return True

    # -------------------------------------------------------------- cross posts
    async def add_cross_post(
        self,
        job_id: int,
        source: str,
        external_id: str,
        url: str,
        similarity: float,
    ) -> None:
        async with self._session() as session:
            session.add(
                CrossPost(
                    job_id=job_id,
                    source=source,
                    external_id=external_id,
                    url=url,
                    similarity=similarity,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()

    async def cross_posts_for(self, job_id: int) -> list[CrossPost]:
        async with self._session() as session:
            rows = await session.execute(select(CrossPost).where(CrossPost.job_id == job_id))
            return list(rows.scalars())

    # ------------------------------------------------------------ source state
    async def get_source_state(self, name: str) -> SourceState | None:
        async with self._session() as session:
            return await session.get(SourceState, name)

    async def list_source_states(self) -> list[SourceState]:
        async with self._session() as session:
            rows = await session.execute(select(SourceState).order_by(SourceState.name))
            return list(rows.scalars())

    async def record_source_success(self, name: str, kind: str, enabled: bool = True) -> None:
        now = utcnow()
        async with self._session() as session:
            row = await session.get(SourceState, name)
            if row is None:
                row = SourceState(name=name)
                session.add(row)
            row.kind = kind
            row.enabled = enabled
            row.state = "ok"
            row.consecutive_failures = 0
            row.last_success_at = now
            row.last_attempt_at = now
            row.last_error = None
            row.cooldown_until = None
            await session.commit()

    async def record_source_failure(
        self,
        name: str,
        kind: str,
        state: str,
        error: str,
        cooldown_until: datetime | None = None,
        enabled: bool = True,
    ) -> None:
        now = utcnow()
        async with self._session() as session:
            row = await session.get(SourceState, name)
            if row is None:
                row = SourceState(name=name)
                session.add(row)
            row.kind = kind
            row.enabled = enabled
            row.state = state
            row.consecutive_failures = (row.consecutive_failures or 0) + 1
            row.last_attempt_at = now
            row.last_error = (error or "")[:900]
            row.cooldown_until = cooldown_until
            await session.commit()

    async def set_source_enabled(self, name: str, enabled: bool) -> None:
        async with self._session() as session:
            row = await session.get(SourceState, name)
            if row is None:
                row = SourceState(name=name, enabled=enabled)
                session.add(row)
            else:
                row.enabled = enabled
            await session.commit()

    async def jobs_per_source(self, hours: int = 24) -> dict[str, int]:
        since = utcnow() - timedelta(hours=hours)
        async with self._session() as session:
            rows = await session.execute(
                select(Job.source, func.count(Job.id))
                .where(Job.first_seen_at >= since)
                .group_by(Job.source)
            )
            return {str(source): int(count) for source, count in rows}

    async def source_statuses(self, hours: int = 24) -> list[SourceStatus]:
        states = await self.list_source_states()
        counts = await self.jobs_per_source(hours)
        result: list[SourceStatus] = []
        for state in states:
            result.append(
                SourceStatus(
                    name=state.name,
                    kind=state.kind or "",
                    enabled=bool(state.enabled),
                    state=state.state or "ok",  # type: ignore[arg-type]
                    consecutive_failures=state.consecutive_failures or 0,
                    last_success_at=as_utc(state.last_success_at),
                    last_attempt_at=as_utc(state.last_attempt_at),
                    last_error=state.last_error,
                    cooldown_until=as_utc(state.cooldown_until),
                    jobs_24h=counts.get(state.name, 0),
                )
            )
        return result

    # -------------------------------------------------------------- notifications
    async def due_notifications(
        self, offsets: Sequence[int], limit: int = 50
    ) -> list[tuple[Notification, Job]]:
        if not offsets:
            return []
        now = utcnow()
        stage_filters = [
            (Notification.check_stage == idx) & (Notification.sent_at <= now - timedelta(minutes=offset))
            for idx, offset in enumerate(offsets)
        ]
        async with self._session() as session:
            rows = await session.execute(
                select(Notification, Job)
                .join(Job, Notification.job_id == Job.id)
                .where(
                    Notification.status == "sent",
                    Notification.message_id.is_not(None),
                    Notification.check_stage < len(offsets),
                    or_(*stage_filters),
                )
                .order_by(Notification.sent_at.asc())
                .limit(limit)
            )
            return [(notification, job) for notification, job in rows.all()]

    async def advance_notification(
        self,
        notification_id: int,
        stage: int,
        *,
        status: str | None = None,
        stale_reason: str | None = None,
    ) -> None:
        async with self._session() as session:
            row = await session.get(Notification, notification_id)
            if row is None:
                return
            row.check_stage = stage
            row.checked_at = utcnow()
            if status:
                row.status = status
            if stale_reason:
                row.stale_reason = stale_reason
            await session.commit()

    async def notifications_for_job(self, job_id: int) -> list[Notification]:
        async with self._session() as session:
            rows = await session.execute(
                select(Notification)
                .where(Notification.job_id == job_id, Notification.message_id.is_not(None))
                .order_by(Notification.sent_at.desc())
            )
            return list(rows.scalars())

    async def notification_for_job(self, job_id: int) -> Notification | None:
        async with self._session() as session:
            rows = await session.execute(
                select(Notification)
                .where(Notification.job_id == job_id)
                .order_by(Notification.sent_at.desc())
                .limit(1)
            )
            return rows.scalar_one_or_none()

    # ------------------------------------------------------------------ settings
    async def get_setting(self, key: str, default: Any = None) -> Any:
        async with self._session() as session:
            row = await session.get(Setting, key)
            if row is None:
                return default
            try:
                return json.loads(row.value)
            except (TypeError, json.JSONDecodeError):
                return default

    async def set_setting(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        async with self._session() as session:
            row = await session.get(Setting, key)
            if row is None:
                session.add(Setting(key=key, value=payload))
            else:
                row.value = payload
            await session.commit()

    async def all_settings(self) -> dict[str, Any]:
        async with self._session() as session:
            rows = await session.execute(select(Setting))
            result: dict[str, Any] = {}
            for row in rows.scalars():
                try:
                    result[row.key] = json.loads(row.value)
                except (TypeError, json.JSONDecodeError):
                    result[row.key] = row.value
            return result

    async def delete_setting(self, key: str) -> None:
        async with self._session() as session:
            await session.execute(delete(Setting).where(Setting.key == key))
            await session.commit()

    # ------------------------------------------------------------------ feedback
    async def add_feedback(self, job_id: int, user_id: int, value: int, comment: str | None = None) -> None:
        async with self._session() as session:
            session.add(Feedback(job_id=job_id, user_id=user_id, value=value, comment=comment))
            await session.commit()

    async def feedback_summary(self) -> dict[str, int]:
        async with self._session() as session:
            rows = await session.execute(select(Feedback.value, func.count(Feedback.id)).group_by(Feedback.value))
            return {str(value): int(count) for value, count in rows}

    # --------------------------------------------------------------------- stats
    async def stats(self, hours: int = 24) -> dict[str, Any]:
        since = utcnow() - timedelta(hours=hours)
        async with self._session() as session:
            total = await session.execute(select(func.count(Job.id)).where(Job.first_seen_at >= since))
            notified = await session.execute(
                select(func.count(Notification.id)).where(Notification.sent_at >= since)
            )
            avg_score = await session.execute(select(func.avg(Job.score)).where(Job.first_seen_at >= since))
            by_source = await session.execute(
                select(Job.source, func.count(Job.id))
                .where(Job.first_seen_at >= since)
                .group_by(Job.source)
                .order_by(func.count(Job.id).desc())
            )
            top = await session.execute(
                select(Job).where(Job.first_seen_at >= since).order_by(Job.score.desc()).limit(5)
            )
            return {
                "hours": hours,
                "total": int(total.scalar_one() or 0),
                "notified": int(notified.scalar_one() or 0),
                "avg_score": round(float(avg_score.scalar() or 0), 1),
                "by_source": {str(source): int(count) for source, count in by_source},
                "top": [
                    {"title": row.title, "score": row.score, "source": row.source, "url": row.url}
                    for row in top.scalars()
                ],
            }

    async def digest_candidates(
        self,
        limit: int = 10,
        max_proposals: int = 5,
        *,
        min_score: float = 0.0,
        max_age_hours: int = 48,
        exclude_sources: Sequence[str] | None = None,
    ) -> list[Job]:
        since = utcnow() - timedelta(hours=max(1, max_age_hours))
        conditions = [
            Job.notified_at.is_(None),
            Job.is_hidden.is_(False),
            Job.status.in_(("open", "unknown")),
            Job.score >= min_score,
            Job.first_seen_at >= since,
            (Job.proposals_count.is_(None)) | (Job.proposals_count <= max_proposals),
        ]
        if exclude_sources:
            conditions.append(~Job.source.in_(list(exclude_sources)))
        async with self._session() as session:
            rows = await session.execute(
                select(Job)
                .where(*conditions)
                .order_by(Job.score.desc(), Job.published_at.desc())
                .limit(limit)
            )
            return list(rows.scalars())

    async def recent_top_jobs(
        self,
        *,
        limit: int = 10,
        hours: int = 24,
        min_score: float = 0.0,
        max_proposals: int | None = None,
        exclude_sources: Sequence[str] | None = None,
        only_unnotified: bool = False,
    ) -> list[Job]:
        since = utcnow() - timedelta(hours=max(1, hours))
        conditions = [
            Job.is_hidden.is_(False),
            Job.status.in_(("open", "unknown")),
            Job.first_seen_at >= since,
            Job.score >= min_score,
        ]
        if only_unnotified:
            conditions.append(Job.notified_at.is_(None))
        if max_proposals is not None:
            conditions.append((Job.proposals_count.is_(None)) | (Job.proposals_count <= max_proposals))
        if exclude_sources:
            conditions.append(~Job.source.in_(list(exclude_sources)))
        async with self._session() as session:
            rows = await session.execute(
                select(Job)
                .where(*conditions)
                .order_by(Job.score.desc(), Job.published_at.desc(), Job.id.desc())
                .limit(limit)
            )
            return list(rows.scalars())

    async def pending_count(self, min_score: float = 0.0) -> int:
        async with self._session() as session:
            rows = await session.execute(
                select(func.count(Job.id)).where(
                    Job.notified_at.is_(None),
                    Job.is_hidden.is_(False),
                    Job.status.in_(("open", "unknown")),
                    Job.score >= min_score,
                )
            )
            return int(rows.scalar_one() or 0)

    async def cleanup_old_jobs(self, days: int = 30) -> int:
        """Delete old hidden/unnotified noise jobs that have fallen off active feeds."""
        cutoff = utcnow() - timedelta(days=max(1, days))
        async with self._session() as session:
            has_notif = select(Notification.id).where(Notification.job_id == Job.id).exists()
            has_fb = select(Feedback.id).where(Feedback.job_id == Job.id).exists()
            has_cp = select(CrossPost.id).where(CrossPost.job_id == Job.id).exists()
            result = await session.execute(
                delete(Job).where(
                    Job.is_hidden.is_(True),
                    Job.notified_at.is_(None),
                    Job.first_seen_at < cutoff,
                    Job.last_seen_at < cutoff,
                    ~has_notif,
                    ~has_fb,
                    ~has_cp,
                )
            )
            await session.commit()
            return int(result.rowcount or 0)
