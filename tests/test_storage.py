"""Storage: jobs, cross posts, notifications, settings, feedback and source state."""

from __future__ import annotations

from datetime import timedelta

import pytest_asyncio
from sqlalchemy import text

from app.models import ScoringResult, utcnow
from app.storage.database import Database
from app.storage.orm import Notification
from app.storage.repository import Repository


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'storage.db').as_posix()}")
    await db.init()
    yield db
    await db.dispose()


@pytest_asyncio.fixture
async def repo(database):
    return Repository(database.session_factory)


def score(value: float = 70.0) -> ScoringResult:
    return ScoringResult(base_score=value, total_score=value, priority="good", reasons=["тест"])


async def test_sqlite_wal_mode(database) -> None:
    async with database.session() as session:
        mode = (await session.execute(text("PRAGMA journal_mode"))).scalar()
    assert str(mode).lower() == "wal"


async def test_upsert_job_creates_then_updates(repo, make_job) -> None:
    job = make_job()
    row, created = await repo.upsert_job(job, score(72.0))
    assert created is True
    assert row.id is not None
    assert row.score == 72.0
    assert row.priority == "good"
    assert await repo.count_jobs() == 1

    updated = make_job(proposals_count=4, description=job.description + " Дополнено.")
    row2, created2 = await repo.upsert_job(updated, score(75.0))
    assert created2 is False
    assert row2.id == row.id
    assert row2.proposals_count == 4
    assert row2.score == 75.0
    assert "Дополнено" in row2.description
    assert await repo.count_jobs() == 1


async def test_known_external_ids(repo, make_job) -> None:
    await repo.upsert_job(make_job(external_id="a"))
    await repo.upsert_job(make_job(external_id="b"))
    known = await repo.known_external_ids("test", ["a", "b", "c"])
    assert known == {"a", "b"}
    assert await repo.job_exists("test", "a") is True
    assert await repo.job_exists("test", "z") is False


async def test_update_and_hide(repo, make_job) -> None:
    row, _ = await repo.upsert_job(make_job())
    updated = await repo.update_job(row.id, proposals_count=9, status="closed")
    assert updated is not None
    assert updated.proposals_count == 9
    assert updated.status == "closed"
    assert await repo.set_job_hidden(row.id, True) is True
    fetched = await repo.get_job(row.id)
    assert fetched is not None and fetched.is_hidden is True


async def test_notification_lifecycle(database, repo, make_job) -> None:
    row, _ = await repo.upsert_job(make_job())
    notification = await repo.mark_notified(row.id, chat_id=111, message_id=222, score=80.0)
    assert notification.id is not None
    assert (await repo.get_job(row.id)).notified_at is not None

    async with database.session() as session:
        stored = await session.get(Notification, notification.id)
        stored.sent_at = utcnow() - timedelta(minutes=10)
        await session.commit()

    due = await repo.due_notifications([3, 10, 30])
    assert len(due) == 1
    assert due[0][0].message_id == 222

    await repo.advance_notification(notification.id, 1, status="stale", stale_reason="закрыт")
    async with database.session() as session:
        stored = await session.get(Notification, notification.id)
        assert stored.check_stage == 1
        assert stored.status == "stale"
        assert stored.stale_reason == "закрыт"

    assert (await repo.notification_for_job(row.id)).id == notification.id


async def test_settings_roundtrip(repo) -> None:
    assert await repo.get_setting("missing", {"default": True}) == {"default": True}
    await repo.set_setting("filters_overrides", {"max_proposals": 2})
    await repo.set_setting("paused", True)
    assert await repo.get_setting("filters_overrides") == {"max_proposals": 2}
    all_settings = await repo.all_settings()
    assert all_settings["paused"] is True
    await repo.delete_setting("paused")
    assert await repo.get_setting("paused") is None


async def test_source_state_and_stats(repo, make_job) -> None:
    await repo.upsert_job(make_job(source="kwork", external_id="1"))
    await repo.upsert_job(make_job(source="fl_ru", external_id="2"))
    await repo.record_source_success("kwork", "kwork")
    await repo.record_source_failure(
        "fl_ru", "fl_ru", "blocked", "HTTP 403", cooldown_until=utcnow() + timedelta(minutes=5)
    )

    statuses = {status.name: status for status in await repo.source_statuses(24)}
    assert statuses["kwork"].state == "ok"
    assert statuses["fl_ru"].state == "blocked"
    assert statuses["fl_ru"].cooldown_remaining_seconds() > 0
    assert statuses["kwork"].jobs_24h == 1

    per_source = await repo.jobs_per_source(24)
    assert per_source == {"kwork": 1, "fl_ru": 1}

    stats = await repo.stats(24)
    assert stats["total"] == 2
    assert set(stats["by_source"]) == {"kwork", "fl_ru"}
    assert len(stats["top"]) == 2


async def test_cross_posts(repo, make_job) -> None:
    row, _ = await repo.upsert_job(make_job(source="kwork", external_id="1"))
    await repo.add_cross_post(row.id, "fl_ru", "2", "https://fl.ru/2", 0.93)
    await repo.add_cross_post(row.id, "fl_ru", "2", "https://fl.ru/2", 0.93)  # duplicate ignored
    posts = await repo.cross_posts_for(row.id)
    assert len(posts) == 1
    assert posts[0].similarity == 0.93


async def test_feedback(repo, make_job) -> None:
    row, _ = await repo.upsert_job(make_job())
    await repo.add_feedback(row.id, 1, 1, None)
    await repo.add_feedback(row.id, 1, -1, "не то")
    summary = await repo.feedback_summary()
    assert summary == {"1": 1, "-1": 1}


async def test_digest_candidates(repo, make_job) -> None:
    active, _ = await repo.upsert_job(make_job(external_id="active", score=None))
    notified, _ = await repo.upsert_job(make_job(external_id="notified"))
    await repo.mark_seeded(notified.id)
    closed, _ = await repo.upsert_job(make_job(external_id="closed", status="closed"))
    crowded, _ = await repo.upsert_job(make_job(external_id="crowded", proposals_count=12))

    candidates = await repo.digest_candidates(limit=10, max_proposals=5)
    ids = {row.external_id for row in candidates}
    assert ids == {"active"}


async def test_pending_count(repo, make_job) -> None:
    await repo.upsert_job(make_job(external_id="1"))
    await repo.upsert_job(make_job(external_id="2"))
    assert await repo.pending_count() == 2
    row = await repo.get_job_by_key("test", "1")
    await repo.mark_seeded(row.id)
    assert await repo.pending_count() == 1
