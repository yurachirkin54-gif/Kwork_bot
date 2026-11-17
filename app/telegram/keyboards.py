"""Inline keyboards for job cards, menu, filters and sources."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.models import SourceStatus, UserFilters


def _valid_url(url: str | None) -> str | None:
    if url and url.startswith(("http://", "https://")):
        return url
    return None


def job_keyboard(job_id: int, url: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    open_url = _valid_url(url)
    if open_url:
        rows.append([InlineKeyboardButton(text="🔗 Открыть заказ", url=open_url)])
    rows.append(
        [
            InlineKeyboardButton(text="✍️ Отклик", callback_data=f"job:proposal:{job_id}"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"job:refresh:{job_id}"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="👍", callback_data=f"job:like:{job_id}"),
            InlineKeyboardButton(text="👎", callback_data=f"job:dislike:{job_id}"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def menu_keyboard(paused: bool) -> InlineKeyboardMarkup:
    toggle = (
        InlineKeyboardButton(text="▶️ Возобновить", callback_data="menu:resume")
        if paused
        else InlineKeyboardButton(text="⏸ Пауза", callback_data="menu:pause")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статус", callback_data="menu:status"),
                InlineKeyboardButton(text="📈 Статистика", callback_data="menu:stats"),
            ],
            [
                InlineKeyboardButton(text="🎛 Фильтры", callback_data="menu:filters"),
                InlineKeyboardButton(text="🌐 Источники", callback_data="menu:sources"),
            ],
            [toggle, InlineKeyboardButton(text="🔄 Сканировать", callback_data="menu:scan")],
        ]
    )


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")]]
    )


def filters_keyboard(filters: UserFilters) -> InlineKeyboardMarkup:
    negotiable = "✅" if filters.allow_negotiable else "❌"
    unknown = "✅" if filters.allow_unknown_proposals else "❌"
    quiet = "✅" if filters.quiet_hours.enabled else "❌"
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="💰 Бюджет ₽", callback_data="flt:budget_rub"),
            InlineKeyboardButton(text="💵 Бюджет $", callback_data="flt:budget_usd"),
        ],
        [
            InlineKeyboardButton(text="💬 Макс. откликов", callback_data="flt:proposals"),
            InlineKeyboardButton(text="⭐ Мин. скор", callback_data="flt:score"),
        ],
        [
            InlineKeyboardButton(text="🕒 Свежесть", callback_data="flt:age"),
            InlineKeyboardButton(text=f"🤝 Договорная {negotiable}", callback_data="flt:negotiable"),
        ],
        [
            InlineKeyboardButton(text=f"❓ Отклики н/д {unknown}", callback_data="flt:unknown"),
            InlineKeyboardButton(text=f"🌙 Тихие часы {quiet}", callback_data="flt:quiet_toggle"),
        ],
        [
            InlineKeyboardButton(text="🌙 Начало", callback_data="flt:quiet_start"),
            InlineKeyboardButton(text="🌅 Конец", callback_data="flt:quiet_end"),
        ],
        [
            InlineKeyboardButton(text="➕ Ключевое слово", callback_data="flt:include_add"),
            InlineKeyboardButton(text="➕ Стоп-слово", callback_data="flt:exclude_add"),
        ],
    ]
    keyword_rows: list[list[InlineKeyboardButton]] = []
    for index, keyword in enumerate(filters.include_keywords[:8]):
        keyword_rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ {keyword[:24]} ✖",
                    callback_data=f"flt:kwdel:include:{index}",
                )
            ]
        )
    for index, keyword in enumerate(filters.exclude_keywords[:10]):
        keyword_rows.append(
            [
                InlineKeyboardButton(
                    text=f"⛔ {keyword[:24]} ✖",
                    callback_data=f"flt:kwdel:exclude:{index}",
                )
            ]
        )
    rows.extend(keyword_rows)
    rows.append(
        [
            InlineKeyboardButton(text="♻️ Сбросить", callback_data="flt:reset"),
            InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


STATE_ICONS = {
    "ok": "🟢 OK",
    "rate_limited": "🟡 Rate limit",
    "blocked": "🔴 Blocked",
    "parse_error": "🟠 Parse error",
    "error": "🟠 Ошибка",
    "cooldown": "🟡 Backoff",
    "disabled": "⏸ Выкл",
}


def source_icon(status: SourceStatus) -> str:
    if not status.enabled:
        return "⏸ Выкл"
    if status.cooldown_remaining_seconds() > 0:
        minutes = max(1, status.cooldown_remaining_seconds() // 60)
        return f"🟡 Backoff {minutes} мин"
    return STATE_ICONS.get(status.state, status.state)


def sources_keyboard(statuses: Sequence[SourceStatus]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for status in statuses[:20]:
        toggle_text = "⏸" if status.enabled else "▶️"
        rows.append(
            [
                InlineKeyboardButton(text=f"{toggle_text} {status.name[:18]}", callback_data=f"src:toggle:{status.name}"),
                InlineKeyboardButton(text="🔍 Проверить", callback_data=f"src:check:{status.name}"),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="➕ Добавить биржу", callback_data="src:add"),
            InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def source_kind_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📡 RSS/Atom", callback_data="addsrc:kind:rss"),
                InlineKeyboardButton(text="🌐 HTML", callback_data="addsrc:kind:html"),
            ],
            [InlineKeyboardButton(text="🧩 JSON API", callback_data="addsrc:kind:json")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="addsrc:cancel")],
        ]
    )


def source_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Сохранить", callback_data="addsrc:save"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="addsrc:cancel"),
            ]
        ]
    )


def dislike_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить стоп-слово", callback_data=f"fb:stop:{job_id}")],
            [InlineKeyboardButton(text="🙈 Скрыть источник", callback_data=f"fb:source:{job_id}")],
            [InlineKeyboardButton(text="🗑 Скрыть заказ", callback_data=f"fb:hide:{job_id}")],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="fsm:cancel")]]
    )
