"""Regression tests for timezone-aware quiet hours, word-boundary filters, digest, LLM Ollama and Telegram escaping."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

from aiogram.exceptions import TelegramNetworkError
from aiogram.types import Message
from app.config import ConfigManager, LLMConfig, reset_env_cache
from app.core.engine import Engine
from app.main import _supervise, create_services
from app.models import QuietHours, ScoringResult, ScoreComponents, UserFilters, utcnow
from app.processing.filters import HardFilter
from app.processing.llm_analyzer import LLMAnalyzer, template_proposal
from app.processing.scorer import readiness_label
from app.sources.base import BaseSourceAdapter
from app.sources.http_client import HttpClient
from app.storage.database import Database
from app.storage.orm import Job, Notification
from app.storage.repository import Repository
from app.telegram.handlers.job_actions import cb_proposal
from app.telegram.notifier import Notifier


@pytest_asyncio.fixture
async def repo(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'reg.db').as_posix()}")
    await db.init()
    yield Repository(db.session_factory), db
    await db.dispose()


def test_quiet_hours_uses_local_timezone() -> None:
    quiet = QuietHours(enabled=True, start="00:00", end="08:00")
    # 22:00 UTC == 01:00 MSK (quiet active in Europe/Moscow, NOT in UTC)
    utc_22 = datetime(2026, 11, 17, 22, 0, tzinfo=timezone.utc)
    assert quiet.is_active(now=utc_22, tz_name="Europe/Moscow") is True
    assert quiet.is_active(now=utc_22, tz_name="UTC") is False

    # 05:30 UTC == 08:30 MSK (quiet ended in Europe/Moscow)
    utc_0530 = datetime(2026, 11, 17, 5, 30, tzinfo=timezone.utc)
    assert quiet.is_active(now=utc_0530, tz_name="Europe/Moscow") is False


def test_hard_filter_word_boundaries(make_job) -> None:
    flt = HardFilter(UserFilters(exclude_keywords=["ставки", "18+"], include_keywords=[]))
    # "доставки" must NOT be blocked by "ставки"
    delivery = make_job(title="Калькулятор доставки товаров", description="Сделать расчёт стоимости доставки.")
    assert flt.check(delivery).passed is True

    # Real "ставки" MUST be blocked
    betting = make_job(title="Парсер линии", description="Нужен парсер для ставок на спорт и ставки онлайн")
    assert flt.check(betting).passed is False

    # "18+" still works
    adult = make_job(title="Контент 18+", description="Оформление канала 18+")
    assert flt.check(adult).passed is False

    # "бот" must NOT match inside "разработка"
    inc = HardFilter(UserFilters(include_keywords=["бот"]))
    site = make_job(title="Разработка сайта", description="Создание корпоративного сайта.")
    assert inc.check(site).passed is False
    bot_job = make_job(title="Нужен телеграм-ботов разработчик", description="Сделать телеграм-бота на Python")
    assert inc.check(bot_job).passed is True


def test_readiness_label_direction(make_job) -> None:
    easy = make_job(title="Парсер каталога", description="Простой скрипт парсера на Python.")
    hard = make_job(
        title="Highload ERP система",
        description="Разработка высоконагруженной ERP и блокчейн платформы с микросервисами. " * 25,
    )
    assert readiness_label(easy) == "простая"
    assert readiness_label(hard) == "сложная"


def test_parse_russian_dates_and_compound_relative() -> None:
    dt_ru = BaseSourceAdapter.parse_datetime("05.11.2026 в 09:33", tz_name="Europe/Moscow")
    assert dt_ru is not None
    assert dt_ru.month == 11 and dt_ru.day == 5
    assert dt_ru.hour == 6 and dt_ru.minute == 33  # 09:33 MSK == 06:33 UTC

    dt_word = BaseSourceAdapter.parse_datetime("17 ноября 2026")
    assert dt_word is not None
    assert dt_word.month == 11 and dt_word.day == 17

    ref = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    rel = BaseSourceAdapter.parse_relative_datetime("1 час 15 минут назад", now=ref)
    assert rel == ref - timedelta(minutes=75)


def test_extract_proposals_handles_zero_and_ignores_dates() -> None:
    assert BaseSourceAdapter.extract_proposals("нет ответов") == 0
    assert BaseSourceAdapter.extract_proposals("Откликов нет") == 0
    assert BaseSourceAdapter.extract_proposals("Крайний срок приема заявок: 27.11.2026") is None
    assert BaseSourceAdapter.extract_proposals("10 отзывов о заказчике") is None
    assert BaseSourceAdapter.extract_proposals("3 отклика") == 3


async def test_local_ollama_available_without_api_key(make_job) -> None:
    cfg = LLMConfig(enabled=True, base_url="http://localhost:11434/v1", model="llama3", api_key="")
    assert cfg.available is True

    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"easiness_score": 8, "price_fairness_score": 8, "clarity_score": 8, "scam_or_toxic_risk": "low", "estimated_time": "2 часа", "summary_ru": "Ок", "red_flags": [], "draft_proposal": "Привет"}'
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        analyzer = LLMAnalyzer(cfg, client=client)
        res = await analyzer.analyze(make_job())
        assert res is not None and res.easiness_score == 8
        assert "authorization" not in {k.lower() for k in captured_headers}


async def test_digest_candidates_respects_min_score_and_due_notifications_no_starve(repo, make_job) -> None:
    repository, database = repo
    low = make_job(source="kwork", external_id="low", status="open")
    high = make_job(source="kwork", external_id="high", status="open")
    await repository.upsert_job(low, ScoringResult(base_score=30, total_score=30, components=ScoreComponents()))
    await repository.upsert_job(high, ScoringResult(base_score=80, total_score=80, components=ScoreComponents()))

    candidates = await repository.digest_candidates(limit=10, max_proposals=5, min_score=55.0)
    assert [c.external_id for c in candidates] == ["high"]

    # Stage 1 waiting for 10m must NOT appear at 4m and starve stage 0 at 4m
    low_row = await repository.get_job_by_key("kwork", "low")
    high_row = await repository.get_job_by_key("kwork", "high")
    n1 = await repository.mark_notified(low_row.id, chat_id=1, message_id=10, score=30)
    n2 = await repository.mark_notified(high_row.id, chat_id=1, message_id=11, score=80)
    async with database.session() as session:
        s1 = await session.get(Notification, n1.id)
        s1.check_stage = 1
        s1.sent_at = utcnow() - timedelta(minutes=5)
        s2 = await session.get(Notification, n2.id)
        s2.check_stage = 0
        s2.sent_at = utcnow() - timedelta(minutes=4)
        await session.commit()

    due = await repository.due_notifications([3, 10, 30], limit=1)
    assert len(due) == 1
    assert due[0][0].id == n2.id


async def test_cross_post_updates_existing_notified_card(config_manager, repo, converter, make_job) -> None:
    repository, _db = repo
    refreshed_calls: list[tuple[int, list[str]]] = []

    class TrackingNotifier:
        async def submit(self, job, score, db_id, chat_ids=None) -> None:
            await repository.mark_notified(db_id, chat_id=1, message_id=777, score=score.total_score)

        async def refresh_card(self, notification, row, *, stale_reason=None, cross_posts=None) -> None:
            refreshed_calls.append((row.id, list(cross_posts or [])))

        async def send_health_alert(self, text: str) -> None:
            pass

    from tests.test_engine_flow import FakeAdapter, attach

    notifier = TrackingNotifier()
    engine = Engine(config_manager, repository, converter, notifier=notifier)
    a = FakeAdapter(
        "alpha",
        [
            make_job(source="alpha", external_id="seed"),
        ],
    )
    b = FakeAdapter("beta", [])
    attach(engine, a, b)
    await engine.initialize()
    await engine.scan_once(names=["alpha"])

    a.jobs = [
        make_job(
            source="alpha",
            external_id="a2",
            title="Нужен парсер каталога Avito",
            description="Собрать все карточки Avito и выгрузить в Excel. Python, requests, прокси.",
            published_at=utcnow(),
        )
    ]
    await engine.scan_once(names=["alpha"])

    b.jobs = [
        make_job(
            source="beta",
            external_id="b2",
            title="Нужен парсер каталога Avito",
            description="Собрать все карточки Avito и выгрузить в Excel. Python, requests, прокси.",
            published_at=utcnow(),
        )
    ]
    await engine.scan_once(names=["beta"])
    assert len(refreshed_calls) == 1
    assert refreshed_calls[0][1] == ["beta"]


async def test_cb_proposal_reuses_saved_draft_and_escapes_html(repo, make_job) -> None:
    repository, _db = repo
    job = make_job(source="kwork", external_id="99", title="Парсер <script> & API")
    row, _ = await repository.upsert_job(job)
    await repository.set_draft_proposal(row.id, "Готов <сделать> & быстро!")

    llm = MagicMock()
    llm.enabled = True
    llm.draft_proposal = AsyncMock(return_value="NEW")

    ctx = MagicMock()
    ctx.repo = repository
    ctx.llm = llm

    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    callback = MagicMock()
    callback.data = f"job:proposal:{row.id}"
    callback.answer = AsyncMock()
    callback.message = message

    await cb_proposal(callback, ctx)
    llm.draft_proposal.assert_not_called()
    sent_text = message.answer.call_args.args[0]
    assert "<script>" not in sent_text
    assert "&lt;script&gt;" in sent_text
    assert "&lt;сделать&gt; &amp; быстро!" in sent_text


def test_vibecoder_and_ai_creative_profile_filters(make_job) -> None:
    cfg = ConfigManager()
    flt = HardFilter(cfg.effective_filters())

    # Should PASS: bots, website creation/checks/edits, AI video/Veo, simple logos
    assert flt.check(make_job(title="Создать Telegram-бота для магазина", description="Бот на Python")).passed is True
    assert flt.check(make_job(title="Проверка сайта и доработка верстки", description="Найти баги и поправить CSS")).passed is True
    assert flt.check(make_job(title="Сгенерировать рекламный видеоролик через Veo", description="Нужно короткое AI видео 15 сек")).passed is True
    assert flt.check(make_job(title="Простой логотип и баннер", description="Нужен логотип для бренда")).passed is True

    # Should REJECT: offline camera filming, leadgen, construction, chat operator, 1C
    assert flt.check(make_job(title="Съемка видеороликов для клиники в Москве", description="Выезд оператора с камерой")).passed is False
    assert flt.check(make_job(title="Найти клиента для разработки Телеграм бота", description="Оплата за лид")).passed is False
    assert flt.check(make_job(title="Вентфасад, срочная работа", description="Чертежи стадии П")).passed is False
    assert flt.check(make_job(title="Доработать расширение 1С срочно", description="1С Бухгалтерия")).passed is False


def test_adaptive_template_proposal_by_task_type(make_job) -> None:
    video = template_proposal(make_job(title="Сделать видеоролик Reels", description="Нужен короткий ролик"))
    logo = template_proposal(make_job(title="Создать логотип", description="Простой логотип для бренда"))
    code = template_proposal(make_job(title="Доработка сайта на Python", description="Исправить баг"))
    assert "кадрам" in video
    assert "концепта" in logo
    assert "работоспособность" in code


async def test_http_client_cache_is_bounded_lru() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("If-None-Match") == "e-1":
            return httpx.Response(304)
        idx = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, text=f"body-{idx}", headers={"ETag": f"e-{idx}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as raw_client:
        http = HttpClient(client=raw_client, max_cache_entries=2)
        await http.get("https://example.test/1", min_interval=0.0)
        await http.get("https://example.test/2", min_interval=0.0)
        # Touch /1 again -> 304 moves /1 to MRU so /2 becomes LRU
        res1 = await http.get("https://example.test/1", min_interval=0.0)
        assert res1.from_cache is True and res1.text == "body-1"
        await http.get("https://example.test/3", min_interval=0.0)

        assert list(http._cache.keys()) == [
            "https://example.test/1",
            "https://example.test/3",
        ]


async def test_cleanup_old_jobs_removes_only_stale_unnotified_noise(repo, make_job) -> None:
    repository, database = repo
    old_ts = utcnow() - timedelta(days=40)

    noise = make_job(source="kwork", external_id="noise-old")
    fresh_hidden = make_job(source="kwork", external_id="noise-fresh")
    notified = make_job(source="kwork", external_id="kept-notified")
    with_fb = make_job(source="kwork", external_id="kept-fb")

    r_noise, _ = await repository.upsert_job(noise)
    r_fresh, _ = await repository.upsert_job(fresh_hidden)
    r_notified, _ = await repository.upsert_job(notified)
    r_fb, _ = await repository.upsert_job(with_fb)

    await repository.mark_notified(r_notified.id, chat_id=1, message_id=10, score=70.0)
    await repository.add_feedback(r_fb.id, user_id=1, value=-1)

    async with database.session() as session:
        for job_id in (r_noise.id, r_notified.id, r_fb.id):
            row = await session.get(Job, job_id)
            row.is_hidden = True
            row.first_seen_at = old_ts
            row.last_seen_at = old_ts
        row_fresh = await session.get(Job, r_fresh.id)
        row_fresh.is_hidden = True
        row_fresh.first_seen_at = old_ts
        # last_seen_at stays recent -> still in feed, must NOT be deleted
        await session.commit()

    deleted = await repository.cleanup_old_jobs(days=30)
    assert deleted == 1
    assert await repository.get_job_by_key("kwork", "noise-old") is None
    assert await repository.get_job_by_key("kwork", "noise-fresh") is not None
    assert await repository.get_job_by_key("kwork", "kept-notified") is not None
    assert await repository.get_job_by_key("kwork", "kept-fb") is not None


async def test_create_services_forwards_global_http_proxy(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8888")
    reset_env_cache()
    try:
        args = SimpleNamespace(dry_run=True, no_bot=True, once=True, sources="", log_level="INFO")
        services = await create_services(args)
        try:
            assert services.engine.http.proxy == "http://127.0.0.1:8888"
            assert services.engine._browser is not None
            assert services.engine._browser.proxy == "http://127.0.0.1:8888"
        finally:
            await services.close()
    finally:
        reset_env_cache()




async def test_supervise_restarts_crashed_task_until_clean_exit() -> None:
    calls = {"n": 0}

    async def flaky() -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")

    await _supervise("flaky", flaky, min_delay=0.01, max_delay=0.02)
    assert calls["n"] == 3


async def test_notifier_retries_on_network_error_then_delivers() -> None:
    attempts = {"n": 0}

    class FlakyBot:
        async def send_message(self, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise TelegramNetworkError(method=MagicMock(), message="net down")
            return SimpleNamespace(message_id=42)

    notifier = Notifier(FlakyBot(), MagicMock(), lambda: UserFilters(), chat_ids={1}, throttle_seconds=0)
    notifier.SEND_RETRY_DELAYS = (0.0, 0.01, 0.01, 0.01)
    message = await notifier._send(1, "hello")
    assert message.message_id == 42
    assert attempts["n"] == 3


async def test_notifier_gives_up_after_all_retries() -> None:
    class DeadBot:
        async def send_message(self, **kwargs):
            raise TelegramNetworkError(method=MagicMock(), message="net down")

    notifier = Notifier(DeadBot(), MagicMock(), lambda: UserFilters(), chat_ids={1}, throttle_seconds=0)
    notifier.SEND_RETRY_DELAYS = (0.0, 0.01)
    with pytest.raises(TelegramNetworkError):
        await notifier._send(1, "hello")
