"""SQLAlchemy 2.0 ORM tables."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.models import UnifiedJob, as_utc, utcnow


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_jobs_source_external"),
        Index("ix_jobs_published_at", "published_at"),
        Index("ix_jobs_score", "score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(191))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    budget_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    budget_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    budget_type: Mapped[str] = mapped_column(String(16), default="negotiable")
    budget_rub: Mapped[float | None] = mapped_column(Float, nullable=True)
    budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    proposals_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    client_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    client_reviews: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_hire_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    client_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    category: Mapped[str | None] = mapped_column(String(160), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    base_score: Mapped[float] = mapped_column(Float, default=0.0)
    llm_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_components: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    score_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    priority: Mapped[str] = mapped_column(String(8), default="ok")
    summary_ru: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_proposal: Mapped[str | None] = mapped_column(Text, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)

    def to_unified(self) -> UnifiedJob:
        return UnifiedJob(
            source=self.source,
            external_id=self.external_id,
            url=self.url,
            title=self.title,
            description=self.description or "",
            budget_min=self.budget_min,
            budget_max=self.budget_max,
            currency=self.currency,
            budget_type=self.budget_type,  # type: ignore[arg-type]
            budget_rub=self.budget_rub,
            budget_usd=self.budget_usd,
            proposals_count=self.proposals_count,
            status=self.status,  # type: ignore[arg-type]
            published_at=as_utc(self.published_at),
            fetched_at=as_utc(self.fetched_at) or utcnow(),
            client_rating=self.client_rating,
            client_reviews=self.client_reviews,
            client_hire_rate=self.client_hire_rate,
            client_verified=self.client_verified,
            skills=list(self.skills or []),
            category=self.category,
            extra=dict(self.extra or {}),
        )


class CrossPost(Base):
    __tablename__ = "cross_posts"
    __table_args__ = (
        UniqueConstraint("job_id", "source", "external_id", name="uq_cross_posts_job_source_ext"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(String(191), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    similarity: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceState(Base):
    __tablename__ = "sources_state"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    state: Mapped[str] = mapped_column(String(16), default="ok")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_sent_at", "sent_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="sent")
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    check_stage: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="null")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (Index("ix_feedback_job_id", "job_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    value: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
