"""Stage C: optional LLM enrichment with strict JSON schema and graceful fallback."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from loguru import logger

from pydantic import ValidationError

from app.config import LLMConfig
from app.models import LLMAnalysis, ScoringResult, UnifiedJob, priority_for
from app.processing.scorer import human_budget

SYSTEM_PROMPT = (
    "Ты — опытный фриланс-аналитик. Оцениваешь заказы для вайбкодера и AI-креатора, который "
    "быстро делает: 1) ботов (TG/Discord/соцсети), сайты, лендинги, доработки, проверки и аудит сайтов, "
    "скрипты, парсеры, автоматизацию; 2) лёгкие визуальные и видео-задачи с помощью AI (простые логотипы, "
    "картинки, баннеры, креативы, AI-видеоролики и анимацию через Veo). "
    "Ставь высокий easiness_score (8-10) заказам, которые реально быстро сделать через код/вайбкодинг или AI/Veo, "
    "и снижай оценку, если нужна физическая офлайн-съёмка, ручная работа или многомесячный энтерпрайз. "
    "Отвечай СТРОГО валидным JSON без markdown и пояснений. Все текстовые поля — на русском, "
    "даже если заказ на английском. Будь краток и практичен, без воды."
)

USER_TEMPLATE = """Проанализируй заказ и верни JSON строго такой структуры:
{{
  "easiness_score": 1-10,            // насколько технически просто и быстро выполнить
  "price_fairness_score": 1-10,      // адекватна ли цена объёму работ
  "clarity_score": 1-10,             // понятность и полнота ТЗ
  "scam_or_toxic_risk": "low|medium|high",
  "estimated_time": "например, 2-4 часа",
  "summary_ru": "суть задачи в 2 коротких предложениях",
  "red_flags": ["..."],
  "draft_proposal": "короткий сильный первый отклик без воды, 3-5 предложений"
}}

Биржа: {source}
Заголовок: {title}
Бюджет: {budget}
Откликов: {proposals}
Навыки: {skills}
Описание:
{description}
"""


class LLMResponseError(ValueError):
    """The model returned something that is not the expected JSON."""


class LLMAnalyzer:
    """OpenAI-compatible chat completion client (OpenAI, OpenRouter, Groq, Ollama)."""

    def __init__(self, config: LLMConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None

    @property
    def enabled(self) -> bool:
        return self.config.available

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.timeout)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ prompts
    def build_messages(self, job: UnifiedJob) -> list[dict[str, str]]:
        proposals = "н/д" if job.proposals_count is None else str(job.proposals_count)
        user = USER_TEMPLATE.format(
            source=job.source,
            title=job.title,
            budget=human_budget(job),
            proposals=proposals,
            skills=", ".join(job.skills) or "не указаны",
            description=(job.description or "")[:6000],
        )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    # ------------------------------------------------------------------ requests
    async def _chat(self, messages: list[dict[str, str]]) -> str:
        base = self.config.base_url.rstrip("/")
        url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 900,
            "response_format": {"type": "json_object"},
        }
        try:
            response = await self.client.post(url, headers=headers, json=payload)
            if response.status_code >= 400:
                # Some providers (and older Ollama builds) reject response_format.
                payload.pop("response_format", None)
                response = await self.client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            raise LLMResponseError(f"LLM request failed: {exc}") from exc
        except ValueError as exc:
            raise LLMResponseError(f"LLM response is not JSON: {exc}") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError("Unexpected LLM response shape") from exc
        return str(content or "")

    @staticmethod
    def parse_analysis(content: str) -> LLMAnalysis:
        text = (content or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.IGNORECASE | re.MULTILINE).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise LLMResponseError("No JSON object found in the model output")
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMResponseError(f"Invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise LLMResponseError("JSON root must be an object")
        try:
            return LLMAnalysis.model_validate(payload)
        except ValidationError as exc:
            raise LLMResponseError(f"JSON does not match the required schema: {exc}") from exc

    async def analyze(self, job: UnifiedJob) -> LLMAnalysis | None:
        """Return None on ANY failure - the deterministic score stays authoritative."""

        if not self.enabled:
            return None
        try:
            content = await self._chat(self.build_messages(job))
            return self.parse_analysis(content)
        except LLMResponseError as exc:
            logger.warning(f"LLM пропущен для {job.source}/{job.external_id}: {exc}")
            return None
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"LLM недоступен ({exc}) - использую базовый скор")
            return None

    # ------------------------------------------------------------------ combine
    def combine(self, base: ScoringResult, analysis: LLMAnalysis) -> ScoringResult:
        llm_raw = (
            analysis.easiness_score * 0.5 + analysis.price_fairness_score * 0.3 + analysis.clarity_score * 0.2
        ) / 10.0 * 100.0
        weight = min(0.9, max(0.0, self.config.base_weight))
        total = base.base_score * weight + llm_raw * (1.0 - weight)
        if analysis.scam_or_toxic_risk == "high":
            total -= 12
        elif analysis.scam_or_toxic_risk == "medium":
            total -= 5
        total = round(max(0.0, min(100.0, total)), 1)
        result = base.model_copy(deep=True)
        result.llm = analysis
        result.llm_score = round(llm_raw, 1)
        result.total_score = total
        result.priority = priority_for(total)
        result.reasons.append(
            f"AI: лёгкость {analysis.easiness_score}/10, цена {analysis.price_fairness_score}/10, "
            f"ясность {analysis.clarity_score}/10"
        )
        if analysis.estimated_time:
            result.reasons.append(f"AI: время ~{analysis.estimated_time}")
        if analysis.scam_or_toxic_risk != "low":
            result.reasons.append(f"AI: риск {analysis.scam_or_toxic_risk}")
        return result

    async def enrich(self, job: UnifiedJob, base: ScoringResult) -> ScoringResult:
        analysis = await self.analyze(job)
        if analysis is None:
            return base
        return self.combine(base, analysis)

    async def draft_proposal(self, job: UnifiedJob) -> str | None:
        """Ask the model for a tailored first reply; None when the LLM is off."""

        if not self.enabled:
            return None
        analysis = await self.analyze(job)
        if analysis is not None and analysis.draft_proposal.strip():
            return analysis.draft_proposal.strip()
        return None


def template_proposal(job: UnifiedJob) -> str:
    """Offline fallback used by the "✍️ Отклик" button when LLM is disabled."""

    topic = job.title.strip().rstrip(".!?")
    budget = human_budget(job)
    blob = job.text_blob()
    if any(k in blob for k in ("видео", "ролик", "reels", "рилс", "shorts", "анимац", "veo", "интро")):
        profile_line = "Создам ролик аккуратно в нужном стиле и формате, с быстрыми правками по кадрам/динамике."
        questions = (
            "1) какой формат, длительность и стиль/референс ориентир;\n"
            "2) есть ли готовый текст/озвучка или нужно предложить сценарий."
        )
    elif any(k in blob for k in ("логотип", "лого", "баннер", "обложк", "инфографик", "иллюстрац", "аватар", "превью")):
        profile_line = "Подготовлю несколько вариантов концепта в нужном стиле и доведу выбранный до чистого результата."
        questions = (
            "1) есть ли примеры/референсы, которые нравятся;\n"
            "2) в каких форматах и размерах нужны исходники."
        )
    else:
        skills = ", ".join(job.skills[:3]) if job.skills else "боты, сайты, доработки, скрипты и автоматизация"
        profile_line = f"Сделаю аккуратно, проверю работоспособность и передам готовое решение. Стек: {skills}."
        questions = (
            "1) есть ли доступы/ссылка или примеры того, что уже сделано;\n"
            "2) какой результат считаем готовым (ключевой сценарий приёмки)."
        )

    return (
        f"Здравствуйте! Готов взяться за «{topic}».\n\n"
        f"{profile_line} Бюджет {budget} обсуждаем — предложу оптимальный вариант.\n\n"
        f"Уточню 2 момента, чтобы сразу стартовать:\n"
        f"{questions}\n\n"
        f"Могу начать сегодня и показать первый вариант/прототип уже в ближайшие 1-2 часа."
    )
