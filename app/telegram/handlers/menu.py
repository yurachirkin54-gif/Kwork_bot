"""Main menu commands: /start, /menu, /status, /stats, /pause, /resume, /scan."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.telegram.bot import AppContext
from app.telegram.handlers import esc, safe_edit
from app.telegram.keyboards import menu_keyboard, top_jobs_keyboard
from app.telegram.notifier import format_budget, source_label

router = Router(name="menu")


async def build_status_text(ctx: AppContext) -> str:
    filters = ctx.engine.filters()
    stats = await ctx.repo.stats(24)
    total_sources = len(ctx.config.sources)
    enabled_sources = sum(1 for source in ctx.config.sources if source.enabled)
    paused = "⏸ рассылка на паузе" if ctx.engine.paused else "▶️ мониторинг активен"
    quiet = "включены" if filters.quiet_hours.enabled else "выключены"
    mode = "🟢 LLM-анализ" if ctx.llm.enabled else "⚙️ только эвристики"
    lines = [
        "🤖 <b>Kwork Monitor</b> - мульти-биржевой мониторинг заказов",
        "",
        f"Состояние: {paused}",
        f"Источники: {enabled_sources}/{total_sources} включено",
        f"За 24 часа: <b>{stats['total']}</b> заказов, отправлено <b>{stats['notified']}</b>",
        f"Средний скор: {stats['avg_score']}",
        f"Очередь отправки: {ctx.notifier.queue_size}",
        f"Тихие часы: {quiet}",
        f"Анализ: {mode}",
        "",
        f"Порог скора: {filters.min_notify_score:.0f} · "
        f"макс. откликов: {filters.max_proposals} · "
        f"мин. бюджет: {filters.min_budget_rub:.0f} ₽",
    ]
    return "\n".join(lines)


async def build_stats_text(ctx: AppContext) -> str:
    day = await ctx.repo.stats(24)
    week = await ctx.repo.stats(168)
    lines = [
        "📈 <b>Статистика</b>",
        "",
        f"<b>24 часа</b>: {day['total']} найдено · {day['notified']} отправлено · "
        f"средний скор {day['avg_score']}",
        f"<b>7 дней</b>: {week['total']} найдено · {week['notified']} отправлено · "
        f"средний скор {week['avg_score']}",
    ]
    if day["by_source"]:
        lines.append("")
        lines.append("<b>По источникам за 24ч:</b>")
        for source, count in list(day["by_source"].items())[:12]:
            lines.append(f"• {esc(source)}: {count}")
    if day["top"]:
        lines.append("")
        lines.append("<b>Топ заказов:</b>")
        for item in day["top"][:5]:
            title = esc(item["title"][:70])
            url = str(item.get("url") or "")
            if url.startswith(("http://", "https://")):
                title = f'<a href="{html.escape(url, quote=True)}">{title}</a>'
            lines.append(f"• <b>{item['score']:.0f}</b> · {esc(source_label(item['source']))} · {title}")
    feedback = await ctx.repo.feedback_summary()
    if feedback:
        lines.append("")
        lines.append(
            "Обратная связь: 👍 " + str(feedback.get("1", 0)) + " · 👎 " + str(feedback.get("-1", 0))
        )
    return "\n".join(lines)


async def build_top_jobs_view(ctx: AppContext) -> tuple[str, list[int]]:
    filters = ctx.engine.filters()
    rows = await ctx.repo.recent_top_jobs(
        limit=10,
        hours=24,
        min_score=filters.min_notify_score,
        max_proposals=filters.max_proposals,
        exclude_sources=filters.exclude_sources,
    )
    fallback_used = False
    if not rows:
        rows = await ctx.repo.recent_top_jobs(
            limit=10,
            hours=24,
            min_score=0.0,
            exclude_sources=filters.exclude_sources,
        )
        fallback_used = bool(rows)
    if not rows:
        return (
            "🔥 <b>Свежие лучшие заказы</b>\n\n"
            "За последние 24 часа пока нет подходящих открытых заказов.\n"
            "Нажмите «🔄 Сканировать» или расширьте фильтры в /filters.",
            [],
        )
    header = (
        "🔥 <b>Лучшие заказы за 24ч</b> (ниже порога, но самые свежие)"
        if fallback_used
        else f"🔥 <b>Лучшие заказы за 24ч</b> (скор ≥ {filters.min_notify_score:.0f})"
    )
    lines = [header, "Нажмите номер ниже, чтобы открыть полную карточку и сгенерировать отклик:", ""]
    ids: list[int] = []
    for idx, row in enumerate(rows, start=1):
        ids.append(row.id)
        unified = row.to_unified()
        budget = esc(format_budget(unified))
        proposals = "н/д" if row.proposals_count is None else str(row.proposals_count)
        title = esc(row.title[:75])
        if row.url.startswith(("http://", "https://")):
            title = f'<a href="{html.escape(row.url, quote=True)}">{title}</a>'
        lines.append(
            f"{idx}. <b>{row.score:.0f}</b> · {esc(source_label(row.source))} · {budget} · откл {proposals}\n"
            f"   {title}"
        )
    return "\n".join(lines), ids


@router.message(Command("start", "menu", "help"))
async def cmd_start(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        await build_status_text(ctx), parse_mode="HTML", reply_markup=menu_keyboard(ctx.engine.paused)
    )


@router.message(Command("status"))
async def cmd_status(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        await build_status_text(ctx), parse_mode="HTML", reply_markup=menu_keyboard(ctx.engine.paused)
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await message.answer(await build_stats_text(ctx), parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("top"))
async def cmd_top(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    text, ids = await build_top_jobs_view(ctx)
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=top_jobs_keyboard(ids),
        disable_web_page_preview=True,
    )


@router.message(Command("pause"))
async def cmd_pause(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await ctx.engine.set_paused(True)
    await message.answer("⏸ Рассылка приостановлена. Заказы продолжают собираться в базу.")


@router.message(Command("resume"))
async def cmd_resume(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await ctx.engine.set_paused(False)
    pending = await ctx.repo.pending_count(min_score=ctx.engine.filters().min_notify_score)
    hint = f"\nВ базе накопилось подходящих заказов: <b>{pending}</b> (посмотреть: /top)." if pending else ""
    await message.answer(
        f"▶️ Рассылка возобновлена.{hint}",
        parse_mode="HTML",
        reply_markup=menu_keyboard(False),
    )


@router.message(Command("scan"))
async def cmd_scan(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🔄 Запускаю внеочередное сканирование...")
    reports = await ctx.engine.scan_once(force=True)
    if not reports:
        await message.answer("Нет включённых источников. Проверьте /sources.")
        return
    lines = ["✅ <b>Сканирование завершено</b>", ""]
    lines.extend(reports_lines(reports))
    await message.answer("\n".join(lines), parse_mode="HTML")


def reports_lines(reports) -> list[str]:
    return [f"• {esc(report.summary())}" for report in reports]


@router.callback_query(F.data == "menu:main")
async def cb_main(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await safe_edit(
        callback, await build_status_text(ctx), menu_keyboard(ctx.engine.paused)
    )


@router.callback_query(F.data == "menu:top")
async def cb_top(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    text, ids = await build_top_jobs_view(ctx)
    await safe_edit(callback, text, top_jobs_keyboard(ids))


@router.callback_query(F.data == "menu:status")
async def cb_status(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await safe_edit(
        callback, await build_status_text(ctx), menu_keyboard(ctx.engine.paused)
    )


@router.callback_query(F.data == "menu:stats")
async def cb_stats(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await safe_edit(callback, await build_stats_text(ctx), menu_keyboard(ctx.engine.paused))


@router.callback_query(F.data == "menu:pause")
async def cb_pause(callback: CallbackQuery, ctx: AppContext) -> None:
    await ctx.engine.set_paused(True)
    await callback.answer("⏸ Пауза")
    await safe_edit(
        callback,
        "⏸ <b>Рассылка приостановлена.</b>\nЗаказы собираются в базе, уведомления не отправляются.",
        menu_keyboard(True),
    )


@router.callback_query(F.data == "menu:resume")
async def cb_resume(callback: CallbackQuery, ctx: AppContext) -> None:
    await ctx.engine.set_paused(False)
    await callback.answer("▶️ Активно")
    await safe_edit(
        callback, "▶️ <b>Рассылка возобновлена.</b>", menu_keyboard(False)
    )


@router.callback_query(F.data == "menu:scan")
async def cb_scan(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer("🔄 Сканирую...")
    reports = await ctx.engine.scan_once(force=True)
    if not reports:
        await safe_edit(callback, "Нет включённых источников.", menu_keyboard(ctx.engine.paused))
        return
    lines = ["✅ <b>Сканирование завершено</b>", ""] + reports_lines(reports)
    await safe_edit(callback, "\n".join(lines), menu_keyboard(ctx.engine.paused))
