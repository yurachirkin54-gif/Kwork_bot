"""Stage B: deterministic 0-100 score for freshness, competition, budget, client, text."""

from __future__ import annotations

import math
import re
from datetime import datetime

from app.config import ScoringConfig
from app.models import (
    ScoreComponents,
    ScoringResult,
    UnifiedJob,
    UserFilters,
    minutes_ago_label,
    priority_for,
    utcnow,
)

RED_FLAGS: list[tuple[str, str]] = [
    (r"бесплатн|без оплаты|безвозмездно", "работа без оплаты"),
    (r"за отзыв|за отзывы|взамен на отзыв", "оплата отзывом"),
    (r"за процент от продаж|за % от продаж|только процент", "оплата процентом"),
    (r"тестовое задание бесплатно|бесплатное тестовое", "бесплатное тестовое"),
    (r"оплата после|оплачу после|по факту сдачи", "оплата после сдачи"),
    (r"срочно на вчера|нужно вчера|дедлайн вчера", "невозможный дедлайн"),
    (r"сделать всё под ключ за|под ключ за \d{3,4}\s*(?:р|руб)", "демпинг «под ключ»"),
]

CODE_SIGNALS = (
    "парсер",
    "бот",
    "чат-бот",
    "python",
    "api",
    "скрипт",
    "автоматизац",
    "сайт",
    "лендинг",
    "доработ",
    "правк",
    "проверк",
    "тестир",
    "аудит",
    "баг",
    "верстк",
    "вёрстк",
    "wordpress",
    "tilda",
    "тильд",
    "n8n",
    "telegram",
    "телеграм",
)

AI_CREATIVE_SIGNALS = (
    "логотип",
    "лого",
    "баннер",
    "обложк",
    "инфографик",
    "иллюстрац",
    "превью",
    "презентац",
    "креатив",
    "видеоролик",
    "видео",
    "ролик",
    "reels",
    "рилс",
    "shorts",
    "анимац",
    "интро",
    "veo",
    "нейросет",
    "gpt",
    "ии",
)

EASY_SIGNALS = [*CODE_SIGNALS, *AI_CREATIVE_SIGNALS, "шаблон", "тз", "техническое задание", "требовани"]

CLARITY_SIGNALS = [
    "тз",
    "требован",
    "функционал",
    "стек",
    "срок",
    "бюджет",
    "api",
    "сценари",
]


def _compile_signal(sig: str) -> re.Pattern[str]:
    esc = re.escape(sig)
    if len(sig) <= 3:
        return re.compile(rf"(?<!\w){esc}(?:[аыуеио]|ов|ам|ами|ах|ом|s|es)?(?!\w)", re.IGNORECASE | re.UNICODE)
    return re.compile(rf"(?<!\w){esc}", re.IGNORECASE | re.UNICODE)


_CODE_PATTERNS = [_compile_signal(s) for s in CODE_SIGNALS]
_CREATIVE_PATTERNS = [_compile_signal(s) for s in AI_CREATIVE_SIGNALS]
_EASY_PATTERNS = [_compile_signal(s) for s in EASY_SIGNALS]
_CLARITY_PATTERNS = [_compile_signal(s) for s in CLARITY_SIGNALS]


class JobScorer:
    """Weighted deterministic scorer. Never raises on partial data."""

    def __init__(self, scoring: ScoringConfig, filters: UserFilters) -> None:
        self.scoring = scoring
        self.filters = filters
        self.weights = scoring.weights

    def score(self, job: UnifiedJob, now: datetime | None = None) -> ScoringResult:
        reference = now or utcnow()
        components = ScoreComponents(
            freshness=self._freshness(job, reference),
            competition=self._competition(job),
            budget=self._budget(job),
            client=self._client(job),
            text=self._text(job),
        )
        total = (
            components.freshness * self.weights.freshness
            + components.competition * self.weights.competition
            + components.budget * self.weights.budget
            + components.client * self.weights.client
            + components.text * self.weights.text
        )
        total = round(max(0.0, min(100.0, total)), 1)
        reasons = self._reasons(job, components, reference)
        return ScoringResult(
            base_score=total,
            total_score=total,
            components=components,
            reasons=reasons,
            priority=priority_for(total),
        )

    # ---------------------------------------------------------------- criteria
    def _freshness(self, job: UnifiedJob, now: datetime) -> float:
        age = job.age_minutes(now)
        if age is None:
            return 0.45
        window = max(1, self.scoring.fresh_window_minutes)
        if age <= window:
            return 1.0
        if age <= window * 2:
            return 0.85
        if age <= 60:
            return 0.68
        if age <= 180:
            return 0.42
        if age <= 720:
            return 0.18
        if age <= 1440:
            return 0.07
        return 0.0

    def _competition(self, job: UnifiedJob) -> float:
        count = job.proposals_count
        if count is None:
            return 0.55
        if count <= 0:
            return 1.0
        if count == 1:
            return 0.92
        if count == 2:
            return 0.78
        if count == 3:
            return 0.6
        if count == 4:
            return 0.43
        if count == 5:
            return 0.28
        return max(0.03, 0.28 - 0.04 * (count - 5))

    def _budget(self, job: UnifiedJob) -> float:
        if job.budget_type == "negotiable" and job.budget_rub is None:
            return 0.32
        amount = job.budget_rub
        if amount is None and job.budget_value is not None:
            amount = job.budget_value if job.currency == "RUB" else None
        if amount is None:
            if job.budget_usd is not None:
                amount = job.budget_usd * 92.0
            else:
                return 0.32
        threshold = max(1.0, self.filters.min_budget_rub)
        ratio = amount / threshold
        if ratio < 1:
            return 0.2
        if ratio < 1.5:
            return 0.4
        if ratio < 2.5:
            return 0.58
        if ratio < 4:
            return 0.7
        if ratio < 10:
            return 0.85
        if ratio < 30:
            return 0.95
        if ratio < 100:
            return 0.8
        return 0.6

    def _client(self, job: UnifiedJob) -> float:
        score = 0.35
        if job.client_verified:
            score += 0.25
        rating = job.client_rating
        if rating is not None:
            if rating >= 4.5:
                score += 0.2
            elif rating >= 4.0:
                score += 0.12
            elif rating >= 3.0:
                score += 0.05
            elif rating < 2.5:
                score -= 0.1
        if job.client_hire_rate is not None:
            if job.client_hire_rate >= 0.7:
                score += 0.13
            elif job.client_hire_rate >= 0.5:
                score += 0.08
            elif job.client_hire_rate < 0.2:
                score -= 0.05
        if job.client_reviews is not None:
            if job.client_reviews >= 20:
                score += 0.1
            elif job.client_reviews >= 5:
                score += 0.05
        return max(0.0, min(1.0, score))

    def _text(self, job: UnifiedJob) -> float:
        description = (job.description or "").strip()
        length = len(description)
        score = 0.5
        if length >= 600:
            score += 0.18
        elif length >= 300:
            score += 0.14
        elif length >= 120:
            score += 0.08
        elif length < 40:
            score -= 0.18

        lowered = job.text_blob()
        easy_hits = sum(1 for pat in _EASY_PATTERNS if pat.search(lowered))
        score += min(0.25, easy_hits * 0.06)

        clarity_hits = sum(1 for pat in _CLARITY_PATTERNS if pat.search(lowered))
        score += min(0.12, clarity_hits * 0.03)

        if any(heavy in lowered for heavy in HARD_SIGNALS):
            score -= 0.15

        if re.search(r"(\n\s*[-*\u2022]|\d+\.)", description):
            score += 0.04

        for pattern, _label in RED_FLAGS:
            if re.search(pattern, lowered):
                score -= 0.22

        return max(0.0, min(1.0, score))

    # ----------------------------------------------------------------- reasons
    def _reasons(self, job: UnifiedJob, components: ScoreComponents, now: datetime) -> list[str]:
        reasons: list[str] = []
        age_label = minutes_ago_label(job.age_minutes(now))
        if components.freshness >= 0.85:
            reasons.append(f"свежий заказ ({age_label})")
        if job.proposals_count is None:
            reasons.append("отклики неизвестны")
        elif job.proposals_count <= 2:
            reasons.append(f"низкая конкуренция ({job.proposals_count} откл.)")
        elif job.proposals_count > self.filters.max_proposals:
            reasons.append(f"много откликов ({job.proposals_count})")
        if job.budget_rub:
            reasons.append(f"бюджет ~{int(job.budget_rub):,} ₽".replace(",", " "))
        elif job.budget_type == "negotiable":
            reasons.append("цена договорная")
        if job.client_verified:
            reasons.append("заказчик с подтверждённой оплатой")
        if job.client_rating and job.client_rating >= 4.5:
            reasons.append(f"рейтинг заказчика {job.client_rating:g}")
        blob = job.text_blob()
        if any(pat.search(blob) for pat in _CODE_PATTERNS):
            reasons.append("вайбкодинг / код")
        elif any(pat.search(blob) for pat in _CREATIVE_PATTERNS):
            reasons.append("ИИ-креатив / видео / дизайн")
        if components.text < 0.4:
            reasons.append("слабое/мутное ТЗ")
        return reasons


HARD_SIGNALS = (
    "highload",
    "высоконагруж",
    "микросервис",
    "блокчейн",
    "smart contract",
    "erp",
    "1с",
    "ios и android",
    "под ключ с нуля",
)


def readiness_label(job: UnifiedJob) -> str:
    """Rough complexity hint used in cards when the LLM is disabled."""

    text = job.text_blob()
    complexity = 1
    if any(pat.search(text) for pat in _CODE_PATTERNS) or any(pat.search(text) for pat in _CREATIVE_PATTERNS):
        complexity -= 1
    if any(keyword in text for keyword in HARD_SIGNALS):
        complexity += 2
    if len(text) > 1500:
        complexity += 2
    elif len(text) > 700:
        complexity += 1
    return "простая" if complexity <= 1 else "средняя" if complexity <= 2 else "сложная"


def human_budget(job: UnifiedJob) -> str:
    amount = job.budget_value
    if amount is None:
        return "Договорная"
    symbol = {"RUB": "₽", "USD": "$", "EUR": "€", "UAH": "₴", "KZT": "₸", "GBP": "£"}.get(job.currency, job.currency)
    if job.currency in ("RUB", "UAH", "KZT"):
        formatted = f"{int(amount):,}".replace(",", " ")
    else:
        formatted = f"{amount:,.0f}".replace(",", " ")
    if symbol in {"$", "€", "£"}:
        return f"{symbol}{formatted}"
    return f"{formatted} {symbol}".strip()


def estimate_hours(job: UnifiedJob) -> float:
    """Very rough effort estimate from budget and text size (used in diagnostics)."""

    amount = job.budget_rub or 0
    base = max(1.0, math.log2(max(amount, 500) / 500) * 2)
    if len(job.description) > 1500:
        base *= 1.5
    return round(base, 1)
