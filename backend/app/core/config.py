"""Layered configuration.

Two sources, deliberately separated by lifetime:

* **Environment / ``.env``** — anything that differs per deployment or that is
  secret (hosts, credentials, feature flags). Typed and validated by pydantic.
* **YAML under ``config/``** — operational policy that a non-developer is
  expected to tune (selectors, weights, rate limits, schedules). Hot-reloadable
  and never contains secrets.

Nothing anywhere in the codebase is allowed to hardcode a value that belongs in
either of these two places.
"""

from __future__ import annotations

import copy
import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Environment-backed settings
# ---------------------------------------------------------------------------
class _Base(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")


class ApiSettings(_Base):
    host: str = "0.0.0.0"
    port: int = 8000
    root_path: str = ""
    #: The organisation a request belongs to when it does not name one.
    #: Tenders are public and shared; CVs are partitioned by this value.
    default_tenant: str = "default"
    title: str = "SmartTender AI — Ingestion API"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    #: Static API keys accepted by the ``X-API-Key`` header. Empty disables the
    #: check entirely, which is only ever appropriate in local development.
    api_keys: list[str] = Field(default_factory=list)
    #: Requests carrying no key are rejected unless this is set.
    allow_anonymous: bool = False
    max_request_body_bytes: int = 32 * 1024 * 1024


class DatabaseSettings(_Base):
    host: str = "localhost"
    port: int = 5432
    user: str = "smarttender"
    password: str = "smarttender"
    name: str = "smarttender"
    pool_size: int = 10
    max_overflow: int = 20
    pool_recycle_seconds: int = 1800
    pool_pre_ping: bool = True
    echo: bool = False
    #: Statements exceeding this are cancelled by PostgreSQL itself, so a
    #: pathological query can never pin a worker forever.
    statement_timeout_ms: int = 30_000

    @property
    def async_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.name}"
        )

    @property
    def sync_dsn(self) -> str:
        return (
            f"postgresql+psycopg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.name}"
        )

    @property
    def alembic_dsn(self) -> str:
        return self.sync_dsn


class RedisSettings(_Base):
    host: str = "localhost"
    port: int = 6379
    password: str = ""
    broker_db: int = 0
    result_db: int = 1
    cache_db: int = 2
    socket_timeout_seconds: float = 5.0

    def _url(self, db: int) -> str:
        auth = f":{self.password}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{db}"

    @property
    def broker_url(self) -> str:
        return self._url(self.broker_db)

    @property
    def result_url(self) -> str:
        return self._url(self.result_db)

    @property
    def cache_url(self) -> str:
        return self._url(self.cache_db)


class StorageSettings(_Base):
    endpoint: str = "localhost:9000"
    #: Host to put in presigned download links. The workers reach MinIO through
    #: the container network ("minio:9000"), which no browser can resolve — a
    #: link built from `endpoint` therefore worked from inside the stack and
    #: failed from the only place it is ever clicked. Left empty, `endpoint` is
    #: used unchanged, which is correct when both sides share a hostname.
    public_endpoint: str = ""
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    secure: bool = False
    bucket: str = "smarttender-documents"
    region: str = "us-east-1"
    server_side_encryption: bool = True
    presigned_ttl_seconds: int = 900
    #: Multipart threshold. Documents are capped at 25 MB so this is mostly a
    #: safety valve for attachment bundles.
    part_size_bytes: int = 10 * 1024 * 1024
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 60.0


class UploadSettings(_Base):
    max_bytes: int = 25 * 1024 * 1024
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [".pdf", ".docx", ".html", ".htm"]
    )
    allowed_mime_types: list[str] = Field(
        default_factory=lambda: [
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "text/html",
        ]
    )
    #: Reject Office documents carrying a VBA project.
    reject_macros: bool = True
    #: Reject PDFs declaring /JavaScript, /Launch, /OpenAction or embedded files.
    reject_active_pdf_content: bool = True
    #: Reject HTML carrying script/iframe/object/event handlers.
    reject_active_html_content: bool = True
    #: Streaming read chunk. Bounds memory regardless of declared size.
    chunk_bytes: int = 1024 * 1024


class NotificationSettings(_Base):
    email_enabled: bool = False
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_tls: bool = False
    smtp_timeout_seconds: float = 15.0
    from_address: str = "smarttender@example.com"
    #: Do not email more than this many notifications to one user per digest.
    max_items_per_digest: int = 25
    #: Minimum relevance band that triggers an email (in-app always fires).
    email_min_band: str = "relevant"


class ProxySettings(_Base):
    enabled: bool = False
    urls: list[str] = Field(default_factory=list)
    rotation: str = "round_robin"


class ExtractionSettings(_Base):
    """Document text extraction and OCR.

    Extraction matters more than it looks: ``field_of_work`` and ``keywords``
    together carry 55% of the scoring weight and both read the tender's text.
    Without it, a PDF tender is scored on its title alone.
    """

    enabled: bool = True
    #: Digital text extraction is pure Python and always available. OCR needs
    #: the Tesseract binary, so it degrades to "no text" rather than failing.
    ocr_enabled: bool = True
    #: Explicit path to the Tesseract executable. Blank means "on PATH", which
    #: is the case in the container and on most Linux hosts.
    tesseract_cmd: str = ""
    #: Tesseract language packs, in priority order. Arabic is included
    #: because Tunisian and Moroccan notices mix it with French; a missing
    #: pack does not fail loudly, it silently transliterates the glyphs.
    ocr_languages: str = "fra+eng+ara"
    #: Below this many characters, a PDF page is assumed to be a scan and is
    #: sent to OCR. Digital PDFs comfortably exceed it; scanned ones yield
    #: almost nothing from the text layer.
    min_chars_before_ocr: int = 120
    #: OCR is expensive — roughly a second per page — so it is bounded.
    max_ocr_pages: int = 20
    ocr_dpi: int = 200
    #: Pages read for digital text. Tender annexes can run to hundreds.
    max_pdf_pages: int = 120
    #: Extracted text stored per document, and per tender overall.
    max_chars_per_document: int = 200_000
    max_chars_per_tender: int = 500_000
    #: Attachments larger than this are skipped: a 25 MB scanned annex would
    #: occupy an OCR worker for minutes for very little scoring value.
    max_document_bytes: int = 25 * 1024 * 1024
    #: Buyers routinely publish the whole dossier as one archive, so an
    #: unopened ZIP can hide the CCTP itself. These bound the damage a hostile
    #: or merely careless archive can do: a 40 KB file can expand to gigabytes,
    #: and an archive containing itself would otherwise recurse forever.
    archive_max_members: int = 60
    archive_max_total_bytes: int = 200 * 1024 * 1024
    archive_max_depth: int = 2


class SemanticSettings(_Base):
    #: ``lexical`` (pure python, deterministic, zero-dependency) or ``minilm``
    #: (ONNX all-MiniLM-L6-v2, requires the ``semantic`` extra).
    backend: str = "lexical"
    #: Used for *deduplication* similarity only.
    model_path: str = "./models/all-MiniLM-L6-v2"
    #: Used for *matching*. Multilingual, and chosen on a measurement rather
    #: than a preference: over ten French procurement pairs, the English
    #: all-MiniLM scored unrelated pairs at 0.166 on average while this one
    #: scored them at 0.042. Matching positives were equivalent (0.489 vs
    #: 0.494) — what the multilingual model buys is the ability to say "these
    #: have nothing to do with each other", which is what makes a threshold
    #: mean anything.
    embedding_model_path: str = "./models/paraphrase-multilingual-MiniLM-L12-v2"
    max_sequence_length: int = 256
    cache_size: int = 4096


class LlmSettings(_Base):
    """Mistral, used to refine what the deterministic pipeline could not.

    Three properties make this an *improvement* rather than a dependency, and
    all three are deliberate:

    **Absent by default.** With no key the platform behaves exactly as it does
    today. Every LLM step checks :meth:`enabled` and falls back to its
    deterministic result, so a missing key, an expired quota or an outage costs
    quality and never availability.

    **Scoped.** ``parcours_smarttender.html`` states as a cross-cutting
    guarantee that CVs never leave the server (RGPD / INPDP). Tender notices are
    public documents and lose nothing by being sent to a third party; a CV is
    personal data about an employee. ``scope`` is what keeps those two cases
    apart, and it defaults to the conservative one.

    **Bounded.** A per-document character ceiling and a timeout, because an
    LLM that silently costs more on a long dossier is how a free tier becomes a
    bill, and a slow one is how ingestion stops.
    """

    mistral_api_key: str = ""
    #: ``tenders`` | ``tenants_and_cvs`` | ``off``
    scope: str = "tenders"
    model: str = "mistral-small-latest"
    base_url: str = "https://api.mistral.ai/v1"
    timeout_seconds: float = 30.0
    max_input_chars: int = 12_000
    #: Sampling is off: these are extraction tasks with a right answer, and a
    #: creative one is a wrong one.
    temperature: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.mistral_api_key) and self.scope != "off"

    def allows(self, kind: str) -> bool:
        """Whether this document kind may be sent.

        ``kind`` is ``tender`` or ``cv``. Asked at every call site rather than
        once at startup, so flipping the scope takes effect without a redeploy
        and a future third kind fails closed.
        """
        if not self.enabled:
            return False
        if kind == "tender":
            return self.scope in {"tenders", "tenders_and_cvs"}
        if kind == "cv":
            return self.scope == "tenders_and_cvs"
        return False


class VectorSettings(_Base):
    """Where embedded passages are indexed and searched.

    Kept out of PostgreSQL deliberately: an approximate-nearest-neighbour
    search over hundreds of thousands of passages is a different access pattern
    from the platform's relational queries, and sharing one engine means a
    matching run and a dashboard refresh contend for the same buffers.
    """

    url: str = "http://localhost:6333"
    #: A matching run must never hang a request. Qdrant answers a top-k in
    #: milliseconds when healthy, so a long timeout only hides a problem.
    timeout_seconds: float = 10.0
    #: Passages of tenders, and passages of CVs. Two collections rather than
    #: one with a type field: they are searched separately, sized differently,
    #: and a CV must never surface in a tender search by accident.
    tender_collection: str = "tender_passages"
    cv_collection: str = "cv_passages"
    #: Points written per request. Large enough to be efficient, small enough
    #: that one failed batch is cheap to retry.
    upsert_batch_size: int = 128


class WorkerSettings(_Base):
    #: Soft limit raises SoftTimeLimitExceeded inside the task so it can clean
    #: up and record its own failure; the hard limit kills it. Both exist so a
    #: wedged task can never occupy a worker slot indefinitely.
    task_soft_time_limit_seconds: int = 600
    task_time_limit_seconds: int = 900
    scraping_soft_time_limit_seconds: int = 1800
    scraping_time_limit_seconds: int = 2100
    prefetch_multiplier: int = 1
    max_tasks_per_child: int = 200
    default_max_retries: int = 3
    result_ttl_seconds: int = 86_400
    #: Beat re-reads the schedule table at this cadence, so edits made through
    #: the API take effect without restarting anything.
    beat_sync_interval_seconds: int = 30
    beat_max_loop_interval_seconds: int = 60


class Settings(_Base):
    model_config = SettingsConfigDict(
        env_prefix="SMARTTENDER_",
        env_nested_delimiter="__",
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    log_format: str = "console"
    config_dir: Path = BACKEND_ROOT / "config"
    service_name: str = "smarttender-ingestion"

    api: ApiSettings = Field(default_factory=ApiSettings)
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    upload: UploadSettings = Field(default_factory=UploadSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    proxy: ProxySettings = Field(default_factory=ProxySettings)
    extraction: ExtractionSettings = Field(default_factory=ExtractionSettings)
    semantic: SemanticSettings = Field(default_factory=SemanticSettings)
    vector: VectorSettings = Field(default_factory=VectorSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)

    @field_validator("config_dir", mode="before")
    @classmethod
    def _resolve_config_dir(cls, value: Any) -> Any:
        if value in (None, ""):
            return BACKEND_ROOT / "config"
        path = Path(value)
        return path if path.is_absolute() else (BACKEND_ROOT / path).resolve()

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, value: str) -> str:
        return value.upper()

    @property
    def is_production(self) -> bool:
        return self.env.lower() in {"production", "prod"}

    @property
    def is_testing(self) -> bool:
        return self.env.lower() in {"test", "testing"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings — used by tests that patch the environment."""
    get_settings.cache_clear()
    reload_yaml_configs()


# ---------------------------------------------------------------------------
# YAML policy configuration
# ---------------------------------------------------------------------------
_yaml_cache: dict[str, dict[str, Any]] = {}
_yaml_lock = threading.RLock()


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Configuration file must contain a mapping: {path}")
    return data


def load_yaml_config(name: str, *, subdir: str | None = None) -> dict[str, Any]:
    """Load ``config/[subdir/]<name>.yaml``, cached and returned as a deep copy.

    Callers get a private copy so that mutating a returned config (a very common
    accident when merging overrides) can never corrupt another caller's view.
    """
    cache_key = f"{subdir}/{name}" if subdir else name
    with _yaml_lock:
        if cache_key not in _yaml_cache:
            base = get_settings().config_dir
            path = (base / subdir / f"{name}.yaml") if subdir else (base / f"{name}.yaml")
            _yaml_cache[cache_key] = _read_yaml(path)
        return copy.deepcopy(_yaml_cache[cache_key])


def list_yaml_configs(subdir: str) -> list[str]:
    """Return the stem of every YAML file in ``config/<subdir>/``."""
    directory = get_settings().config_dir / subdir
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.yaml"))


def reload_yaml_configs() -> None:
    """Invalidate the YAML cache so the next read picks changes up from disk."""
    with _yaml_lock:
        _yaml_cache.clear()


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base`` without mutating either.

    Mappings merge key-by-key; every other type (including lists) is replaced
    wholesale, because a partially-overridden selector list is never what the
    author meant.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def env_credential(var_name: str) -> str | None:
    """Read a credential from the environment.

    Returns ``None`` for both "unset" and "set but blank" so that an empty
    variable in a ``.env`` file disables a connector rather than making it
    attempt a login with an empty password.
    """
    value = os.environ.get(var_name, "").strip()
    return value or None


__all__ = [
    "BACKEND_ROOT",
    "Settings",
    "deep_merge",
    "env_credential",
    "get_settings",
    "list_yaml_configs",
    "load_yaml_config",
    "reload_yaml_configs",
    "reset_settings_cache",
]
