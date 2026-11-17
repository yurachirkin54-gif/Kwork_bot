"""Shared Pydantic schemas used across the whole application."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

JobStatus = Literal["open", "in_progress", "closed", "unknown"]
BudgetType = Literal["fixed", "hourly", "negotiable"]
SourceState = Literal["ok", "rate_limited", "blocked", "parse_error", "error", "cooldown", "disabled"]
Priority = Literal["top", "good", "ok"]
RiskLevel = Literal["low", "medium", "high"]


def utcnow() -> datetime:
    """Time-zone aware current UTC time (single source of truth)."""

    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to naive datetimes coming from parsers."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class LLMAnalysis(BaseModel):
    """Strict schema of the optional LLM enrichment step."""

    model_config = ConfigDict(extra="ignore")

    easiness_score: int = Field(ge=1, le=10)
    price_fairness_score: int = Field(ge=1, le=10)
    clarity_score: int = Field(ge=1, le=10)
    scam_or_toxic_risk: RiskLevel = "low"
    estimated_time: str = "не определено"
    summary_ru: str = ""
    red_flags: list[str] = Field(default_factory=list)
    draft_proposal: str = ""

    @field_validator("estimated_time", "summary_ru", "draft_proposal", mode="before")
    @classmethod
    def _coerce_str(cls, value: Any) -> str:
        return "" if value is None else str(value)

    @field_validator("red_flags", mode="before")
    @classmethod
    def _coerce_flags(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return [str(item) for item in value if str(item).strip()]

    @field_validator("scam_or_toxic_risk", mode="before")
    @classmethod
    def _coerce_risk(cls, value: Any) -> str:
        text = str(value or "low").strip().lower()
        return text if text in {"low", "medium", "high"} else "low"


class UnifiedJob(BaseModel):
    """Normalized job post shared by every adapter."""

    model_config = ConfigDict(extra="ignore")

    source: str
    external_id: str
    url: str
    title: str
    description: str = ""
    budget_min: float | None = None
    budget_max: float | None = None
    currency: str = "RUB"
    budget_type: BudgetType = "negotiable"
    budget_rub: float | None = None
    budget_usd: float | None = None
    proposals_count: int | None = None
    status: JobStatus = "unknown"
    published_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=utcnow)
    client_rating: float | None = None
    client_reviews: int | None = None
    client_hire_rate: float | None = None
    client_verified: bool | None = None
    skills: list[str] = Field(default_factory=list)
    category: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, value: Any) -> str:
        text = str(value or "RUB").strip().upper()
        aliases = {"RUR": "RUB", "РУБ": "RUB", "₽": "RUB", "$": "USD", "€": "EUR", "ГРН": "UAH", "UAH": "UAH"}
        return aliases.get(text, text or "RUB")

    @field_validator("skills", mode="before")
    @classmethod
    def _normalize_skills(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            separators = [",", ";", "|"]
            parts = [value]
            for sep in separators:
                parts = [chunk for part in parts for chunk in part.split(sep)]
            return [p.strip() for p in parts if p.strip()]
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator("budget_min", "budget_max", mode="before")
    @classmethod
    def _coerce_numbers(cls, value: Any) -> float | None:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            return None if value <= 0 else float(value)
        text = str(value).replace("\u00a0", "").replace(" ", "").replace(",", ".")
        digits = "".join(ch for ch in text if ch.isdigit() or ch == ".")
        try:
            number = float(digits)
        except ValueError:
            return None
        return number if number > 0 else None

    @model_validator(mode="after")
    def _fill_budget_bounds(self) -> UnifiedJob:
        if self.budget_min is None and self.budget_max is not None:
            self.budget_min = self.budget_max
        if self.budget_max is None and self.budget_min is not None:
            self.budget_max = self.budget_min
        if self.budget_min is not None and self.budget_max is not None and self.budget_min > self.budget_max:
            self.budget_min, self.budget_max = self.budget_max, self.budget_min
        return self

    @property
    def key(self) -> tuple[str, str]:
        return self.source, self.external_id

    @property
    def has_budget(self) -> bool:
        return self.budget_rub is not None or self.budget_min is not None or self.budget_max is not None

    @property
    def budget_value(self) -> float | None:
        """Largest known amount - used for display and scoring."""

        if self.budget_max is not None:
            return self.budget_max
        return self.budget_min

    def age_minutes(self, now: datetime | None = None) -> float | None:
        if self.published_at is None:
            return None
        reference = now or utcnow()
        published = as_utc(self.published_at)
        if published is None:
            return None
        return max(0.0, (reference - published).total_seconds() / 60.0)

    def text_blob(self) -> str:
        parts = [self.title, self.description, " ".join(self.skills), self.category or ""]
        return " ".join(p for p in parts if p).lower()


class FilterDecision(BaseModel):
    """Result of the hard filter stage."""

    passed: bool
    reason: str = ""


class ScoreComponents(BaseModel):
    """Normalized (0..1) scores per criterion before weighting."""

    freshness: float = 0.0
    competition: float = 0.0
    budget: float = 0.0
    client: float = 0.0
    text: float = 0.0


class ScoringResult(BaseModel):
    """Deterministic score plus optional LLM enrichment."""

    base_score: float = 0.0
    llm_score: float | None = None
    total_score: float = 0.0
    components: ScoreComponents = Field(default_factory=ScoreComponents)
    reasons: list[str] = Field(default_factory=list)
    priority: Priority = "ok"
    llm: LLMAnalysis | None = None

    def recalculate_total(self) -> float:
        """Keep total in sync when only the base half is available."""

        self.total_score = round(self.llm_score if self.llm_score is not None else self.base_score, 1)
        self.priority = priority_for(self.total_score)
        return self.total_score


def priority_for(score: float) -> Priority:
    if score >= 75:
        return "top"
    if score >= 55:
        return "good"
    return "ok"


class QuietHours(BaseModel):
    """Quiet hours window, supports windows crossing midnight."""

    enabled: bool = False
    start: str = "00:00"
    end: str = "08:00"

    @field_validator("start", "end")
    @classmethod
    def _validate_time(cls, value: str) -> str:
        text = str(value).strip()
        parts = text.split(":")
        if len(parts) != 2:
            raise ValueError("time must be HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("time must be HH:MM")
        return f"{hour:02d}:{minute:02d}"

    @staticmethod
    def _parse(value: str) -> time:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour=hour, minute=minute)

    def is_active(self, now: datetime | None = None) -> bool:
        if not self.enabled:
            return False
        reference = now or utcnow()
        start = self._parse(self.start)
        end = self._parse(self.end)
        current = reference.timetz().replace(tzinfo=None)
        if start == end:
            return True
        if start < end:
            return start <= current < end
        return current >= start or current < end


DEFAULT_EXCLUDE_KEYWORDS: list[str] = [
    "казино",
    "casino",
    "ставки",
    "ставок",
    "букмекер",
    "за отзыв",
    "за отзывы",
    "без оплаты",
    "безвозмездно",
    "бесплатно",
    "18+",
    "интим",
    "дропы",
    "обнал",
    "скам",
    "за 50 рублей",
]


class UserFilters(BaseModel):
    """User-controlled filters. Persisted as DB overrides on top of settings.yaml."""

    model_config = ConfigDict(extra="ignore")

    min_budget_rub: float = 1500.0
    min_budget_usd: float = 20.0
    max_proposals: int = 5
    allow_unknown_proposals: bool = True
    min_notify_score: float = 55.0
    allow_negotiable: bool = True
    max_age_minutes: int = 1440
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDE_KEYWORDS))
    include_regex: list[str] = Field(default_factory=list)
    exclude_regex: list[str] = Field(default_factory=list)
    exclude_sources: list[str] = Field(default_factory=list)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    cold_start_mode: Literal["silent_seed", "notify_all"] = "silent_seed"
    send_top_n_on_start: int = 0

    @field_validator("include_keywords", "exclude_keywords", "include_regex", "exclude_regex", "exclude_sources", mode="before")
    @classmethod
    def _normalize_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [chunk.strip() for chunk in value.split(",") if chunk.strip()]
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator("max_proposals", mode="before")
    @classmethod
    def _clamp_proposals(cls, value: Any) -> int:
        return max(0, int(value))

    @field_validator("max_age_minutes", mode="before")
    @classmethod
    def _clamp_age(cls, value: Any) -> int:
        return max(1, int(value))


class SourceStatus(BaseModel):
    """Health snapshot of one adapter."""

    name: str
    kind: str = ""
    enabled: bool = True
    state: SourceState = "ok"
    consecutive_failures: int = 0
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_error: str | None = None
    cooldown_until: datetime | None = None
    jobs_24h: int = 0
    notes: str = ""

    def cooldown_remaining_seconds(self, now: datetime | None = None) -> int:
        if self.cooldown_until is None:
            return 0
        reference = now or utcnow()
        remaining = (as_utc(self.cooldown_until) - reference).total_seconds()
        return max(0, int(remaining))


class JobWithScore(BaseModel):
    """Convenience pair used by the notifier and live updater."""

    job: UnifiedJob
    score: ScoringResult
    db_id: int | None = None


def minutes_ago_label(minutes: float | None) -> str:
    if minutes is None:
        return "время н/д"
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{int(minutes)} мин назад"
    if minutes < 60 * 24:
        hours = int(minutes // 60)
        return f"{hours} ч назад"
    return f"{int(minutes // (60 * 24))} дн назад"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} сек"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {sec} сек"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин"


def fresh_cutoff(minutes: int, now: datetime | None = None) -> datetime:
    return (now or utcnow()) - timedelta(minutes=minutes)
