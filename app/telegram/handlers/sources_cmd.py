"""Sources management: /sources, enable/disable, health check and /add_source wizard."""

from __future__ import annotations

import re
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from loguru import logger

from app.config import SelectorConfig, JsonMappingConfig, SourceConfig
from app.models import SourceStatus
from app.sources.base import SourceError
from app.sources.registry import create_adapter
from app.telegram.bot import AppContext
from app.telegram.handlers import esc, safe_edit
from app.telegram.keyboards import (
    back_to_menu_keyboard,
    cancel_keyboard,
    source_confirm_keyboard,
    source_detail_keyboard,
    source_icon,
    source_kind_keyboard,
    sources_keyboard,
)

router = Router(name="sources")

NAME_RE = re.compile(r"^[a-z0-9_]{2,32}$")


class AddSource(StatesGroup):
    waiting_kind = State()
    waiting_name = State()
    waiting_url = State()
    waiting_item = State()
    waiting_title = State()
    waiting_confirm = State()


async def collect_statuses(ctx: AppContext) -> list[SourceStatus]:
    active_configs = {cfg.name: cfg for cfg in ctx.config.sources}
    statuses = [
        status for status in await ctx.repo.source_statuses(24) if status.name in active_configs
    ]
    for status in statuses:
        status.enabled = active_configs[status.name].enabled
    known = {status.name for status in statuses}
    for config in ctx.config.sources:
        if config.name in known:
            continue
        statuses.append(
            SourceStatus(
                name=config.name,
                kind=config.kind,
                enabled=config.enabled,
                state="ok",
                jobs_24h=0,
                notes=config.notes,
            )
        )
    statuses.sort(key=lambda item: (not item.enabled, item.name))
    return statuses


async def build_source_detail(ctx: AppContext, name: str) -> tuple[str, Any] | None:
    config = ctx.config.get_source(name)
    if config is None:
        return None
    statuses = {s.name: s for s in await collect_statuses(ctx)}
    status = statuses.get(
        name,
        SourceStatus(name=name, kind=config.kind, enabled=config.enabled, state="ok"),
    )
    lines = [
        f"⚙️ <b>Источник: {esc(name)}</b>",
        "",
        f"Статус: {source_icon(status)}",
        f"Тип: <code>{esc(config.kind)}</code>",
        f"Интервал опроса: <b>{config.interval_seconds} с</b>",
        f"За 24ч найдено: <b>{status.jobs_24h}</b>",
    ]
    if status.last_error:
        lines.append(f"Последняя ошибка: {esc(status.last_error[:200])}")
    if config.notes:
        lines.append(f"Примечание: {esc(config.notes[:200])}")
    lines.append("")
    lines.append("Выберите интервал опроса или действие (сохраняется в БД):")
    markup = source_detail_keyboard(
        name,
        config.enabled,
        config.interval_seconds,
        can_delete=not ctx.config.is_builtin_source(name),
    )
    return "\n".join(lines), markup


async def build_sources_view(ctx: AppContext) -> tuple[str, Any]:
    statuses = await collect_statuses(ctx)
    lines = ["🌐 <b>Источники мониторинга</b>", ""]
    for status in statuses:
        config = ctx.config.get_source(status.name)
        interval = f"{config.interval_seconds} с" if config else "-"
        lines.append(
            f"{source_icon(status)} · <b>{esc(status.name)}</b> ({esc(config.kind if config else status.kind)}) · "
            f"24ч: {status.jobs_24h} · интервал {interval}"
        )
        if status.last_error:
            lines.append(f"   ↳ {esc(status.last_error[:120])}")
        if config and config.notes:
            lines.append(f"   ℹ️ {esc(config.notes[:140])}")
    if len(lines) == 2:
        lines.append("Источники не настроены.")
    return "\n".join(lines), sources_keyboard(statuses)


async def show_sources(target: Message | CallbackQuery, ctx: AppContext) -> None:
    text, markup = await build_sources_view(ctx)
    if isinstance(target, CallbackQuery):
        await safe_edit(target, text, markup)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


@router.message(Command("sources"))
async def cmd_sources(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await show_sources(message, ctx)


@router.message(Command("del_source"))
async def cmd_del_source(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: <code>/del_source имя_источника</code>", parse_mode="HTML")
        return
    name = parts[1].strip()
    if await ctx.engine.remove_source(name):
        await message.answer(f"🗑 Источник <b>{esc(name)}</b> удалён.", parse_mode="HTML")
    else:
        await message.answer(f"Источник <b>{esc(name)}</b> не найден.", parse_mode="HTML")


@router.callback_query(F.data == "menu:sources")
async def cb_sources(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await show_sources(callback, ctx)


@router.callback_query(F.data.startswith("src:toggle:"))
async def cb_toggle_source(callback: CallbackQuery, ctx: AppContext) -> None:
    name = (callback.data or "").split(":", 2)[2]
    config = ctx.config.get_source(name)
    if config is None:
        await callback.answer("Источник не найден", show_alert=True)
        return
    new_state = not config.enabled
    await ctx.engine.set_source_enabled(name, new_state)
    await callback.answer("Включён" if new_state else "Выключен")
    await show_sources(callback, ctx)


@router.callback_query(F.data.startswith("src:open:"))
async def cb_open_source(callback: CallbackQuery, ctx: AppContext) -> None:
    name = (callback.data or "").split(":", 2)[2]
    view = await build_source_detail(ctx, name)
    if view is None:
        await callback.answer("Источник не найден", show_alert=True)
        return
    await callback.answer()
    await safe_edit(callback, view[0], view[1])


@router.callback_query(F.data.startswith("src:toggle_detail:"))
async def cb_toggle_source_detail(callback: CallbackQuery, ctx: AppContext) -> None:
    name = (callback.data or "").split(":", 2)[2]
    config = ctx.config.get_source(name)
    if config is None:
        await callback.answer("Источник не найден", show_alert=True)
        return
    new_state = not config.enabled
    await ctx.engine.set_source_enabled(name, new_state)
    await callback.answer("Включён" if new_state else "Выключен")
    view = await build_source_detail(ctx, name)
    if view is not None:
        await safe_edit(callback, view[0], view[1])


@router.callback_query(F.data.startswith("src:int:"))
async def cb_source_interval(callback: CallbackQuery, ctx: AppContext) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) < 4 or not parts[3].isdigit():
        await callback.answer("Некорректный интервал", show_alert=True)
        return
    name = parts[2]
    seconds = int(parts[3])
    if not await ctx.engine.set_source_interval(name, seconds):
        await callback.answer("Источник не найден", show_alert=True)
        return
    await callback.answer(f"Интервал: {seconds} с")
    view = await build_source_detail(ctx, name)
    if view is not None:
        await safe_edit(callback, view[0], view[1])


@router.callback_query(F.data.startswith("src:del:"))
async def cb_delete_source(callback: CallbackQuery, ctx: AppContext) -> None:
    name = (callback.data or "").split(":", 2)[2]
    if await ctx.engine.remove_source(name):
        await callback.answer(f"🗑 {name} удалён")
    else:
        await callback.answer("Источник не найден", show_alert=True)
    await show_sources(callback, ctx)


@router.callback_query(F.data.startswith("src:check_detail:"))
async def cb_check_source_detail(callback: CallbackQuery, ctx: AppContext) -> None:
    name = (callback.data or "").split(":", 2)[2]
    await callback.answer("Проверяю источник...")
    ok, message = await ctx.engine.probe_source(name)
    icon = "✅" if ok else "⚠️"
    if isinstance(callback.message, Message):
        await callback.message.answer(f"{icon} <b>{esc(name)}</b>: {esc(message)}", parse_mode="HTML")
    view = await build_source_detail(ctx, name)
    if view is not None:
        await safe_edit(callback, view[0], view[1])


@router.callback_query(F.data.startswith("src:check:"))
async def cb_check_source(callback: CallbackQuery, ctx: AppContext) -> None:
    name = (callback.data or "").split(":", 2)[2]
    await callback.answer("Проверяю источник...")
    ok, message = await ctx.engine.probe_source(name)
    icon = "✅" if ok else "⚠️"
    text, markup = await build_sources_view(ctx)
    if isinstance(callback.message, Message):
        await callback.message.answer(f"{icon} <b>{esc(name)}</b>: {esc(message)}", parse_mode="HTML")
    await safe_edit(callback, text, markup)


# --------------------------------------------------------------- add source flow
@router.message(Command("add_source"))
async def cmd_add_source(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddSource.waiting_kind)
    await message.answer(
        "➕ <b>Добавление новой биржи</b>\n\nВыберите тип источника:",
        parse_mode="HTML",
        reply_markup=source_kind_keyboard(),
    )


@router.callback_query(F.data == "src:add")
async def cb_add_source(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddSource.waiting_kind)
    await callback.answer()
    await safe_edit(
        callback,
        "➕ <b>Добавление новой биржи</b>\n\nВыберите тип источника:",
        source_kind_keyboard(),
    )


@router.callback_query(F.data.startswith("addsrc:kind:"), StateFilter(AddSource.waiting_kind))
async def cb_add_kind(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    kind = (callback.data or "").split(":", 2)[2]
    if kind not in ("rss", "html", "json"):
        await callback.answer("Неизвестный тип", show_alert=True)
        return
    await state.update_data(kind=kind)
    await state.set_state(AddSource.waiting_name)
    await callback.answer()
    await safe_edit(
        callback,
        "Шаг 1/3. Отправьте короткое имя источника латиницей, например <code>myboard</code>.",
        cancel_keyboard(),
    )


@router.message(StateFilter(AddSource.waiting_name))
async def add_source_name(message: Message, ctx: AppContext, state: FSMContext) -> None:
    name = (message.text or "").strip().lower()
    if not NAME_RE.match(name):
        await message.answer("Имя: 2-32 символа, латиница/цифры/подчёркивание. Попробуйте ещё раз.")
        return
    if ctx.config.get_source(name) is not None:
        await message.answer("Источник с таким именем уже существует. Введите другое имя.")
        return
    await state.update_data(name=name)
    await state.set_state(AddSource.waiting_url)
    await message.answer(
        "Шаг 2/3. Отправьте URL ленты или страницы со списком заказов (https://...).",
        reply_markup=cancel_keyboard(),
    )


@router.message(StateFilter(AddSource.waiting_url))
async def add_source_url(message: Message, ctx: AppContext, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if not url.startswith(("http://", "https://")):
        await message.answer(
            "Нужен полный URL, начинающийся с http:// или https://",
            reply_markup=cancel_keyboard(),
        )
        return
    data = await state.get_data()
    kind = data.get("kind")
    await state.update_data(url=url)
    if kind == "html":
        await state.set_state(AddSource.waiting_item)
        await message.answer(
            "Шаг 3/3. CSS-селектор карточки заказа, например <code>div.project-card</code>.",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return
    if kind == "json":
        await state.set_state(AddSource.waiting_item)
        await message.answer(
            "Шаг 3/3. Путь к массиву заказов в JSON, например <code>data.jobs</code>.\n"
            "Отправьте <code>-</code>, если массив в корне.",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return
    await _preview_source(message, ctx, state)


@router.message(StateFilter(AddSource.waiting_item))
async def add_source_item(message: Message, ctx: AppContext, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if value == "-":
        value = ""
    data = await state.get_data()
    kind = data.get("kind")
    await state.update_data(item=value)
    if kind == "html":
        if not value:
            await message.answer(
                "Селектор карточки обязателен. Например: div.project-card",
                reply_markup=cancel_keyboard(),
            )
            return
        await state.set_state(AddSource.waiting_title)
        await message.answer(
            "Укажите CSS-селектор заголовка внутри карточки, например <code>a.title</code>.",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return
    await _preview_source(message, ctx, state)


@router.message(StateFilter(AddSource.waiting_title))
async def add_source_title(message: Message, ctx: AppContext, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer("Селектор заголовка обязателен. Например: a.title")
        return
    await state.update_data(title=title)
    await _preview_source(message, ctx, state)


def _build_config(data: dict[str, Any]) -> SourceConfig:
    kind = str(data.get("kind") or "rss")
    name = str(data.get("name") or "")
    url = str(data.get("url") or "")
    params: dict[str, Any] = {"url": url}
    selectors = SelectorConfig()
    mapping = JsonMappingConfig()
    if kind == "html":
        params["url"] = url
        selectors = SelectorConfig(
            item=str(data.get("item") or ""),
            title=str(data.get("title") or ""),
            url=f"{data.get('title')}@href" if data.get("title") else "",
        )
    elif kind == "json":
        mapping = JsonMappingConfig(items_path=str(data.get("item") or ""))
    return SourceConfig(
        name=name,
        kind=kind,
        enabled=True,
        url=url,
        params=params,
        selectors=selectors,
        mapping=mapping,
        interval_seconds=300,
        timeout=25,
        max_items=30,
        notes="Добавлен через Telegram",
    )


async def _preview_source(message: Message, ctx: AppContext, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        config = _build_config(data)
    except Exception as exc:
        await message.answer(f"Не удалось собрать конфиг: {exc}")
        return
    adapter = create_adapter(config, ctx.engine.http, ctx.engine.converter, ctx.engine._browser)
    if adapter is None:
        await message.answer("Не удалось создать адаптер для этого типа источника.")
        return
    await message.answer("🧪 Тестовый прогон...")
    try:
        jobs = await adapter.fetch_listing()
    except SourceError as exc:
        await message.answer(
            f"❌ Тест не прошёл: <b>{esc(exc)}</b>\n\n"
            "Проверьте URL/селекторы и попробуйте снова через /add_source.",
            parse_mode="HTML",
        )
        await state.clear()
        return
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(f"add_source: тест упал: {exc}")
        await message.answer(f"❌ Ошибка теста: {esc(exc)}", parse_mode="HTML")
        await state.clear()
        return
    preview = "\n".join(f"• {esc(job.title[:90])} ({esc(job.url)})" for job in jobs[:3])
    await state.update_data(config=config.model_dump(mode="json"))
    await state.set_state(AddSource.waiting_confirm)
    await message.answer(
        f"✅ Тест успешен: найдено <b>{len(jobs)}</b> заказов.\n\n{preview}\n\nСохранить источник?",
        parse_mode="HTML",
        reply_markup=source_confirm_keyboard(),
    )


@router.callback_query(F.data == "addsrc:save", StateFilter(AddSource.waiting_confirm))
async def save_new_source(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    data = await state.get_data()
    raw = data.get("config")
    if not isinstance(raw, dict):
        await callback.answer("Черновик потерян, начните заново", show_alert=True)
        await state.clear()
        return
    config = SourceConfig.model_validate(raw)
    await ctx.engine.add_custom_source(config)
    await state.clear()
    await callback.answer("Источник сохранён")
    await show_sources(callback, ctx)


@router.callback_query(F.data == "addsrc:cancel")
async def cancel_add_source(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await show_sources(callback, ctx)
