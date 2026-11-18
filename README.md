# Kwork Bot — мульти-биржевой мониторинг фриланс-заказов 24/7

Продакшн-система на **Python 3.11+ / asyncio**, которая круглосуточно опрашивает
фриланс-биржи, нормализует заказы к единой схеме, отсеивает мусор, считает
«скор лёгкости и выгоды» (0–100), при необходимости обогащает заказ через
LLM и присылает в Telegram только те задания, где реально есть шанс успеть:
свежие, с низкой конкуренцией и адекватным бюджетом.

---

## Содержание

- [Возможности](#возможности)
- [Архитектура](#архитектура)
- [Быстрый старт (Windows)](#быстрый-старт-windows)
- [Настройка Telegram](#настройка-telegram)
- [Источники: что работает из коробки](#источники-что-работает-из-коробки)
- [Настройка фильтров](#настройка-фильтров)
- [Скоринг: как считается 0–100](#скоринг-как-считается-0100)
- [Опциональный LLM-анализ](#опциональный-llm-анализ)
- [Команды и кнопки бота](#команды-и-кнопки-бота)
- [Добавление новой биржи](#добавление-новой-биржи)
- [Запуск и режимы CLI](#запуск-и-режимы-cli)
- [Тесты](#тесты)
- [Деплой 24/7 через Docker](#деплой-247-через-docker)
- [Структура проекта](#структура-проекта)
- [Диагностика проблем](#диагностика-проблем)
- [Ограничения и антибот](#ограничения-и-антибот)
- [Безопасность](#безопасность)

---

## Возможности

- **10 нативных адаптеров** + 3 универсальных движка (RSS, HTML/CSS, JSON/GraphQL):
  любая ноунейм-биржа подключается правкой YAML или мастером `/add_source` в Telegram.
- **Двухфазное сканирование**: лёгкий опрос ленты → детальная карточка только для
  новых заказов (экономим запросы и не провоцируем блокировки).
- **Единая схема `UnifiedJob`**: бюджет в оригинальной валюте + пересчёт в RUB и USD
  (ЦБ РФ → open.er-api → резервные курсы, кэш 6 часов, в том числе в SQLite).
- **Двухуровневая дедупликация**: точная `(source, external_id)` + нечёткая
  кросс-биржевая (SimHash + Jaccard + rapidfuzz), результат в таблице `cross_posts`.
- **Cold start**: при первом запуске на пустой базе старые заказы тихо индексируются,
  в Telegram уходят только реально новые (опционально `send_top_n_on_start`).
- **Умный скоринг**: свежесть, конкуренция, бюджет, надёжность заказчика, качество ТЗ.
- **Circuit breaker** на каждый источник: 403/429/капча/смена разметки → backoff
  5 мин → 15 мин → 1 час, health-алерт админу, остальные источники работают.
- **Live-обновление карточек**: через 3/10/30 минут бот перепроверяет заказ
  и тихо помечает «⚠️ Неактуально / Много откликов» без нового пуша.
- **Тихие часы + утренний дайджест** лучших всё ещё открытых заказов.
- **Управление из Telegram**: фильтры, источники, пауза, ручное сканирование,
  статистика, отклик, 👍/👎, добавление стоп-слов и новых бирж.

---

## Архитектура

```text
app/
├── main.py                  # CLI: 24/7, --once, --dry-run, логирование
├── config.py                # Pydantic Settings (.env) + горячая перезагрузка YAML
├── models.py                # UnifiedJob, UserFilters, ScoringResult, SourceStatus
├── core/
│   ├── engine.py            # планировщик, холодный старт, скоринг, live-апдейты, дайджест
│   ├── rate_limiter.py      # per-domain лимитер, jitter, circuit breaker
│   ├── currency.py          # курсы ЦБ РФ + fallback, кэш 6 ч, конвертация
│   └── deduplicator.py      # SimHash, Jaccard, rapidfuzz, кросс-биржевые дубли
├── sources/
│   ├── base.py              # 2-фазный контракт, нормализация, классификация ошибок
│   ├── http_client.py       # httpx, ETag/If-Modified-Since, прокси на источник
│   ├── browser_client.py    # опциональный Playwright (публичные SPA без капчи)
│   ├── generic_rss.py       # любой RSS/Atom
│   ├── generic_html.py      # любые CSS-селекторы + detail_selectors
│   ├── generic_json.py      # JSON/GraphQL + маппинг полей
│   ├── registry.py          # kind → адаптер
│   └── native/              # kwork, fl_ru, freelance_ru, habr_freelance,
│                            # freelancehunt, weblancer, youdo, workspace,
│                            # freelancer_com, remote_boards
├── processing/
│   ├── filters.py           # жёсткие фильтры (этап A)
│   ├── scorer.py            # детерминированный скор (этап B)
│   └── llm_analyzer.py      # опциональный LLM-анализ (этап C) + шаблон отклика
├── storage/
│   ├── database.py          # async SQLAlchemy 2.0, SQLite WAL / PostgreSQL
│   ├── orm.py               # jobs, cross_posts, sources_state, notifications,
│   │                        # settings, feedback
│   └── repository.py        # весь доступ к данным
└── telegram/
    ├── bot.py               # aiogram 3, middleware ALLOWED_USER_IDS, AppContext
    ├── notifier.py          # очередь, троттлинг, RetryAfter, HTML-карточки, live-edit
    ├── keyboards.py         # inline-клавиатуры
    └── handlers/            # menu, sources_cmd, filters_cmd, job_actions

config/settings.yaml         # фильтры, веса скоринга, LLM, движок
config/sources.yaml          # все биржи: интервалы, прокси, селекторы, маппинги
```

---

## Быстрый старт (Windows)

Требуется **Python 3.11+** (проверено на 3.11.15).

```bat
cd d:\BOTS\Kwork_bot

:: 1. чистое виртуальное окружение
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt

:: 2. конфиг
copy .env.example .env
:: откройте .env и заполните TELEGRAM_BOT_TOKEN и ALLOWED_USER_IDS

:: 3. проверка без Telegram (ничего не отправляет, БД в памяти)
.venv\Scripts\python.exe -m app.main --once --dry-run

:: 4. боевой запуск 24/7
run_bot.bat
```

`run_bot.bat` сам использует `.venv\Scripts\python.exe` и пробрасывает аргументы:
`run_bot.bat --once --dry-run`, `run_bot.bat --no-bot` и т.д.

---

## Настройка Telegram

1. **Токен бота**: напишите [@BotFather](https://t.me/BotFather) → `/newbot` →
   скопируйте токен в `TELEGRAM_BOT_TOKEN`.
2. **Свой ID**: напишите [@userinfobot](https://t.me/userinfobot) → скопируйте
   `Id` в `ALLOWED_USER_IDS` (несколько ID — через запятую).
   Без `ALLOWED_USER_IDS` бот откажет всем и покажет ваш ID в ответе — это
   защита от «случайно открытого» бота.
3. Запустите бота и отправьте `/start`.

`.env` (все переменные):

| Переменная | Назначение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен от @BotFather |
| `ALLOWED_USER_IDS` | белый список Telegram ID (обязательно) |
| `ADMIN_IDS` | кому слать health-алерты (по умолчанию = `ALLOWED_USER_IDS`) |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/kwork.db` или `postgresql+asyncpg://...` |
| `LLM_ENABLED` | `true` включает AI-этап |
| `LLM_BASE_URL` | OpenAI-совместимый endpoint (`.../v1`) |
| `LLM_API_KEY` | ключ (OpenAI / OpenRouter / Groq / локальный Ollama) |
| `LLM_MODEL` | например `gpt-4o-mini` |
| `LLM_TIMEOUT` | таймаут LLM, сек |
| `HTTP_PROXY` | общий прокси для источников без своего `proxy_url` |
| `FREELANCEHUNT_TOKEN` | токен официального API Freelancehunt (не обязателен) |
| `LOG_LEVEL` | `INFO` / `DEBUG` |
| `CONFIG_DIR` | папка с YAML (по умолчанию `config`) |

Секреты никогда не пишутся в логи: логируются только имена источников и статусы.

---

## Источники: что работает из коробки

Проверено на живых публичных страницах **17.11.2026** (Windows, RU-IP).

| Биржа | Механизм | Статус | Примечание |
|---|---|---|---|
| **Kwork** | биржа проектов `/projects` (`stateData.wantsListData`) | ✅ работает без ключа | реальные заказы покупателей, точные `kwork_count` отклики, бюджет, % найма, live-обновление |
| **FL.ru** | HTML-каталог `/projects/` + RSS fallback | ✅ работает без ключа | точное число ответов (`0`, `1`, `116`), бюджет, дата, догрузка карточки |
| **Freelance.ru** | HTML-каталог `article.task-card` | ✅ работает без ключа | отклики, бюджет, дата, категория, догрузка `.tv-hero` |
| **Freelancehunt** | API v2 + RSS fallback `https://freelancehunt.com/projects.rss` | ✅ RSS без ключа; API — с токеном | токен: <https://freelancehunt.com/my/api> → `FREELANCEHUNT_TOKEN` |
| **Weblancer** | Next.js SSR (`self.__next_f` RSC-поток) | ✅ работает без ключа и Playwright | точные отклики, бюджет в `$`, статистика заказчика |
| **Workspace** | официальный RSS `https://workspace.ru/tenders/rss/` + HTML | ✅ работает без ключа | тендеры, бюджеты, даты (при включении антибота автоматически уходит в backoff) |
| **Kadrof** | `GenericHTMLAdapter` (`div.post.project-list`) | ✅ работает без ключа | живой пример подключения сторонней биржи без Python-кода |
| **Freelancer.com** | официальное публичное REST API | ✅ работает без ключа | бюджет, `bid_count`, навыки, валюта |
| **Remote Boards** | Remotive API + RemoteOK API + WeWorkRemotely RSS | ✅ работает без ключа | отсортированы от самых свежих; старые отсекаются `max_age_minutes` |
| **Habr Freelance** | RSS | ⛔ сервис закрыт навсегда | RSS отдаёт HTML-заглушку; адаптер корректно репортит `blocked` |
| **YouDo** | JS-антибот ServicePipe + captcha | ⛔ выключен | обход не реализуется принципиально |

Важно: адаптер никогда не «падает молча». При 403/429/капче/изменении разметки
источник получает состояние `blocked` / `rate_limited` / `parse_error`, уходит
в cooldown (5 мин → 15 мин → 1 час) и присылает админу аккуратный health-алерт,
а остальные источники продолжают работать.

Управление: `/sources` → вкл/выкл, «🔍 Проверить» (мгновенный пробный прогон),
`/add_source` — мастер добавления новой биржи прямо из Telegram.

---

## Настройка фильтров

Базовые значения — `config/settings.yaml`, изменения из `/filters` сохраняются
в таблицу `settings` и применяются **мгновенно, без перезапуска**.

| Фильтр | По умолчанию | Смысл |
|---|---|---|
| `min_budget_rub` | `1000` | минимальный бюджет в рублях |
| `min_budget_usd` | `15` | минимум для валютных заказов |
| `max_proposals` | `5` | больше откликов — не присылаем |
| `allow_unknown_proposals` | `true` | присылать заказы с «отклики: н/д» |
| `min_notify_score` | `50` | порог отправки |
| `allow_negotiable` | `true` | присылать «договорную» цену |
| `max_age_minutes` | `1440` | свежесть (24 часа) |
| `include_keywords` | профиль вайбкодинг + AI/Veo/лого | боты, сайты, доработки, проверки, AI-видео, дизайн |
| `exclude_keywords` | скам/казино/лидген/1С/офлайн/… | стоп-лист |
| `include_regex` / `exclude_regex` | настроены под код/AI и отсечение очной съёмки | регулярные выражения |
| `exclude_sources` | `[]` | полностью игнорировать биржу |
| `quiet_hours` | выкл., `00:00–08:00` | тихие часы + утренний дайджест |
| `cold_start_mode` | `silent_seed` | первый запуск без спама |
| `send_top_n_on_start` | `0` | сколько самых свежих прислать при старте |

Порядок обработки: **жёсткие фильтры → детерминированный скор → (опционально) LLM**.
LLM никогда не вызывается для отфильтрованного заказа — экономия токенов.

---

## Скоринг: как считается 0–100

| Критерий | Вес | Логика |
|---|---|---|
| Свежесть | 25 | max баллов за первые 15 минут, дальше плавно вниз |
| Конкуренция | 25 | 0 откликов = max, 1–2 = высоко, 3–5 = средне, `н/д` = нейтрально |
| Бюджет | 20 | нормализация к `min_budget_rub`, демпинг и «слишком жирно» штрафуются |
| Заказчик | 15 | верификация оплаты, рейтинг, % найма, число отзывов |
| ТЗ | 15 | понятность, конкретика, стек; минус за красные флаги |

Красные флаги, которые снижают балл: «бесплатно», «за отзыв», «тестовое
бесплатно», «оплата после сдачи», «срочно на вчера», «всё под ключ за 500р».

Приоритеты в карточке: `🔥 ТОП` (≥75), `⚡ Хороший` (≥55), `📌 Подходит` (<55,
но прошёл фильтры).

---

## Опциональный LLM-анализ

Работает с любым OpenAI-совместимым API (OpenAI, OpenRouter, Groq, локальный Ollama).
Включается в `.env`:

```env
LLM_ENABLED=true
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
```

Модель обязана вернуть строгий JSON, который валидируется Pydantic:

```json
{
  "easiness_score": 8,
  "price_fairness_score": 7,
  "clarity_score": 9,
  "scam_or_toxic_risk": "low",
  "estimated_time": "2-4 часа",
  "summary_ru": "Нужен парсер каталога на Python с выгрузкой в CSV.",
  "red_flags": [],
  "draft_proposal": "Здравствуйте! Сделаю парсер..."
}
```

Итог: `base * 0.65 + llm * 0.35` с штрафом за риск. Любая ошибка сети,
таймаут, мусор вместо JSON, 400/500 — **прозрачный fallback** на базовый скор,
заказ всё равно обрабатывается. Если LLM выключен, кнопка «✍️ Отклик» отдаёт
аккуратный офлайн-шаблон.

Для Ollama:

```env
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=llama3.1
```

---

## Команды и кнопки бота

| Команда | Что делает |
|---|---|
| `/start`, `/menu` | главное меню и статус |
| `/status` | состояние, источники, очередь, тихие часы |
| `/stats` | статистика за 24 ч и 7 дней по каждой бирже + топ заказов |
| `/pause`, `/resume` | пауза/возобновление рассылки (сбор заказов не останавливается) |
| `/scan` | внеочередной цикл сканирования с отчётом |
| `/filters` | интерактивное изменение всех фильтров, стоп-слов и тихих часов |
| `/sources` | список бирж с индикаторами `🟢 OK / 🟡 Backoff / 🔴 Blocked / ⏸ Выкл` |
| `/add_source` | пошаговый мастер добавления RSS/HTML/JSON-биржи с тестовым прогоном |
| `/del_source` | удаление источника по имени (`/del_source myboard`) |

Под каждой карточкой:

- `🔗 Открыть заказ` — ссылка;
- `✍️ Отклик` — AI-черновик или офлайн-шаблон, сохраняется в БД;
- `🔄 Обновить` — перепроверить отклики/статус прямо сейчас;
- `👍` / `👎` — обратная связь; при 👎 можно добавить стоп-слово, скрыть источник или заказ.

Карточка всегда ≤ 4096 символов, весь пользовательский текст проходит `html.escape`.

---

## Добавление новой биржи

### Вариант 1 — через Telegram (без кода)

`/add_source` → выбрать `RSS` / `HTML` / `JSON` → имя → URL → селекторы →
бот делает тестовый прогон, показывает первые заказы и сохраняет в `sources.yaml`.

### Вариант 2 — через `config/sources.yaml`

**RSS/Atom (самый простой):**

```yaml
- name: my_niche_board
  kind: rss
  enabled: true
  interval_seconds: 300
  currency: RUB
  params:
    url: "https://example.com/jobs.rss"
  regex:
    budget: "(?i)(?:бюджет|цена)[:\\s]*([\\d\\s.,]+)\\s*(?:руб|₽)"
    proposals: "(?i)(\\d+)\\s*(?:отклик|заявк)"
```

**HTML/CSS:**

```yaml
- name: my_html_board
  kind: html
  enabled: true
  interval_seconds: 300
  params:
    url: "https://example.com/projects"
  selectors:
    item: "div.project-card"          # карточка заказа
    title: "a.title"                  # заголовок
    url: "a.title@href"               # @attr = взять атрибут
    price: "span.price"
    proposals: "span.responses"
    description: "div.desc"
    published_at: "time@datetime"
    status: "span.status"
    skills: "span.tag"
    # опционально: догрузка детальной страницы
    detail:
      container: "article"
      description: "#full-description"
      proposals: ".bids"
      status: ".status"
```

**JSON/GraphQL:**

```yaml
- name: my_api_board
  kind: json
  enabled: true
  params:
    url: "https://api.example.com/v1/jobs"
    method: GET
    headers: {Accept: "application/json"}
  mapping:
    items_path: "data.jobs"
    external_id: "id"
    title: "title"
    url: "url"
    description: "description"
    budget_min: "budget.from"
    budget_max: "budget.to"
    currency: "budget.currency"
    proposals: "proposals_count"
    published_at: "created_at"
    status: "status"
    url_template: "https://example.com/jobs/{external_id}"
```

Прокси и браузер задаются на уровне источника:

```yaml
  proxy_url: "http://user:pass@proxy-host:8080"
  use_browser: true   # только для публичных SPA без капчи; нужен playwright
```

После правки YAML конфиг перечитывается автоматически (hot reload) — перезапуск не нужен.

---

## Запуск и режимы CLI

```bat
:: боевой режим 24/7 (бот + планировщик + live-апдейты + дайджест)
python -m app.main

:: один цикл всех включённых бирж, с отправкой в Telegram
python -m app.main --once

:: отладка: один цикл, БД в памяти, Telegram отключён
python -m app.main --once --dry-run

:: только указанные источники
python -m app.main --once --dry-run --sources kwork,fl_ru

:: движок без бота (например, для проверки логов)
python -m app.main --no-bot
```

`--dry-run` печатает найденные, отфильтрованные и оценённые заказы с причинами.
Если `.env` не настроен, обычный запуск выводит понятную инструкцию и код 2 —
**без stacktrace**. `--help` работает всегда.

---

## Тесты

```bat
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -v
```

Сейчас: **100 тестов, 100% зелёные**. Сеть в тестах не используется:

- `test_filters_and_scorer.py` — жёсткие фильтры, пороги, красные флаги, веса;
- `test_deduplicator.py` — нормализация URL/текста, SimHash, Jaccard, кросс-дубли;
- `test_currency.py` — ЦБ РФ XML, open.er-api fallback, кэш, конвертация (`respx`);
- `test_generic_adapters.py` — RSS/HTML/JSON движки, detail-селекторы, ошибки;
- `test_native_adapters.py` — Kwork, FL.ru, Freelance.ru, Habr (закрыт), Freelancehunt
  (API/403/RSS fallback), Freelancer.com;
- `test_llm_fallback.py` — строгий JSON, retry без `response_format`, любые сбои → fallback;
- `test_storage.py` — WAL, upsert, настройки, фидбек, уведомления, статистика;
- `test_engine_flow.py` — cold start без спама, фильтры, дубли, live-апдейты,
  circuit breaker, сборка диспетчера;
- `test_notifier_format.py` — HTML-экранирование, лимит 4096, бюджеты, клавиатуры.

HTML/RSS/JSON фикстуры лежат в `tests/fixtures/`.

---

## Деплой 24/7 через Docker

```bash
cp .env.example .env      # заполнить токен и ID
docker compose up -d --build
docker compose logs -f
```

Права на `data/` и `logs/` выставляет `docker-entrypoint.sh` сам (контейнер стартует
от root, делает `chown` и дальше работает от `botuser`) — ручной `chown` не нужен.

`docker-compose.yml`:

- рестарт `unless-stopped`;
- `data/` и `logs/` смонтированы на хост (база и логи переживают пересборку);
- `config/` смонтирован rw (правки стоп-слов и вкл/выкл бирж из бота сохраняются в YAML);
- `healthcheck` проверяет свежесть `data/.heartbeat` (обновляется каждую минуту циклом движка);
- ротация логов Docker (10 МБ × 3).

Для PostgreSQL: раскомментируйте `DATABASE_URL=postgresql+asyncpg://...`
и поставьте пакет `asyncpg` (`pip install -r requirements.txt asyncpg`).

### Railway

1. New Project → Deploy from GitHub repo (подхватится `Dockerfile` + `railway.json`,
   рестарт-политика `ALWAYS`).
2. Variables: `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS` (файл `.env` в GitHub не попадает).
3. **Volume:** добавьте Volume и смонтируйте в `/app/data` — иначе база (`kwork.db`) и
   настройки, сделанные из бота (`/filters`, пауза), обнулятся при каждом деплое.
4. Сервису не нужен порт/домен — это worker. Дубль-экземпляров не запускайте
   (`numReplicas` = 1): два процесса с одним токеном мешают друг другу.

Что бот делает сам для беспрерывной работы: перезапускает упавшие фоновые задачи с
нарастающей паузой, переподключается к Telegram при обрывах сети, повторяет отправку
карточки при сбоях Telegram и корректно завершается по SIGTERM (деплой/рестарт).

> Включение/добавление бирж через `/sources` пишется в `config/sources.yaml` внутри
> контейнера и на Railway теряется при пересборке — постоянные изменения вносите в
> репозиторий. Фильтры и пауза хранятся в базе (на Volume) и сохраняются.

Альтернатива без Docker — `systemd`-юнит:

```ini
[Unit]
Description=Kwork Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/kwork_bot
ExecStart=/opt/kwork_bot/.venv/bin/python -m app.main
Restart=always
RestartSec=10
EnvironmentFile=/opt/kwork_bot/.env

[Install]
WantedBy=multi-user.target
```

---

## Структура проекта

```text
.
├── app/                  # весь код (см. архитектуру выше)
├── config/
│   ├── settings.yaml     # фильтры, скоринг, LLM, движок
│   └── sources.yaml      # биржи (нативные + примеры generic)
├── tests/                # pytest + fixtures
├── .env.example
├── .gitignore
├── pyproject.toml
├── requirements.txt
├── run_bot.bat
├── Dockerfile
├── docker-compose.yml
└── README.md
```

База (SQLite по умолчанию, WAL): `data/kwork.db`. Логи: `logs/bot.log`
(ротация 10 МБ, 14 дней) и `logs/errors.log`.

---

## Диагностика проблем

| Симптом | Что проверить |
|---|---|
| `TELEGRAM_BOT_TOKEN не задан` | скопируйте `.env.example` → `.env`, заполните токен |
| Бот молчит | ваш ID в `ALLOWED_USER_IDS`; бот сам пришлёт ваш ID при отказе |
| `🔴 Blocked` у биржи | `/sources` → «Проверить»; часто это IP/Cloudflare, попробуйте `proxy_url` |
| `🟠 Parse error` | изменилась разметка — обновите селекторы в `sources.yaml` |
| Нет уведомлений | `/status` → пауза? тихие часы? порог `min_score`? бюджет? |
| Заказы «устарели» | remote-вакансии часто старше 24 ч — поднимите `max_age_minutes` |
| Ошибки LLM в логах | бот продолжит работать на базовом скоре; проверьте `LLM_BASE_URL`/ключ |
| Дубли | работают `cross_posts` и порог `dedupe_similarity` в `settings.yaml` |

Полезно: `python -m app.main --once --dry-run --log-level DEBUG`.

---

## Ограничения и антибот

Проект **сознательно не реализует** обход CAPTCHA, Cloudflare Turnstile/reCAPTCHA,
спуфинг авторизации и прочие обходы защит. Приоритет — официальные API и RSS,
затем аккуратный опрос публичных страниц с jitter и лимитами.

Что встретилось при проверке 17.11.2026:

- **Kwork** — лента проектов не рендерится для анонимов, но официальный RSS
  отдаёт свежие проекты; детальные страницы могут требовать сессию, поэтому
  `fetch_details` по умолчанию выключен.
- **Habr Freelance** — сервис объявил о закрытии, `/tasks.rss` возвращает HTML.
- **Weblancer** — Next.js, карточки подгружаются JS; нужен `use_browser: true`
  или собственные селекторы.
- **YouDo** — ServicePipe JS challenge + captcha frame; источник выключен.
- **Workspace** — публичные тендеры за страницей «Проверка безопасности»; выключен.
- **Freelancehunt** — на части дата-центровых IP Cloudflare отвечает 403;
  официальный API с токеном решает проблему, плюс есть RSS fallback.

---

## Безопасность

- доступ к боту только по `ALLOWED_USER_IDS` (пустой список = отказ всем);
- все секреты — в `.env`, который в `.gitignore`; в логи токены не попадают;
- пользовательский текст в сообщениях экранируется, длина ограничена 4096;
- LLM получает только текст заказа (без ваших ключей и ID);
- SQL-запросы параметризованы (SQLAlchemy ORM);
- `data/`, `logs/`, `.venv/`, `*.db` исключены из git.

---

## Лицензия

MIT. Используйте ответственно: соблюдайте правила бирж и не создавайте
избыточную нагрузку на их публичные страницы.
