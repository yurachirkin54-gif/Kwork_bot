"""aiogram 3 setup: bot, dispatcher, access middleware and dependency container."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message, TelegramObject
from loguru import logger

from app.config import ConfigManager, EnvSettings
from app.core.engine import Engine
from app.processing.llm_analyzer import LLMAnalyzer
from app.storage.repository import Repository
from app.telegram.notifier import Notifier


@dataclass
class AppContext:
    """Everything handlers need."""

    engine: Engine
    repo: Repository
    config: ConfigManager
    notifier: Notifier
    llm: LLMAnalyzer
    env: EnvSettings


class AccessMiddleware(BaseMiddleware):
    """Allows only users from ALLOWED_USER_IDS."""

    def __init__(self, allowed: set[int]) -> None:
        self.allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        if not self.allowed or user.id not in self.allowed:
            logger.warning(f"Отклонён доступ для user_id={user.id} (@{user.username or '-'})")
            if isinstance(event, Message):
                reason = (
                    "ALLOWED_USER_IDS не настроен."
                    if not self.allowed
                    else "Доступ ограничен."
                )
                await event.answer(
                    f"⛔ {reason}\nВаш Telegram ID: {user.id}\n"
                    "Добавьте его в ALLOWED_USER_IDS в .env и перезапустите бота."
                )
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ Доступ ограничен", show_alert=True)
            return None
        return await handler(event, data)


def create_bot(token: str) -> Bot:
    return Bot(token=token)


def create_dispatcher(ctx: AppContext, allowed_ids: set[int]) -> Dispatcher:
    from app.telegram.handlers import filters_cmd, job_actions, menu, sources_cmd

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["ctx"] = ctx
    dispatcher.message.outer_middleware(AccessMiddleware(allowed_ids))
    dispatcher.callback_query.outer_middleware(AccessMiddleware(allowed_ids))
    dispatcher.include_router(menu.router)
    dispatcher.include_router(sources_cmd.router)
    dispatcher.include_router(filters_cmd.router)
    dispatcher.include_router(job_actions.router)
    return dispatcher
