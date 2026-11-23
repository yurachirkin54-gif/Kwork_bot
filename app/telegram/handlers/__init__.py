"""Telegram handlers package."""

from __future__ import annotations

import html

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

MAX_TEXT = 3800


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Split long text on whitespace boundaries."""

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


async def safe_edit(
    callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    message = callback.message
    if not isinstance(message, Message):
        return
    if len(text) > 4000:
        text = text[:3990].rstrip() + "\n…"
    try:
        await message.edit_text(
            text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        # Message too old / cannot be edited - send a fresh one.
        try:
            await message.answer(
                text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True
            )
        except TelegramBadRequest:
            pass


async def reply_long(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    parts = split_text(text)
    for index, part in enumerate(parts):
        await message.answer(
            part,
            parse_mode="HTML",
            reply_markup=markup if index == len(parts) - 1 else None,
        )


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=False)
