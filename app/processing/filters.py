"""Stage A: hard filters that reject jobs before any scoring happens."""

from __future__ import annotations

import re
from datetime import datetime

from app.models import FilterDecision, UnifiedJob, UserFilters, utcnow

INACTIVE_STATUSES = {"in_progress", "closed"}


class HardFilter:
    """Deterministic, cheap rejection rules."""

    def __init__(self, filters: UserFilters) -> None:
        self.filters = filters
        self._include_regex = _compile(filters.include_regex)
        self._exclude_regex = _compile(filters.exclude_regex)
        self._include_kw_patterns = _compile_keywords(filters.include_keywords, allow_inflection=True)
        self._exclude_kw_patterns = _compile_keywords(filters.exclude_keywords, allow_inflection=True)

    def check(self, job: UnifiedJob, now: datetime | None = None) -> FilterDecision:
        reference = now or utcnow()
        filters = self.filters

        if job.source in filters.exclude_sources:
            return FilterDecision(passed=False, reason=f"источник {job.source} в стоп-листе")

        if job.status in INACTIVE_STATUSES:
            return FilterDecision(passed=False, reason=f"заказ не активен ({job.status})")

        age = job.age_minutes(reference)
        if age is not None and age > filters.max_age_minutes:
            return FilterDecision(passed=False, reason=f"устарел ({int(age)} мин > {filters.max_age_minutes})")

        if job.proposals_count is not None:
            if job.proposals_count > filters.max_proposals:
                return FilterDecision(
                    passed=False,
                    reason=f"высокая конкуренция ({job.proposals_count} > {filters.max_proposals})",
                )
        elif not filters.allow_unknown_proposals:
            return FilterDecision(passed=False, reason="число откликов неизвестно (запрещено настройкой)")

        budget_decision = self._check_budget(job)
        if budget_decision is not None:
            return budget_decision

        text = job.text_blob()
        for keyword, pattern in self._exclude_kw_patterns:
            if pattern.search(text):
                return FilterDecision(passed=False, reason=f"стоп-слово: {keyword}")
        for pattern in self._exclude_regex:
            if pattern.search(text):
                return FilterDecision(passed=False, reason=f"стоп-regex: {pattern.pattern}")

        if self._include_kw_patterns or self._include_regex:
            keyword_hit = any(pattern.search(text) for _kw, pattern in self._include_kw_patterns)
            regex_hit = any(pattern.search(text) for pattern in self._include_regex)
            if not keyword_hit and not regex_hit:
                return FilterDecision(passed=False, reason="нет ни одного ключевого слова из include")

        return FilterDecision(passed=True, reason="")

    def _check_budget(self, job: UnifiedJob) -> FilterDecision | None:
        filters = self.filters
        negotiable = job.budget_type == "negotiable" or (
            job.budget_min is None and job.budget_max is None
        )
        if negotiable:
            if not filters.allow_negotiable:
                return FilterDecision(passed=False, reason="цена договорная (запрещено настройкой)")
            return None

        rub_threshold = filters.min_budget_rub
        usd_threshold = filters.min_budget_usd

        if job.currency in ("USD", "EUR", "GBP") and job.budget_usd is not None:
            if usd_threshold > 0 and job.budget_usd < usd_threshold:
                return FilterDecision(
                    passed=False,
                    reason=f"бюджет ${job.budget_usd:.0f} < ${usd_threshold:.0f}",
                )
            return None

        effective_rub = job.budget_rub
        if effective_rub is None and job.currency == "RUB":
            effective_rub = job.budget_value
        if effective_rub is not None and rub_threshold > 0 and effective_rub < rub_threshold:
            return FilterDecision(
                passed=False,
                reason=f"бюджет {effective_rub:.0f} ₽ < {rub_threshold:.0f} ₽",
            )
        return None


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for raw in patterns:
        if not raw:
            continue
        try:
            compiled.append(re.compile(raw, re.IGNORECASE | re.UNICODE))
        except re.error:
            continue
    return compiled


def _compile_keywords(keywords: list[str], *, allow_inflection: bool = True) -> list[tuple[str, re.Pattern[str]]]:
    """Compile word-boundary aware patterns so e.g. 'ставки' never blocks 'доставки'."""

    compiled: list[tuple[str, re.Pattern[str]]] = []
    for raw in keywords:
        kw = str(raw or "").strip().lower()
        if not kw:
            continue
        has_wildcard = "*" in kw
        if has_wildcard:
            parts = [r"\s+".join(re.escape(w) for w in seg.split()) for seg in kw.split("*")]
            expr = r"\w*".join(parts)
            if kw[0].isalnum() or kw[0] == "_":
                expr = rf"(?<!\w){expr}"
            if kw[-1].isalnum() or kw[-1] == "_":
                expr = rf"{expr}(?!\w)"
        else:
            tokens = kw.split()
            expr = r"\s+".join(re.escape(tok) for tok in tokens)
            if kw[0].isalnum() or kw[0] == "_":
                expr = rf"(?<!\w){expr}"
            if kw[-1].isalnum() or kw[-1] == "_":
                if allow_inflection and len(tokens) == 1:
                    if len(kw) <= 3:
                        expr = rf"{expr}(?:[аыуеио]|ов|ам|ами|ах|ом|s|es)?(?!\w)"
                    elif len(kw) <= 5:
                        expr = rf"{expr}\w{{0,3}}(?!\w)"
                    else:
                        expr = rf"{expr}\w{{0,4}}(?!\w)"
                else:
                    expr = rf"{expr}(?!\w)"
        compiled.append((raw, re.compile(expr, re.IGNORECASE | re.UNICODE)))
    return compiled
