"""Main menu commands: /start, /menu, /status, /stats, /pause, /resume, /scan."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.models import minutes_ago_label, utcnow
from app.telegram.bot import AppContext
from app.telegram.handlers import safe_edit
from app.telegram.keyboards import menu_keyboard

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
            lines.append(f"• {source}: {count}")
    if day["top"]:
        lines.append("")
        lines.append("<b>Топ заказов:</b>")
        for item in day["top"][:5]:
            lines.append(f"• {item['score']:.0f} · {item['source']} · {item['title'][:70]}")
    feedback = await ctx.repo.feedback_summary()
    if feedback:
        lines.append("")
        lines.append(
            "Обратная связь: 👍 " + str(feedback.get("1", 0)) + " · 👎 " + str(feedback.get("-1", 0))
        )
    return "\n".join(lines)


@router.message(Command("start", "menu", "help"))
async def cmd_start(message: Message, ctx: AppContext) -> None:
    await message.answer(
        await build_status_text(ctx), reply_markup=menu_keyboard(ctx.engine.paused)
    )


@router.message(Command("status"))
async def cmd_status(message: Message, ctx: AppContext) -> None:
    await message.answer(
        await build_status_text(ctx), reply_markup=menu_keyboard(ctx.engine.paused)
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, ctx: AppContext) -> None:
    await message.answer(await build_stats_text(ctx))


@router.message(Command("pause"))
async def cmd_pause(message: Message, ctx: AppContext) -> None:
    await ctx.engine.set_paused(True)
    await message.answer("⏸ Рассылка приостановлена. Заказы продолжают собираться в базу.")


@router.message(Command("resume"))
async def cmd_resume(message: Message, ctx: AppContext) -> None:
    await ctx.engine.set_paused(False)
    await message.answer("▶️ Рассылка возобновлена.", reply_markup=menu_keyboard(False))


@router.message(Command("scan"))
async def cmd_scan(message: Message, ctx: AppContext) -> None:
    await message.answer("🔄 Запускаю внеочередное сканирование...")
    reports = await ctx.engine.scan_once(force=True)
    if not reports:
        await message.answer("Нет включённых источников. Проверьте /sources.")
        return
    lines = ["✅ <b>Сканирование завершено</b>", ""]
    lines.extend(reports_lines(reports))
    await message.answer("\n".join(lines))


def reports_lines(reports) -> list[str]:
    return [f"• {report.summary()}" for report in reports]


@router.callback_query(F.data == "menu:main")
async def cb_main(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer()
    await safe_edit(
        callback, await build_status_text(ctx), menu_keyboard(ctx.engine.paused)
    )


@router.callback_query(F.data == "menu:status")
async def cb_status(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer()
    await safe_edit(
        callback, await build_status_text(ctx), menu_keyboard(ctx.engine.paused)
    )


@router.callback_query(F.data == "menu:stats")
async def cb_stats(callback: CallbackQuery, ctx: AppContext) -> None:
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
