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
                InlineKeyboardButton(text="🔥 Лучшие заказы", callback_data="menu:top"),
                InlineKeyboardButton(text="🔄 Сканировать", callback_data="menu:scan"),
            ],
            [
                InlineKeyboardButton(text="🎛 Фильтры", callback_data="menu:filters"),
                InlineKeyboardButton(text="🌐 Источники", callback_data="menu:sources"),
            ],
            [
                InlineKeyboardButton(text="📊 Статус", callback_data="menu:status"),
                InlineKeyboardButton(text="📈 Статистика", callback_data="menu:stats"),
            ],
            [toggle],
        ]
    )


def top_jobs_keyboard(job_ids: Sequence[int]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    chunk: list[InlineKeyboardButton] = []
    for idx, job_id in enumerate(job_ids[:10], start=1):
        chunk.append(InlineKeyboardButton(text=f"📄 {idx}", callback_data=f"job:card:{job_id}"))
        if len(chunk) == 5:
            rows.append(chunk)
            chunk = []
    if chunk:
        rows.append(chunk)
    rows.append(
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="menu:top"),
            InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
            InlineKeyboardButton(
                text=f"✅ Ключевые ({len(filters.include_keywords)})",
                callback_data="flt:kw:include:0",
            ),
            InlineKeyboardButton(
                text=f"⛔ Стоп-слова ({len(filters.exclude_keywords)})",
                callback_data="flt:kw:exclude:0",
            ),
        ],
        [InlineKeyboardButton(text="🎯 Пресеты профиля", callback_data="flt:presets")],
    ]
    if filters.exclude_sources:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"👁 Вернуть скрытые биржи ({len(filters.exclude_sources)})",
                    callback_data="flt:clear_excl_src",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="♻️ Сбросить", callback_data="flt:reset"),
            InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def keywords_keyboard(
    target: str, keywords: Sequence[str], page: int = 0, per_page: int = 12
) -> InlineKeyboardMarkup:
    total = len(keywords)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    slice_items = list(keywords[start : start + per_page])
    icon = "✅" if target == "include" else "⛔"

    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for offset, word in enumerate(slice_items):
        real_index = start + offset
        pair.append(
            InlineKeyboardButton(
                text=f"{icon} {word[:18]} ✖",
                callback_data=f"flt:kwdel:{target}:{real_index}:{page}",
            )
        )
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"flt:kw:{target}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="flt:noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"flt:kw:{target}:{page + 1}"))
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data=f"flt:{target}_add"),
            InlineKeyboardButton(text="🗑 Удалить текстом", callback_data=f"flt:{target}_deltext"),
        ]
    )
    bottom: list[InlineKeyboardButton] = []
    if total > 0:
        bottom.append(InlineKeyboardButton(text="🧹 Очистить все", callback_data=f"flt:kwclear:{target}"))
    bottom.append(InlineKeyboardButton(text="⬅️ К фильтрам", callback_data="menu:filters"))
    rows.append(bottom)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def presets_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💻+🎨 Вайбкодинг + ИИ (мой профиль)", callback_data="flt:preset:default")],
            [InlineKeyboardButton(text="💻 Только код / боты / сайты", callback_data="flt:preset:code")],
            [InlineKeyboardButton(text="🎬 Только ИИ-видео / лого / дизайн", callback_data="flt:preset:ai_creative")],
            [InlineKeyboardButton(text="🌍 Вообще все заказы (без ключевых)", callback_data="flt:preset:all")],
            [InlineKeyboardButton(text="⬅️ К фильтрам", callback_data="menu:filters")],
        ]
    )


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
                InlineKeyboardButton(text=f"{toggle_text} {status.name[:16]}", callback_data=f"src:toggle:{status.name}"),
                InlineKeyboardButton(text="🔍", callback_data=f"src:check:{status.name}"),
                InlineKeyboardButton(text="⚙️", callback_data=f"src:open:{status.name}"),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="➕ Добавить биржу", callback_data="src:add"),
            InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def source_detail_keyboard(name: str, enabled: bool, interval: int, *, can_delete: bool = True) -> InlineKeyboardMarkup:
    toggle_label = "⏸ Выключить" if enabled else "▶️ Включить"
    intervals = [60, 120, 300, 600]
    interval_row = [
        InlineKeyboardButton(
            text=f"{'• ' if interval == sec else ''}{sec}с",
            callback_data=f"src:int:{name}:{sec}",
        )
        for sec in intervals
    ]
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text=toggle_label, callback_data=f"src:toggle_detail:{name}"),
            InlineKeyboardButton(text="🔍 Проверить", callback_data=f"src:check_detail:{name}"),
        ],
        interval_row,
    ]
    if can_delete:
        rows.append([InlineKeyboardButton(text="🗑 Удалить источник", callback_data=f"src:del:{name}")])
    rows.append([InlineKeyboardButton(text="⬅️ К источникам", callback_data="menu:sources")])
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


def proposal_keyboard(
    job_id: int, *, llm_enabled: bool = False, url: str | None = None
) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    open_url = _valid_url(url)
    if open_url:
        rows.append([InlineKeyboardButton(text="🔗 Открыть заказ", url=open_url)])
    if llm_enabled:
        rows.append(
            [InlineKeyboardButton(text="🔄 Перегенерировать (AI)", callback_data=f"job:proposal_regen:{job_id}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
