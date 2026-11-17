"""Engine flow: silent cold start, notifications, filtering, dedupe and live updates."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest_asyncio

from app.core.engine import Engine
from app.models import SourceStatus, utcnow
from app.sources.base import SourceBlocked
from app.storage.database import Database
from app.storage.orm import Notification
from app.storage.repository import Repository


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.health: list[str] = []
        self.stale: list[tuple[int, str]] = []
        self.digest: list = []

    async def submit(self, job, score, db_id, chat_ids=None) -> None:  # noqa: ANN001
        self.sent.append((job, score, db_id))

    async def send_health_alert(self, text: str) -> None:
        self.health.append(text)

    async def mark_stale(self, notification, row, reason: str, cross_posts=None) -> None:  # noqa: ANN001
        self.stale.append((row.id, reason))

    async def refresh_card(self, notification, row, *, stale_reason=None, cross_posts=None) -> None:  # noqa: ANN001
        if stale_reason:
            self.stale.append((row.id, stale_reason))

    async def send_digest(self, jobs) -> None:  # noqa: ANN001
        self.digest.extend(jobs)


class FakeAdapter:
    kind = "html"

    def __init__(self, name: str, jobs: list, details: dict[str, dict[str, Any]] | None = None) -> None:
        self._name = name
        self.jobs = jobs
        self.details = details or {}
        self.fail: Exception | None = None
        from app.config import SourceConfig

        self.config = SourceConfig(
            name=name, kind="html", enabled=True, interval_seconds=30, params={"fetch_details": True}
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def interval(self) -> int:
        return 30

    def needs_details(self, job) -> bool:  # noqa: ANN001
        return bool(self.details)

    async def fetch_listing(self) -> list:
        if self.fail is not None:
            raise self.fail
        return list(self.jobs)

    async def fetch_details(self, job):  # noqa: ANN001
        patch = self.details.get(job.external_id)
        if patch:
            for key, value in patch.items():
                setattr(job, key, value)
        return job


@pytest_asyncio.fixture
async def repo(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'engine.db').as_posix()}")
    await db.init()
    yield Repository(db.session_factory), db
    await db.dispose()


def attach(engine: Engine, *adapters: FakeAdapter) -> None:
    engine.adapters = list(adapters)
    engine._adapters = {adapter.name: adapter for adapter in adapters}


async def test_cold_start_seeds_without_spam(config_manager, repo, converter, make_job) -> None:
    repository, _db = repo
    notifier = FakeNotifier()
    engine = Engine(config_manager, repository, converter, notifier=notifier)
    fake = FakeAdapter(
        "fake",
        [
            make_job(source="fake", external_id="1", title="Парсер Avito"),
            make_job(source="fake", external_id="2", title="Бот для заявок"),
        ],
    )
    attach(engine, fake)
    await engine.initialize()
    assert engine._needs_seed is True

    reports = await engine.scan_once(names=["fake"])
    assert reports[0].seeded == 2
    assert reports[0].notified == 0
    assert notifier.sent == []
    assert await repository.count_jobs() == 2
    assert await repository.digest_candidates() == []
    assert engine._needs_seed is False


async def test_new_job_is_notified_on_second_scan(config_manager, repo, converter, make_job) -> None:
    repository, _db = repo
    notifier = FakeNotifier()
    engine = Engine(config_manager, repository, converter, notifier=notifier)
    first = make_job(source="fake", external_id="1", title="Парсер Avito")
    fake = FakeAdapter("fake", [first])
    attach(engine, fake)
    await engine.initialize()
    await engine.scan_once(names=["fake"])

    fresh = make_job(
        source="fake",
        external_id="2",
        title="Нужен Telegram-бот на Python",
        description="Бот для заявок. Требования: Python, aiogram, SQLite. Срок 2 дня.",
        published_at=utcnow(),
        budget_min=6000,
        budget_max=6000,
        budget_rub=6000,
        proposals_count=0,
    )
    fake.jobs.append(fresh)
    reports = await engine.scan_once(names=["fake"])

    assert reports[0].new == 1
    assert reports[0].notified == 1
    assert len(notifier.sent) == 1
    assert notifier.sent[0][0].external_id == "2"
    assert notifier.sent[0][1].total_score >= 55


async def test_hard_filter_rejects_and_remembers(config_manager, repo, converter, make_job) -> None:
    repository, _db = repo
    notifier = FakeNotifier()
    engine = Engine(config_manager, repository, converter, notifier=notifier)
    cheap = make_job(
        source="fake",
        external_id="cheap",
        title="Доработка за 500 рублей",
        budget_min=500,
        budget_max=500,
        budget_rub=500,
        budget_usd=5,
    )
    fake = FakeAdapter("fake", [cheap])
    attach(engine, fake)
    await engine.initialize()
    await engine.scan_once(names=["fake"])  # cold start seeds

    fake.jobs = [
        make_job(
            source="fake",
            external_id="cheap2",
            title="Ещё одна доработка",
            budget_min=400,
            budget_max=400,
            budget_rub=400,
            budget_usd=4,
        )
    ]
    reports = await engine.scan_once(names=["fake"])
    assert reports[0].filtered == 1
    assert notifier.sent == []
    row = await repository.get_job_by_key("fake", "cheap2")
    assert row is not None and row.is_hidden is True


async def test_below_threshold_is_not_notified(config_manager, repo, converter, make_job) -> None:
    repository, _db = repo
    notifier = FakeNotifier()
    engine = Engine(config_manager, repository, converter, notifier=notifier)
    fake = FakeAdapter("fake", [make_job(source="fake", external_id="seed")])
    attach(engine, fake)
    await engine.initialize()
    await engine.scan_once(names=["fake"])

    weak = make_job(
        source="fake",
        external_id="weak",
        title="Сделать что-то",
        description="Нужно сделать что-то по сайту.",
        budget_min=None,
        budget_max=None,
        budget_rub=None,
        budget_usd=None,
        budget_type="negotiable",
        proposals_count=5,
        published_at=utcnow() - timedelta(hours=20),
        client_verified=None,
        client_rating=None,
        client_reviews=None,
        client_hire_rate=None,
    )
    fake.jobs = [weak]
    reports = await engine.scan_once(names=["fake"])
    assert reports[0].below_threshold == 1
    assert notifier.sent == []


async def test_cross_source_duplicate_recorded(config_manager, repo, converter, make_job) -> None:
    repository, _db = repo
    notifier = FakeNotifier()
    engine = Engine(config_manager, repository, converter, notifier=notifier)
    alpha = FakeAdapter(
        "alpha",
        [
            make_job(
                source="alpha",
                external_id="a1",
                title="Нужен парсер каталога Avito",
                description="Собрать все карточки Avito и выгрузить в Excel. Python, requests, прокси.",
            )
        ],
    )
    beta = FakeAdapter(
        "beta",
        [
            make_job(
                source="beta",
                external_id="b1",
                title="Нужен парсер каталога Avito",
                description="Собрать все карточки Avito и выгрузить в Excel. Python, requests, прокси.",
            )
        ],
    )
    attach(engine, alpha, beta)
    await engine.initialize()
    await engine.scan_once(names=["alpha"])
    assert notifier.sent == []

    reports = await engine.scan_once(names=["beta"])
    assert reports[0].duplicates == 1
    assert notifier.sent == []
    alpha_row = await repository.get_job_by_key("alpha", "a1")
    posts = await repository.cross_posts_for(alpha_row.id)
    assert len(posts) == 1
    assert posts[0].source == "beta"


async def test_dry_run_does_not_touch_database(config_manager, repo, converter, make_job) -> None:
    repository, _db = repo
    notifier = FakeNotifier()
    engine = Engine(config_manager, repository, converter, notifier=notifier, dry_run=True)
    fake = FakeAdapter("fake", [make_job(source="fake", external_id="1")])
    attach(engine, fake)
    await engine.initialize()
    reports = await engine.scan_once(names=["fake"])
    assert reports[0].details
    assert await repository.count_jobs() == 0
    assert notifier.sent == []


async def test_refresh_job_updates_proposals(config_manager, repo, converter, make_job) -> None:
    repository, _db = repo
    engine = Engine(config_manager, repository, converter, notifier=FakeNotifier())
    fake = FakeAdapter("refresh", [make_job(source="refresh", external_id="1")], details={"1": {"proposals_count": 7}})
    attach(engine, fake)
    await engine.initialize()
    await engine.scan_once(names=["refresh"])
    row = await repository.get_job_by_key("refresh", "1")
    updated, message = await engine.refresh_job(row.id)
    assert updated is not None
    assert updated.proposals_count == 7
    assert "обновлен" in message.lower()


async def test_live_update_marks_closed_job_stale(config_manager, repo, converter, make_job) -> None:
    repository, database = repo
    notifier = FakeNotifier()
    engine = Engine(config_manager, repository, converter, notifier=notifier)
    fake = FakeAdapter("live", [make_job(source="live", external_id="1")])
    attach(engine, fake)
    await engine.initialize()
    await engine.scan_once(names=["live"])

    row = await repository.get_job_by_key("live", "1")
    notification = await repository.mark_notified(row.id, chat_id=1, message_id=2, score=80)
    async with database.session() as session:
        stored = await session.get(Notification, notification.id)
        stored.sent_at = utcnow() - timedelta(minutes=10)
        await session.commit()
    await repository.update_job(row.id, status="closed")

    updated = await engine.live_updates_once()
    assert updated == 1
    assert notifier.stale
    assert "закрыт" in notifier.stale[0][1]


async def test_dispatcher_wires_all_handlers(config_manager, repo, converter) -> None:
    from app.config import get_env
    from app.telegram.bot import AppContext, create_dispatcher
    from app.telegram.notifier import Notifier

    repository, _db = repo
    engine = Engine(config_manager, repository, converter)
    notifier = Notifier(None, repository, engine.filters, chat_ids={1})
    ctx = AppContext(
        engine=engine,
        repo=repository,
        config=config_manager,
        notifier=notifier,
        llm=engine.llm,
        env=get_env(),
    )
    dispatcher = create_dispatcher(ctx, {1})
    assert len(dispatcher.sub_routers) == 4


async def test_circuit_breaker_opens_after_failures(config_manager, repo, converter, make_job) -> None:
    repository, _db = repo
    notifier = FakeNotifier()
    engine = Engine(config_manager, repository, converter, notifier=notifier)
    fake = FakeAdapter("broken", [])
    fake.fail = SourceBlocked("HTTP 403: доступ запрещён", source="broken", status=403)
    attach(engine, fake)
    await engine.initialize()

    for _ in range(3):
        reports = await engine.scan_once(names=["broken"])
        assert reports[0].errors == 1

    assert notifier.health  # health alert sent once (throttled)
    state = await repository.get_source_state("broken")
    assert state is not None
    assert state.state == "blocked"
    assert state.cooldown_until is not None

    skipped = await engine.scan_once(names=["broken"])
    assert skipped[0].skipped is not None
    assert "cooldown" in skipped[0].skipped
