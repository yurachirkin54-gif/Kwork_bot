"""Configuration layer: environment settings + hot reloadable YAML configs."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models import UserFilters

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = BASE_DIR / "config"


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


class ConfigError(RuntimeError):
    """Raised when a YAML config cannot be loaded."""


class EnvSettings(BaseSettings):
    """Values from .env / process environment."""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    telegram_bot_token: str = ""
    allowed_user_ids: str = ""
    admin_ids: str = ""
    database_url: str = "sqlite+aiosqlite:///./data/kwork.db"
    llm_enabled: bool = False
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout: float = 45.0
    http_proxy: str = ""
    freelancehunt_token: str = ""
    log_level: str = "INFO"
    config_dir: str = "config"
    timezone: str = "Europe/Moscow"

    @field_validator("llm_enabled", mode="before")
    @classmethod
    def _bool_flag(cls, value: Any) -> bool:
        return _coerce_bool(value)

    @property
    def allowed_ids(self) -> set[int]:
        return _parse_ids(self.allowed_user_ids)

    @property
    def admin_ids_set(self) -> set[int]:
        admins = _parse_ids(self.admin_ids)
        return admins or self.allowed_ids


def _parse_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for chunk in str(raw or "").replace(";", ",").replace(" ", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            result.add(int(chunk))
        except ValueError:
            continue
    return result


@lru_cache(maxsize=1)
def get_env() -> EnvSettings:
    return EnvSettings()


def reset_env_cache() -> None:
    get_env.cache_clear()


class ScoringWeights(BaseModel):
    freshness: float = 25.0
    competition: float = 25.0
    budget: float = 20.0
    client: float = 15.0
    text: float = 15.0


class ScoringConfig(BaseModel):
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    fresh_window_minutes: int = 15


class LLMConfig(BaseModel):
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    timeout: float = 45.0
    base_weight: float = 0.65

    @property
    def is_local_endpoint(self) -> bool:
        lowered = (self.base_url or "").lower()
        return any(
            host in lowered
            for host in ("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal", "ollama")
        )

    @property
    def available(self) -> bool:
        return bool(self.enabled and (self.api_key or self.is_local_endpoint))


class EngineConfig(BaseModel):
    max_concurrency: int = 6
    notify_throttle_seconds: float = 1.2
    live_update_offsets_minutes: list[int] = Field(default_factory=lambda: [3, 10, 30])
    live_stale_proposals_factor: float = 2.0
    live_stale_proposals_absolute: int = 10
    dedupe_window_hours: int = 48
    dedupe_similarity: float = 0.88


class AppConfig(BaseModel):
    """Everything from settings.yaml plus env overrides."""

    model_config = ConfigDict(extra="ignore")

    filters: UserFilters = Field(default_factory=UserFilters)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)


class SelectorConfig(BaseModel):
    """CSS selectors for GenericHTMLAdapter. `@attr` suffix extracts an attribute."""

    model_config = ConfigDict(extra="ignore")

    item: str = ""
    title: str = ""
    url: str = ""
    price: str = ""
    proposals: str = ""
    description: str = ""
    published_at: str = ""
    status: str = ""
    client_rating: str = ""
    client_reviews: str = ""
    skills: str = ""
    detail: dict[str, str] = Field(default_factory=dict)


class JsonMappingConfig(BaseModel):
    """Path mapping for GenericJSONAdapter (dot paths such as `data.jobs`)."""

    model_config = ConfigDict(extra="ignore")

    items_path: str = ""
    external_id: str = "id"
    title: str = "title"
    url: str = "url"
    description: str = "description"
    budget_min: str | None = None
    budget_max: str | None = None
    currency: str | None = None
    proposals: str | None = None
    published_at: str | None = None
    status: str | None = None
    client_rating: str | None = None
    client_reviews: str | None = None
    client_hire_rate: str | None = None
    client_verified: str | None = None
    skills: str | None = None
    category: str | None = None
    url_template: str = ""


class RegexRules(BaseModel):
    model_config = ConfigDict(extra="ignore")

    budget: str | None = None
    proposals: str | None = None
    currency: str | None = None
    published_at: str | None = None


class SourceConfig(BaseModel):
    """One marketplace definition from sources.yaml."""

    model_config = ConfigDict(extra="ignore")

    name: str
    kind: str
    enabled: bool = True
    url: str = ""
    interval_seconds: int = 90
    timeout: float = 20.0
    proxy_url: str | None = None
    use_browser: bool = False
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    selectors: SelectorConfig = Field(default_factory=SelectorConfig)
    mapping: JsonMappingConfig = Field(default_factory=JsonMappingConfig)
    regex: RegexRules = Field(default_factory=RegexRules)
    currency: str = "RUB"
    category: str | None = None
    token_env: str | None = None
    notes: str = ""
    max_items: int = 50

    @field_validator("interval_seconds", mode="before")
    @classmethod
    def _min_interval(cls, value: Any) -> int:
        return max(15, int(value or 90))

    @property
    def token(self) -> str:
        if not self.token_env:
            return ""
        return os.getenv(self.token_env, "") or getattr(get_env(), self.token_env.lower(), "")


class ConfigManager:
    """Loads YAML configs, watches mtime for hot reload and persists edits."""

    SETTINGS_FILE = "settings.yaml"
    SOURCES_FILE = "sources.yaml"

    def __init__(self, config_dir: str | Path | None = None) -> None:
        env = get_env()
        raw_dir = config_dir or env.config_dir or "config"
        path = Path(raw_dir)
        self.config_dir = path if path.is_absolute() else BASE_DIR / path
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.app = AppConfig()
        self._base_sources: list[SourceConfig] = []
        self.sources: list[SourceConfig] = []
        self._mtimes: dict[Path, float] = {}
        self._runtime_overrides: dict[str, Any] = {}
        self._source_overrides: dict[str, Any] = {}
        self.load()

    # ------------------------------------------------------------------ load
    @property
    def settings_path(self) -> Path:
        return self.config_dir / self.SETTINGS_FILE

    @property
    def sources_path(self) -> Path:
        return self.config_dir / self.SOURCES_FILE

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise ConfigError(f"Конфиг не найден: {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:  # pragma: no cover - defensive
            raise ConfigError(f"Некорректный YAML в {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"Ожидался объект в {path}")
        return data

    def load(self) -> None:
        settings_data = self._read_yaml(self.settings_path)
        # Backwards compatible: allow either top-level filters or nested app config
        if "filters" not in settings_data and any(k in settings_data for k in ("min_budget_rub", "max_proposals")):
            settings_data = {"filters": settings_data}
        try:
            app = AppConfig.model_validate(settings_data)
        except ValidationError as exc:
            raise ConfigError(f"Ошибка валидации {self.settings_path}: {exc}") from exc

        env = get_env()
        app.llm.api_key = env.llm_api_key
        if env.llm_enabled:
            app.llm.enabled = True
        if env.llm_base_url:
            app.llm.base_url = env.llm_base_url
        if env.llm_model:
            app.llm.model = env.llm_model
        if env.llm_timeout:
            app.llm.timeout = env.llm_timeout
        self.app = app

        sources_data = self._read_yaml(self.sources_path)
        raw_sources = sources_data.get("sources", [])
        if not isinstance(raw_sources, list):
            raise ConfigError(f"{self.sources_path}: 'sources' должен быть списком")
        parsed: list[SourceConfig] = []
        for item in raw_sources:
            if not isinstance(item, dict):
                continue
            try:
                parsed.append(SourceConfig.model_validate(item))
            except ValidationError as exc:
                raise ConfigError(f"Ошибка в источнике {item.get('name')!r}: {exc}") from exc
        self._base_sources = parsed
        self.sources = self._apply_source_overrides()
        self._mtimes = {
            self.settings_path: self.settings_path.stat().st_mtime,
            self.sources_path: self.sources_path.stat().st_mtime,
        }

    def maybe_reload(self) -> bool:
        """Reload YAML files if they were modified on disk."""

        changed = False
        for path in (self.settings_path, self.sources_path):
            try:
                current = path.stat().st_mtime
            except OSError:
                continue
            if self._mtimes.get(path) != current:
                changed = True
                break
        if not changed:
            return False
        overrides = dict(self._runtime_overrides)
        source_overrides = dict(self._source_overrides)
        self.load()
        self._runtime_overrides = overrides
        self._source_overrides = source_overrides
        self.sources = self._apply_source_overrides()
        return True

    # -------------------------------------------------------------- sources DB
    def set_source_overrides(self, overrides: dict[str, Any] | None) -> None:
        self._source_overrides = dict(overrides or {})
        self.sources = self._apply_source_overrides()

    @property
    def source_overrides(self) -> dict[str, Any]:
        return {
            "enabled": dict(self._source_overrides.get("enabled") or {}),
            "intervals": dict(self._source_overrides.get("intervals") or {}),
            "custom": dict(self._source_overrides.get("custom") or {}),
            "deleted": list(self._source_overrides.get("deleted") or []),
        }

    def is_builtin_source(self, name: str) -> bool:
        return any(s.name == name for s in self._base_sources)

    def _apply_source_overrides(self) -> list[SourceConfig]:
        deleted = {str(n) for n in (self._source_overrides.get("deleted") or [])}
        enabled_map = self._source_overrides.get("enabled") or {}
        intervals_map = self._source_overrides.get("intervals") or {}
        custom_map = self._source_overrides.get("custom") or {}

        by_name: dict[str, SourceConfig] = {}
        order: list[str] = []
        for base in self._base_sources:
            if base.name in deleted:
                continue
            by_name[base.name] = base.model_copy(deep=True)
            order.append(base.name)
        if isinstance(custom_map, dict):
            for name, raw in custom_map.items():
                if name in deleted or not isinstance(raw, dict):
                    continue
                try:
                    cfg = SourceConfig.model_validate(raw)
                except ValidationError:
                    continue
                if cfg.name not in by_name:
                    order.append(cfg.name)
                by_name[cfg.name] = cfg

        result: list[SourceConfig] = []
        for name in order:
            cfg = by_name[name]
            if isinstance(enabled_map, dict) and name in enabled_map:
                cfg.enabled = bool(enabled_map[name])
            if isinstance(intervals_map, dict) and name in intervals_map:
                try:
                    cfg.interval_seconds = max(15, int(intervals_map[name]))
                except (TypeError, ValueError):
                    pass
            result.append(cfg)
        return result

    # -------------------------------------------------------------- filters
    def set_runtime_overrides(self, overrides: dict[str, Any] | None) -> None:
        self._runtime_overrides = dict(overrides or {})

    @property
    def runtime_overrides(self) -> dict[str, Any]:
        return dict(self._runtime_overrides)

    def effective_filters(self) -> UserFilters:
        base = self.app.filters.model_copy(deep=True)
        if not self._runtime_overrides:
            return base
        merged = base.model_dump()
        for key, value in self._runtime_overrides.items():
            if key not in merged:
                continue
            if key == "quiet_hours" and isinstance(value, dict):
                merged["quiet_hours"] = {**merged["quiet_hours"], **value}
            else:
                merged[key] = value
        try:
            return UserFilters.model_validate(merged)
        except ValidationError:
            return base

    # -------------------------------------------------------------- sources
    def get_source(self, name: str) -> SourceConfig | None:
        for source in self.sources:
            if source.name == name:
                return source
        return None

    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled]

    def set_source_enabled(self, name: str, enabled: bool) -> bool:
        source = self.get_source(name)
        if source is None:
            return False
        source.enabled = enabled
        self._persist_sources()
        return True

    def add_source(self, config: SourceConfig) -> None:
        self.sources = [s for s in self.sources if s.name != config.name]
        self.sources.append(config)
        self._persist_sources()

    def remove_source(self, name: str) -> bool:
        before = len(self.sources)
        self.sources = [s for s in self.sources if s.name != name]
        if len(self.sources) == before:
            return False
        self._persist_sources()
        return True

    def _persist_sources(self) -> None:
        payload = {"sources": [s.model_dump(mode="json") for s in self.sources]}
        header = "# Managed by Kwork_bot - edited automatically when sources change from Telegram.\n"
        try:
            self.sources_path.write_text(
                header + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            self._mtimes[self.sources_path] = self.sources_path.stat().st_mtime
        except OSError:
            pass


def load_config(config_dir: str | Path | None = None) -> ConfigManager:
    return ConfigManager(config_dir)
