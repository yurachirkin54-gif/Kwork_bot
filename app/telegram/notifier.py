"""Notification queue with throttling, RetryAfter handling, HTML cards and live edits."""

from __future__ import annotations

import asyncio
import html
import time
from collections.abc import Callable, Iterable
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from loguru import logger

from app.models import (
    ScoringResult,
    ScoreComponents,
    UnifiedJob,
    UserFilters,
    minutes_ago_label,
    priority_for,
    utcnow,
)
from app.storage.orm import Job, Notification
from app.storage.repository import Repository
from app.telegram.keyboards import job_keyboard

MAX_MESSAGE_LEN = 4096
SAFE_MESSAGE_LEN = 3900

SOURCE_LABELS = {
    "kwork": "Kwork",
    "fl_ru": "FL.ru",
    "freelance_ru": "Freelance.ru",
    "habr_freelance": "Habr Freelance",
    "freelancehunt": "Freelancehunt",
    "freelancer_com": "Freelancer.com",
    "weblancer": "Weblancer",
    "youdo": "YouDo",
    "workspace": "Workspace",
    "remote_boards": "Remote Boards",
}

PRIORITY_MARKERS = {"top": "🔥 ТОП", "good": "⚡ Хороший", "ok": "📌 Подходит"}

CURRENCY_SYMBOLS = {"RUB": "₽", "USD": "$", "EUR": "€", "UAH": "₴", "KZT": "₸", "GBP": "£", "BYN": "Br"}
PREFIX_SYMBOLS = {"$", "€", "£"}


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source)


def escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def _format_amount(amount: float, currency: str) -> str:
    symbol = CURRENCY_SYMBOLS.get(currency)
    if amount >= 100:
        formatted = f"{amount:,.0f}".replace(",", " ")
    else:
        formatted = f"{amount:,.2f}".rstrip("0").rstrip(".")
    if symbol in PREFIX_SYMBOLS:
        return f"{symbol}{formatted}"
    if symbol:
        return f"{formatted} {symbol}"
    return f"{formatted} {currency}"


def format_budget(job: UnifiedJob) -> str:
    if job.budget_value is None:
        return "Договорная"
    low = job.budget_min
    high = job.budget_max
    if low is not None and high is not None and abs(low - high) > 0.01:
        original = f"{_format_amount(low, job.currency)} – {_format_amount(high, job.currency)}"
        amount = high
    else:
        amount = job.budget_value
        original = _format_amount(amount, job.currency)

    equivalents: list[str] = []
    if job.currency == "RUB":
        if job.budget_usd:
            equivalents.append(f"~${job.budget_usd:,.0f}".replace(",", " "))
    elif job.currency == "USD":
        if job.budget_rub:
            equivalents.append(f"~{_format_amount(job.budget_rub, 'RUB')}")
    else:
        if job.budget_rub:
            equivalents.append(f"~{_format_amount(job.budget_rub, 'RUB')}")
        if job.budget_usd and job.currency != "USD":
            equivalents.append(f"~${job.budget_usd:,.0f}".replace(",", " "))
    suffix = f" ({', '.join(equivalents)})" if equivalents else ""
    if job.budget_type == "hourly":
        suffix += " / час"
    return f"{original}{suffix}"


def format_client(job: UnifiedJob) -> str:
    parts: list[str] = []
    if job.client_rating is not None:
        parts.append(f"⭐ {job.client_rating:g}")
    if job.client_reviews:
        parts.append(f"{job.client_reviews} отз.")
    if job.client_hire_rate is not None:
        parts.append(f"найм {job.client_hire_rate * 100:.0f}%")
    if job.client_verified is True:
        parts.append("✅ оплата")
    elif job.client_verified is False:
        parts.append("⚠️ оплата не подтверждена")
    if not parts:
        return ""
    return "👤 " + " · ".join(parts)


def format_job_card(
    job: UnifiedJob,
    score: ScoringResult,
    stale_reason: str | None = None,
    cross_posts: Iterable[str] | None = None,
) -> str:
    """Build a Telegram HTML card, always <= 4096 characters."""

    priority = score.priority or priority_for(score.total_score)
    marker = PRIORITY_MARKERS.get(priority, PRIORITY_MARKERS["ok"])
    header = (
        f"{marker} · <b>{score.total_score:.0f}/100</b> · "
        f"<i>{escape(source_label(job.source))}</i>"
    )
    link = job.url if job.url.startswith(("http://", "https://")) else ""
    title = escape(job.title)
    title_line = f'<a href="{html.escape(link, quote=True)}">{title}</a>' if link else f"<b>{title}</b>"

    budget_line = f"💰 <b>{escape(format_budget(job))}</b>"
    proposals = "н/д" if job.proposals_count is None else str(job.proposals_count)
    meta_line = f"💬 Откликов: <b>{proposals}</b> · 🕒 {escape(minutes_ago_label(job.age_minutes()))}"
    client_line = format_client(job)

    llm_line = ""
    flags_line = ""
    summary = ""
    if score.llm is not None:
        analysis = score.llm
        llm_line = (
            f"🤖 Лёгкость {analysis.easiness_score}/10 · цена {analysis.price_fairness_score}/10"
            f" · ТЗ {analysis.clarity_score}/10"
            + (f" · ~{escape(analysis.estimated_time)}" if analysis.estimated_time else "")
        )
        if analysis.red_flags:
            flags_line = "🚩 " + escape("; ".join(analysis.red_flags[:4]))[:300]
        summary = analysis.summary_ru.strip()

    description = (summary or job.description or "").strip()
    if description:
        description = " ".join(description.split())

    def compose(desc_limit: int) -> str:
        body: list[str] = [header, title_line, budget_line, meta_line]
        if client_line:
            body.append(client_line)
        if llm_line:
            body.append(llm_line)
        if description and desc_limit > 0:
            snippet = description[:desc_limit]
            if len(description) > desc_limit:
                snippet = snippet.rstrip() + "…"
            body.append(f"\n📝 {escape(snippet)}")
        if flags_line:
            body.append(flags_line)
        if cross_posts:
            unique = list(dict.fromkeys(str(item) for item in cross_posts if item))[:3]
            if unique:
                body.append("🔁 Также найдено на: " + escape(", ".join(unique)))
        if stale_reason:
            body.append(f"\n⚠️ <b>Неактуально:</b> {escape(stale_reason)}")
        return "\n".join(body)

    for limit in (700, 500, 350, 220, 120, 60, 0):
        text = compose(limit)
        if len(text) <= SAFE_MESSAGE_LEN:
            return text
    text = compose(0)
    if len(text) > MAX_MESSAGE_LEN:
        text = text[: MAX_MESSAGE_LEN - 1] + "…"
    return text


def format_digest(jobs: list[Job]) -> str:
    lines = ["☀️ <b>Утренний дайджест лучших заказов</b>", ""]
    for index, row in enumerate(jobs, start=1):
        budget = "Договорная"
        if row.budget_rub:
            budget = f"{row.budget_rub:,.0f} ₽".replace(",", " ")
        elif row.budget_max or row.budget_min:
            budget = _format_amount(row.budget_max or row.budget_min or 0, row.currency)
        proposals = "н/д" if row.proposals_count is None else str(row.proposals_count)
        title = escape(row.title)
        if row.url.startswith(("http://", "https://")):
            title = f'<a href="{html.escape(row.url, quote=True)}">{title}</a>'
        lines.append(
            f"{index}. <b>{row.score:.0f}</b> · {source_label(row.source)} · {budget} · "
            f"откликов {proposals}\n   {title}"
        )
    text = "\n".join(lines)
    if len(text) > SAFE_MESSAGE_LEN:
        text = text[: SAFE_MESSAGE_LEN - 1] + "…"
    return text


def scoring_from_row(row: Job) -> ScoringResult:
    components = ScoreComponents(**(row.score_components or {}))
    return ScoringResult(
        base_score=row.base_score or row.score or 0.0,
        llm_score=row.llm_score,
        total_score=row.score or 0.0,
        components=components,
        reasons=list(row.score_reasons or []),
        priority=priority_for(row.score or 0.0),
    )


class Notifier:
    """Async send queue with throttling and RetryAfter-safe delivery."""

    def __init__(
        self,
        bot: Bot | None,
        repository: Repository,
        filters_provider: Callable[[], UserFilters],
        *,
        chat_ids: set[int] | None = None,
        throttle_seconds: float = 1.2,
    ) -> None:
        self.bot = bot
        self.repo = repository
        self.filters_provider = filters_provider
        self.chat_ids = chat_ids or set()
        self.throttle_seconds = max(0.0, throttle_seconds)
        self._queue: asyncio.Queue[tuple[UnifiedJob, ScoringResult, int | None, set[int]]] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._last_send = 0.0
        self._sent_counter = 0

    @property
    def enabled(self) -> bool:
        return self.bot is not None and bool(self.chat_ids)

    async def start(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker(), name="notifier-worker")

    async def stop(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    async def drain(self, timeout: float = 60.0) -> None:
        """Wait until every queued notification is delivered."""

        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Очередь уведомлений не успела опустеть")

    async def submit(
        self,
        job: UnifiedJob,
        score: ScoringResult,
        db_id: int | None,
        chat_ids: set[int] | None = None,
    ) -> None:
        targets = set(chat_ids or self.chat_ids)
        if not targets:
            logger.debug(f"Нет получателей для {job.source}/{job.external_id} - уведомление пропущено")
            return
        await self._queue.put((job, score, db_id, targets))

    async def _worker(self) -> None:
        while True:
            job, score, db_id, chat_ids = await self._queue.get()
            try:
                await self._deliver(job, score, db_id, chat_ids)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - keep worker alive
                logger.exception(f"Не удалось отправить уведомление: {exc}")
            finally:
                self._queue.task_done()

    async def _throttle(self) -> None:
        if self.throttle_seconds <= 0:
            return
        loop = asyncio.get_running_loop()
        wait = self._last_send + self.throttle_seconds - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_send = loop.time()

    async def _deliver(
        self,
        job: UnifiedJob,
        score: ScoringResult,
        db_id: int | None,
        chat_ids: set[int],
    ) -> None:
        if self.bot is None:
            return
        text = format_job_card(job, score)
        markup = job_keyboard(db_id or 0, job.url)
        for chat_id in chat_ids:
            await self._throttle()
            try:
                message = await self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
                try:
                    message = await self.bot.send_message(
                        chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup
                    )
                except Exception as retry_exc:
                    logger.warning(f"Повторная отправка не удалась: {retry_exc}")
                    continue
            except TelegramForbiddenError:
                logger.warning(f"Пользователь {chat_id} заблокировал бота")
                continue
            except TelegramBadRequest as exc:
                logger.warning(f"Telegram отклонил сообщение: {exc}")
                continue
            if db_id:
                await self.repo.mark_notified(db_id, chat_id, message.message_id, score.total_score)
            self._sent_counter += 1

    async def send_health_alert(self, text: str) -> None:
        if self.bot is None or not self.chat_ids:
            logger.warning(f"Health-алерт (не отправлен): {text}")
            return
        for chat_id in self.chat_ids:
            try:
                await self.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            except Exception as exc:  # pragma: no cover - network
                logger.debug(f"Health-алерт не отправлен {chat_id}: {exc}")

    async def send_digest(self, jobs: list[Job]) -> None:
        if self.bot is None or not jobs:
            return
        text = format_digest(jobs)
        for chat_id in self.chat_ids:
            try:
                message = await self.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            except Exception as exc:
                logger.warning(f"Дайджест не отправлен {chat_id}: {exc}")
                continue
            for row in jobs:
                notification = await self.repo.mark_notified(row.id, chat_id, message.message_id, row.score or 0)
                await self.repo.advance_notification(notification.id, 99)

    async def mark_stale(self, notification: Notification, row: Job, reason: str) -> None:
        if self.bot is None or not notification.message_id:
            return
        text = format_job_card(row.to_unified(), scoring_from_row(row), stale_reason=reason)
        try:
            await self.bot.edit_message_text(
                chat_id=notification.chat_id,
                message_id=notification.message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=job_keyboard(row.id, row.url),
            )
        except TelegramBadRequest as exc:
            logger.debug(f"Live-обновление не применено: {exc}")
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except Exception as exc:  # pragma: no cover - network
            logger.debug(f"Live-обновление упало: {exc}")

    async def send_text(self, chat_id: int, text: str, markup: Any = None) -> bool:
        if self.bot is None:
            return False
        try:
            await self.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
            return True
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
            return False
        except Exception as exc:  # pragma: no cover - network
            logger.debug(f"Сообщение не отправлено: {exc}")
            return False
