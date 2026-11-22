"""Entry point: 24/7 service, one-shot scan and dry-run diagnostics."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand
from loguru import logger

from app.config import ConfigError, ConfigManager, EnvSettings, get_env
from app.core.currency import CurrencyConverter
from app.core.engine import Engine, ScanReport
from app.processing.llm_analyzer import LLMAnalyzer
from app.sources.browser_client import BrowserClient
from app.sources.http_client import HttpClient
from app.storage.database import Database
from app.storage.repository import Repository
from app.telegram.bot import AppContext, create_bot, create_dispatcher
from app.telegram.notifier import Notifier, source_label

BASE_DIR = Path(__file__).resolve().parent.parent
HEARTBEAT_PATH = BASE_DIR / "data" / ".heartbeat"
SETUP_HINT = (
    "Не настроен Telegram-бот.\n\n"
    "1) Скопируйте .env.example в .env\n"
    "2) Получите токен у @BotFather и укажите TELEGRAM_BOT_TOKEN\n"
    "3) Узнайте свой ID у @userinfobot и укажите ALLOWED_USER_IDS\n"
    "4) Запустите снова: python -m app.main\n\n"
    "Проверить источники без Telegram можно так:\n"
    "   python -m app.main --once --dry-run"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.main",
        description="Мульти-биржевой мониторинг фриланс-заказов с Telegram-ботом.",
    )
    parser.add_argument("--once", action="store_true", help="один цикл сканирования и выход")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ничего не отправлять и не писать в базу (используется с --once)",
    )
    parser.add_argument("--no-bot", action="store_true", help="работать без Telegram-бота")
    parser.add_argument("--sources", default="", help="источники через запятую (для --once)")
    parser.add_argument("--log-level", default="", help="уровень логирования (DEBUG/INFO/WARNING)")
    return parser


def configure_logging(level: str) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        colorize=True,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
        "<cyan>{name}</cyan> - <level>{message}</level>",
    )
    logs_dir = BASE_DIR / "logs"
    try:
        logs_dir.mkdir(exist_ok=True)
        logger.add(
            logs_dir / "bot.log",
            level=level,
            rotation="10 MB",
            retention=14,
            encoding="utf-8",
            enqueue=True,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        )
        logger.add(
            logs_dir / "errors.log",
            level="ERROR",
            rotation="10 MB",
            retention=30,
            encoding="utf-8",
            enqueue=True,
        )
    except OSError as exc:
        logger.warning(f"Файловое логирование отключено ({logs_dir}): {exc}")


class Services:
    """Owns all long-lived objects so shutdown is deterministic."""

    def __init__(
        self,
        env: EnvSettings,
        config: ConfigManager,
        db: Database,
        repo: Repository,
        converter: CurrencyConverter,
        engine: Engine,
        notifier: Notifier,
        bot,
    ) -> None:
        self.env = env
        self.config = config
        self.db = db
        self.repo = repo
        self.converter = converter
        self.engine = engine
        self.notifier = notifier
        self.bot = bot

    async def close(self) -> None:
        await self.engine.stop()
        await self.notifier.stop()
        if self.bot is not None:
            try:
                await self.bot.session.close()
            except Exception:  # pragma: no cover - network
                pass
        await self.engine.aclose()
        await self.db.dispose()


async def create_services(args: argparse.Namespace) -> Services:
    env = get_env()
    config = ConfigManager()
    database_url = "sqlite+aiosqlite:///:memory:" if args.dry_run else env.database_url
    db = Database(database_url)
    await db.init()
    repo = Repository(db.session_factory)

    async def load_rates() -> dict | None:
        value = await repo.get_setting("currency_rates")
        return value if isinstance(value, dict) else None

    async def save_rates(payload: dict) -> None:
        await repo.set_setting("currency_rates", payload)

    converter = CurrencyConverter(cache_loader=load_rates, cache_saver=save_rates)
    llm = LLMAnalyzer(config.app.llm)
    bot = None
    if not args.dry_run and not args.no_bot and env.telegram_bot_token:
        bot = create_bot(env.telegram_bot_token)

    proxy = env.http_proxy or None
    http = HttpClient(proxy=proxy)
    browser = BrowserClient(proxy=proxy)
    engine = Engine(
        config,
        repo,
        converter,
        notifier=None,
        llm=llm,
        http=http,
        browser=browser,
        dry_run=args.dry_run,
        chat_ids=env.allowed_ids,
        timezone=env.timezone,
    )
    notifier = Notifier(
        bot,
        repo,
        engine.filters,
        chat_ids=env.allowed_ids,
        admin_ids=env.admin_ids_set,
        throttle_seconds=config.app.engine.notify_throttle_seconds,
    )
    engine.notifier = notifier
    return Services(env, config, db, repo, converter, engine, notifier, bot)


def _print_report(report: ScanReport, dry_run: bool) -> None:
    print(report.summary())
    if not dry_run:
        return
    shown = report.details[:12]
    for item in shown:
        score = item.get("score")
        score_text = f"{score:5.1f}" if isinstance(score, (int, float)) else "  -  "
        budget = item.get("budget")
        budget_text = f"{budget:.0f} ₽" if isinstance(budget, (int, float)) else "—"
        proposals = item.get("proposals")
        proposals_text = "н/д" if proposals is None else str(proposals)
        print(
            f"   [{score_text}] {source_label(item['source'])[:16]:<16} "
            f"{budget_text:>10} · откл {proposals_text:<4} · {item['title'][:72]}"
        )
        print(f"             {item['note']} | {item['url']}")
        if item.get("reasons"):
            print(f"             причины: {', '.join(item['reasons'][:4])}")
    if len(report.details) > len(shown):
        print(f"   … ещё {len(report.details) - len(shown)} заказов в этом источнике")


async def run_once(args: argparse.Namespace) -> int:
    services = await create_services(args)
    try:
        if not args.dry_run:
            await services.notifier.start()
        await services.engine.initialize()
        names = [chunk.strip() for chunk in args.sources.split(",") if chunk.strip()] or None
        if args.dry_run:
            print("=== DRY RUN: база в памяти, Telegram отключён, ничего не отправляется ===")
        reports = await services.engine.scan_once(names=names, force=True)
        if not reports:
            print(
                "Нет доступных источников. Проверьте config/sources.yaml "
                "(все источники могут быть выключены)."
            )
            return 1
        for report in reports:
            _print_report(report, args.dry_run)
        if not args.dry_run:
            await services.notifier.drain()
        errors = sum(1 for report in reports if report.errors)
        print(
            f"\nИтог: источников {len(reports)}, с ошибками {errors}, "
            f"новых заказов {sum(report.new for report in reports)}, "
            f"отправлено {sum(report.notified for report in reports)}"
        )
        return 0 if errors < len(reports) else 1
    finally:
        await services.close()


def _touch_heartbeat() -> None:
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


async def _live_loop(engine: Engine) -> None:
    while True:
        await asyncio.sleep(60)
        _touch_heartbeat()
        try:
            await engine.live_updates_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - keep the loop alive
            logger.exception(f"Live-обновление упало: {exc}")


async def _digest_loop(engine: Engine) -> None:
    while True:
        await asyncio.sleep(300)
        try:
            await engine.digest_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - keep the loop alive
            logger.exception(f"Дайджест упал: {exc}")


async def _supervise(
    name: str,
    factory: Callable[[], Awaitable[Any]],
    *,
    min_delay: float = 5.0,
    max_delay: float = 120.0,
) -> None:
    """Run ``factory()`` forever: any crash is logged and the task is restarted with backoff.

    A normal return means "stopped on purpose" and ends supervision.
    """

    delay = min_delay
    while True:
        started = time.monotonic()
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"Задача «{name}» упала: {exc}. Перезапуск через {delay:.0f} с")
        if time.monotonic() - started > 300:
            delay = min_delay  # worked for a while: the failure is not a crash loop
        await asyncio.sleep(delay)
        delay = min(max_delay, delay * 2)


BOT_COMMANDS = [
    BotCommand(command="menu", description="Главное меню и статус"),
    BotCommand(command="top", description="Лучшие свежие заказы сейчас"),
    BotCommand(command="filters", description="Фильтры, слова и пресеты"),
    BotCommand(command="sources", description="Биржи: вкл/выкл, интервалы, тест"),
    BotCommand(command="scan", description="Просканировать все биржи сейчас"),
    BotCommand(command="stats", description="Статистика за 24ч и 7 дней"),
    BotCommand(command="pause", description="Поставить рассылку на паузу"),
    BotCommand(command="resume", description="Возобновить рассылку"),
    BotCommand(command="add_source", description="Добавить новую биржу"),
    BotCommand(command="del_source", description="Удалить биржу по имени"),
]


async def _telegram_loop(bot: Bot, dispatcher: Dispatcher) -> None:
    """Connect to Telegram and poll. Network errors raise -> `_supervise` retries with backoff."""

    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        logger.error("Telegram отклонил токен (TELEGRAM_BOT_TOKEN неверный или отозван). Бот отключён, движок работает.")
        return
    logger.info(f"Telegram-бот запущен: @{me.username}")
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception as exc:
        logger.debug(f"Не удалось зарегистрировать меню команд: {exc}")
    await bot.delete_webhook(drop_pending_updates=False)
    await dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False)


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """SIGTERM (docker/Railway stop) and SIGINT -> graceful shutdown. No-op on Windows."""

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass


async def run_service(args: argparse.Namespace) -> int:
    services = await create_services(args)
    engine, notifier, bot = services.engine, services.notifier, services.bot
    dispatcher = None
    if bot is not None:
        ctx = AppContext(
            engine=engine,
            repo=services.repo,
            config=services.config,
            notifier=notifier,
            llm=engine.llm,
            env=services.env,
        )
        dispatcher = create_dispatcher(ctx, services.env.allowed_ids)
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    tasks: list[asyncio.Task] = []
    try:
        await engine.initialize()
        await notifier.start()
        tasks.append(asyncio.create_task(_supervise("engine", engine.run_forever), name="engine"))
        tasks.append(asyncio.create_task(_supervise("live-updates", lambda: _live_loop(engine)), name="live"))
        tasks.append(asyncio.create_task(_supervise("digest", lambda: _digest_loop(engine)), name="digest"))
        if dispatcher is not None and bot is not None:
            tasks.append(
                asyncio.create_task(
                    _supervise("telegram", lambda: _telegram_loop(bot, dispatcher), max_delay=60.0),
                    name="telegram",
                )
            )
        else:
            logger.info("Telegram-бот не запущен (--no-bot или нет токена): работает только движок")
        logger.info(
            f"Мониторинг запущен: источников {len(engine.adapters)}, "
            f"включено {sum(1 for adapter in engine.adapters if adapter.config.enabled)}"
        )
        _touch_heartbeat()
        await stop_event.wait()
        logger.info("Получен сигнал остановки, завершаю работу…")
    except asyncio.CancelledError:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await services.close()
    return 0


async def _run(args: argparse.Namespace) -> int:
    env = get_env()
    if not args.dry_run and not args.no_bot:
        if not env.telegram_bot_token or not env.allowed_ids:
            missing = []
            if not env.telegram_bot_token:
                missing.append("TELEGRAM_BOT_TOKEN")
            if not env.allowed_ids:
                missing.append("ALLOWED_USER_IDS")
            print(f"Не заполнено: {', '.join(missing)}\n")
            print(SETUP_HINT)
            return 2
    try:
        if args.once or args.dry_run:
            return await run_once(args)
        return await run_service(args)
    except ConfigError as exc:
        logger.error(f"Ошибка конфигурации: {exc}")
        print(f"Ошибка конфигурации: {exc}")
        return 2
    except OSError as exc:
        logger.error(f"Системная ошибка: {exc}")
        print(f"Системная ошибка: {exc}")
        return 2


def main() -> None:
    args = build_parser().parse_args()
    env = get_env()
    configure_logging((args.log_level or env.log_level or "INFO").upper())
    try:
        code = asyncio.run(_run(args))
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C")
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
