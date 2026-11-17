"""Filter management: /filters, inline toggles and FSM value input."""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from pydantic import ValidationError

from app.models import QuietHours, UserFilters
from app.telegram.bot import AppContext
from app.telegram.handlers import esc, safe_edit
from app.telegram.keyboards import cancel_keyboard, filters_keyboard

router = Router(name="filters")

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

NUMERIC_FIELDS: dict[str, tuple[str, str, type]] = {
    "budget_rub": ("min_budget_rub", "минимальный бюджет в рублях", float),
    "budget_usd": ("min_budget_usd", "минимальный бюджет в долларах", float),
    "proposals": ("max_proposals", "максимальное число откликов", int),
    "score": ("min_notify_score", "минимальный скор для отправки (0-100)", float),
    "age": ("max_age_minutes", "максимальный возраст заказа в минутах", int),
}


class FilterInput(StatesGroup):
    waiting_value = State()


def build_filters_text(filters: UserFilters) -> str:
    include = ", ".join(filters.include_keywords[:12]) or "все категории"
    exclude = ", ".join(filters.exclude_keywords[:12]) or "нет"
    quiet = (
        f"🌙 {filters.quiet_hours.start}-{filters.quiet_hours.end}"
        if filters.quiet_hours.enabled
        else "🌙 выключены"
    )
    return "\n".join(
        [
            "🎛 <b>Фильтры</b> (применяются сразу)",
            "",
            f"💰 Мин. бюджет: <b>{filters.min_budget_rub:.0f} ₽</b> / "
            f"<b>{filters.min_budget_usd:.0f} $</b>",
            f"💬 Макс. откликов: <b>{filters.max_proposals}</b> "
            f"({'отклики н/д допускаются' if filters.allow_unknown_proposals else 'только известные'})",
            f"⭐ Мин. скор: <b>{filters.min_notify_score:.0f}</b>",
            f"🕒 Макс. возраст: <b>{filters.max_age_minutes} мин</b>",
            f"🤝 Договорная: <b>{'да' if filters.allow_negotiable else 'нет'}</b>",
            f"{quiet}",
            "",
            f"✅ Ключевые слова ({len(filters.include_keywords)}): {esc(include)}",
            f"⛔ Стоп-слова ({len(filters.exclude_keywords)}): {esc(exclude)}",
        ]
    )


async def show_filters(target: Message | CallbackQuery, ctx: AppContext) -> None:
    filters = ctx.engine.filters()
    text = build_filters_text(filters)
    markup = filters_keyboard(filters)
    if isinstance(target, CallbackQuery):
        await safe_edit(target, text, markup)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


@router.message(Command("filters"))
async def cmd_filters(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await show_filters(message, ctx)


@router.callback_query(F.data == "menu:filters")
async def cb_filters(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer()
    await show_filters(callback, ctx)


@router.callback_query(F.data.startswith("flt:"))
async def cb_filter_action(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    data = callback.data or ""
    parts = data.split(":")

    if data == "flt:reset":
        filters = await ctx.engine.reset_filters()
        await callback.answer("♻️ Сброшено к настройкам по умолчанию")
        await safe_edit(callback, build_filters_text(filters), filters_keyboard(filters))
        return

    if data == "flt:negotiable":
        filters = ctx.engine.filters()
        filters = await ctx.engine.update_filters({"allow_negotiable": not filters.allow_negotiable})
        await callback.answer("Обновлено")
        await safe_edit(callback, build_filters_text(filters), filters_keyboard(filters))
        return

    if data == "flt:unknown":
        filters = ctx.engine.filters()
        filters = await ctx.engine.update_filters(
            {"allow_unknown_proposals": not filters.allow_unknown_proposals}
        )
        await callback.answer("Обновлено")
        await safe_edit(callback, build_filters_text(filters), filters_keyboard(filters))
        return

    if data == "flt:quiet_toggle":
        filters = ctx.engine.filters()
        enabled = not filters.quiet_hours.enabled
        filters = await ctx.engine.update_filters({"quiet_hours": {"enabled": enabled}})
        await callback.answer("Тихие часы включены" if enabled else "Тихие часы выключены")
        await safe_edit(callback, build_filters_text(filters), filters_keyboard(filters))
        return

    if data.startswith("flt:kwdel:"):
        _, _, target, index_raw = parts
        try:
            index = int(index_raw)
        except ValueError:
            await callback.answer("Некорректный индекс", show_alert=True)
            return
        filters = ctx.engine.filters()
        field = "include_keywords" if target == "include" else "exclude_keywords"
        values = list(getattr(filters, field))
        if 0 <= index < len(values):
            removed = values.pop(index)
            filters = await ctx.engine.update_filters({field: values})
            await callback.answer(f"Удалено: {removed}")
        else:
            await callback.answer("Уже удалено")
        await safe_edit(callback, build_filters_text(filters), filters_keyboard(filters))
        return

    if data in ("flt:quiet_start", "flt:quiet_end"):
        field = "start" if data.endswith("start") else "end"
        await state.set_state(FilterInput.waiting_value)
        await state.update_data(field=data, quiet_field=field)
        await callback.answer()
        await safe_edit(
            callback,
            f"Введите время {'начала' if field == 'start' else 'окончания'} тихих часов в формате HH:MM\n"
            f"Сейчас: {getattr(ctx.engine.filters().quiet_hours, field)}",
            cancel_keyboard(),
        )
        return

    if data in ("flt:include_add", "flt:exclude_add"):
        await state.set_state(FilterInput.waiting_value)
        await state.update_data(field=data)
        await callback.answer()
        label = "ключевое слово" if data == "flt:include_add" else "стоп-слово"
        await safe_edit(
            callback,
            f"Отправьте {label} сообщением.\nМожно несколько через запятую.",
            cancel_keyboard(),
        )
        return

    key = data.removeprefix("flt:")
    if key in NUMERIC_FIELDS:
        field, label, _type = NUMERIC_FIELDS[key]
        current = getattr(ctx.engine.filters(), field)
        await state.set_state(FilterInput.waiting_value)
        await state.update_data(field=key)
        await callback.answer()
        await safe_edit(
            callback,
            f"Введите новое значение: <b>{label}</b>\nСейчас: <b>{current}</b>",
            cancel_keyboard(),
        )
        return

    await callback.answer("Неизвестное действие", show_alert=True)


@router.message(StateFilter(FilterInput.waiting_value))
async def process_filter_value(message: Message, ctx: AppContext, state: FSMContext) -> None:
    data = await state.get_data()
    field = str(data.get("field") or "")
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пустое значение. Попробуйте ещё раз или нажмите «Отмена».")
        return

    patch: dict = {}
    if field == "flt:include_add":
        values = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
        filters = ctx.engine.filters()
        merged = list(dict.fromkeys([*filters.include_keywords, *values]))
        patch = {"include_keywords": merged}
    elif field == "flt:exclude_add":
        values = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
        filters = ctx.engine.filters()
        merged = list(dict.fromkeys([*filters.exclude_keywords, *values]))
        patch = {"exclude_keywords": merged}
    elif field in ("flt:quiet_start", "flt:quiet_end"):
        if not TIME_RE.match(raw):
            await message.answer("Нужен формат HH:MM, например 23:30.")
            return
        quiet_field = "start" if field == "flt:quiet_start" else "end"
        try:
            QuietHours(**{**ctx.engine.filters().quiet_hours.model_dump(), quiet_field: raw})
        except ValidationError:
            await message.answer("Некорректное время.")
            return
        patch = {"quiet_hours": {quiet_field: raw}}
    elif field in NUMERIC_FIELDS:
        target, label, caster = NUMERIC_FIELDS[field]
        try:
            value = caster(raw.replace(",", "."))
        except ValueError:
            await message.answer("Нужно число. Попробуйте ещё раз.")
            return
        if value < 0:
            await message.answer("Значение не может быть отрицательным.")
            return
        if target == "min_notify_score" and value > 100:
            await message.answer("Скор не может быть больше 100.")
            return
        if target == "max_age_minutes" and value < 1:
            await message.answer("Минимум 1 минута.")
            return
        patch = {target: value}
    else:
        await state.clear()
        await message.answer("Не понял, что менять. Откройте /filters заново.")
        return

    filters = await ctx.engine.update_filters(patch)
    await state.clear()
    await message.answer(
        build_filters_text(filters), parse_mode="HTML", reply_markup=filters_keyboard(filters)
    )


@router.callback_query(F.data == "fsm:cancel")
async def cancel_fsm(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await show_filters(callback, ctx)
