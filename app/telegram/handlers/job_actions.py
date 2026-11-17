"""Buttons under job cards: proposal, refresh, 👍/👎, stop-word and hide actions."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from loguru import logger

from app.processing.llm_analyzer import template_proposal
from app.telegram.bot import AppContext
from app.telegram.handlers import reply_long, safe_edit
from app.telegram.keyboards import dislike_keyboard, job_keyboard
from app.telegram.notifier import format_job_card, scoring_from_row, source_label

router = Router(name="job_actions")


class FeedbackInput(StatesGroup):
    waiting_stopword = State()


def _job_id(data: str | None) -> int | None:
    if not data:
        return None
    try:
        return int(data.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


@router.callback_query(F.data.startswith("job:proposal:"))
async def cb_proposal(callback: CallbackQuery, ctx: AppContext) -> None:
    job_id = _job_id(callback.data)
    if job_id is None:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    row = await ctx.repo.get_job(job_id)
    if row is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    await callback.answer("Готовлю отклик...")
    unified = row.to_unified()
    draft = await ctx.llm.draft_proposal(unified)
    origin = "AI" if draft else "шаблон"
    if not draft:
        draft = template_proposal(unified)
    await ctx.repo.set_draft_proposal(job_id, draft)
    text = (
        f"✍️ <b>Черновик отклика</b> ({origin}) - {source_label(row.source)}\n"
        f"<b>{row.title[:160]}</b>\n\n{draft}\n\n"
        f"🔗 {row.url}"
    )
    if isinstance(callback.message, Message):
        await reply_long(callback.message, text)


@router.callback_query(F.data.startswith("job:refresh:"))
async def cb_refresh(callback: CallbackQuery, ctx: AppContext) -> None:
    job_id = _job_id(callback.data)
    if job_id is None:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    await callback.answer("Обновляю...")
    row, message = await ctx.engine.refresh_job(job_id)
    if row is None:
        await callback.answer(message, show_alert=True)
        return
    text = format_job_card(row.to_unified(), scoring_from_row(row))
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=job_keyboard(row.id, row.url),
            )
        except Exception:  # pragma: no cover - message too old
            await callback.message.answer(text, parse_mode="HTML", reply_markup=job_keyboard(row.id, row.url))
        await callback.message.answer(f"🔄 {message}")


@router.callback_query(F.data.startswith("job:like:"))
async def cb_like(callback: CallbackQuery, ctx: AppContext) -> None:
    job_id = _job_id(callback.data)
    if job_id is None or callback.from_user is None:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    await ctx.repo.add_feedback(job_id, callback.from_user.id, 1)
    await callback.answer("👍 Спасибо! Учту, что такие заказы нравятся")


@router.callback_query(F.data.startswith("job:dislike:"))
async def cb_dislike(callback: CallbackQuery, ctx: AppContext) -> None:
    job_id = _job_id(callback.data)
    if job_id is None or callback.from_user is None:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    await ctx.repo.add_feedback(job_id, callback.from_user.id, -1)
    await callback.answer("👎 Понял. Что сделать с такими заказами?")
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=dislike_keyboard(job_id))


@router.callback_query(F.data.startswith("fb:hide:"))
async def cb_hide(callback: CallbackQuery, ctx: AppContext) -> None:
    job_id = _job_id(callback.data)
    if job_id is None:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    await ctx.repo.set_job_hidden(job_id, True)
    await callback.answer("🗑 Заказ скрыт")
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


@router.callback_query(F.data.startswith("fb:source:"))
async def cb_hide_source(callback: CallbackQuery, ctx: AppContext) -> None:
    job_id = _job_id(callback.data)
    if job_id is None:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    row = await ctx.repo.get_job(job_id)
    if row is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    filters = ctx.engine.filters()
    excluded = list(dict.fromkeys([*filters.exclude_sources, row.source]))
    await ctx.engine.update_filters({"exclude_sources": excluded})
    await ctx.repo.set_job_hidden(job_id, True)
    await callback.answer(f"🙈 Источник {row.source} отключён")
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("fb:stop:"))
async def cb_stopword(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    job_id = _job_id(callback.data)
    if job_id is None:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    row = await ctx.repo.get_job(job_id)
    await state.set_state(FeedbackInput.waiting_stopword)
    await state.update_data(job_id=job_id)
    await callback.answer()
    hint = f"\nЗаголовок: {row.title[:120]}" if row else ""
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Отправьте слово или фразу, которую нужно добавить в стоп-лист."
            + hint
            + "\n\nМожно несколько через запятую.",
        )


@router.message(FeedbackInput.waiting_stopword)
async def process_stopword(message: Message, ctx: AppContext, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пустая фраза. Отправьте слово или нажмите /filters для отмены.")
        return
    values = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
    filters = ctx.engine.filters()
    merged = list(dict.fromkeys([*filters.exclude_keywords, *values]))
    await ctx.engine.update_filters({"exclude_keywords": merged})
    data = await state.get_data()
    job_id = data.get("job_id")
    if isinstance(job_id, int):
        await ctx.repo.set_job_hidden(job_id, True)
    await state.clear()
    await message.answer(
        f"⛔ Добавлено в стоп-лист: {', '.join(values)}\nВсего стоп-слов: {len(merged)}"
    )


@router.callback_query(F.data == "job:open")
async def cb_open_unknown(callback: CallbackQuery) -> None:
    await callback.answer("Ссылка есть в карточке заказа", show_alert=True)
