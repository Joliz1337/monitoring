"""Схемы exit-прокси: конфиг от панели, кандидаты-выходы, результаты проверок, статус."""

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

DEFAULT_PORT = 7590
DEFAULT_INTERVAL_MINUTES = 30
DEFAULT_CHECK_TIMEOUT_SEC = 15
MAX_CUSTOM_CHECKS = 20

WARP_CANDIDATE_ID = "warp"
WARP_SOCKS_HOST = "127.0.0.1"
WARP_SOCKS_PORT = 9091

CandidateKind = Literal["ip", "warp"]
SelectMode = Literal["auto", "manual"]
GeminiVerdict = Literal["ok", "blocked", "error", "skipped"]

CHECK_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
# Проверки уезжают на хост строками TSV — табуляция и перевод строки в полях недопустимы
TSV_FORBIDDEN_RE = re.compile(r"[\t\r\n]")


def _tsv_safe(value: str) -> str:
    if TSV_FORBIDDEN_RE.search(value):
        raise ValueError("tabs and line breaks are not allowed")
    return value.strip()


class CustomCheck(BaseModel):
    """Пользовательская проверка: URL открывается через кандидата, вердикт по коду/телу/редиректу."""

    id: str = Field(..., pattern=CHECK_ID_PATTERN)
    name: str = Field(..., min_length=1, max_length=64)
    url: str = Field(..., max_length=512, pattern=r"^https?://")
    enabled: bool = True
    block_status: list[int] = Field(default_factory=list)
    block_regex: str = Field("", max_length=256)
    block_url_regex: str = Field("", max_length=256)
    expect_status: Optional[int] = Field(None, ge=100, le=599)

    @field_validator("name", "url", "block_regex", "block_url_regex")
    @classmethod
    def _no_tsv_breakers(cls, value: str) -> str:
        return _tsv_safe(value)

    @field_validator("block_status")
    @classmethod
    def _valid_statuses(cls, value: list[int]) -> list[int]:
        unique = sorted({code for code in value})
        for code in unique:
            if not 100 <= code <= 599:
                raise ValueError(f"invalid HTTP status {code}")
        return unique


class BuiltinChecks(BaseModel):
    google_country: bool = True
    google_captcha: bool = True
    gemini: bool = True


class ExitProxyConfig(BaseModel):
    enabled: bool = False
    port: int = Field(DEFAULT_PORT, ge=1024, le=65535)
    interval_minutes: int = Field(DEFAULT_INTERVAL_MINUTES, ge=1, le=1440)
    blocked_countries: list[str] = Field(default_factory=lambda: ["RU"])
    builtin_checks: BuiltinChecks = Field(default_factory=BuiltinChecks)
    custom_checks: list[CustomCheck] = Field(default_factory=list, max_length=MAX_CUSTOM_CHECKS)
    candidates_order: list[str] = Field(default_factory=list)
    candidates_disabled: list[str] = Field(default_factory=list)
    select_mode: SelectMode = "auto"
    pinned_candidate: Optional[str] = None
    check_timeout: int = Field(DEFAULT_CHECK_TIMEOUT_SEC, ge=5, le=60)

    @field_validator("blocked_countries")
    @classmethod
    def _iso_countries(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in value:
            code = raw.strip().upper()
            if not COUNTRY_RE.match(code):
                raise ValueError(f"'{raw}' is not an ISO-2 country code")
            if code not in normalized:
                normalized.append(code)
        return normalized

    @field_validator("custom_checks")
    @classmethod
    def _unique_check_ids(cls, value: list[CustomCheck]) -> list[CustomCheck]:
        seen: set[str] = set()
        for check in value:
            if check.id in seen:
                raise ValueError(f"duplicate check id '{check.id}'")
            seen.add(check.id)
        return value


class Candidate(BaseModel):
    id: str
    kind: CandidateKind
    # IP для kind=ip, host:port socks-прокси WARP для kind=warp
    address: str
    primary: bool = False
    managed: bool = False
    enabled: bool = True
    priority: int = 0


class CheckItem(BaseModel):
    name: str
    ok: bool
    status: Optional[int] = None
    detail: str = ""


class CheckResult(BaseModel):
    # Трасса через выход прошла — он вообще ходит в интернет; иначе остальные поля пусты
    ok: bool
    ip: Optional[str] = None
    country: Optional[str] = None
    country_confirm: Optional[str] = None
    captcha: bool = False
    gemini: GeminiVerdict = "skipped"
    warp: Optional[str] = None
    checks: list[CheckItem] = Field(default_factory=list)
    error: Optional[str] = None
    checked_at: str
    elapsed_ms: int = 0


class CandidateStatus(Candidate):
    # None — проверки ещё не было или транспорт до цели не дошёл
    healthy: Optional[bool] = None
    last_check: Optional[CheckResult] = None


class SelfTest(BaseModel):
    ok: bool
    ip: Optional[str] = None
    warp: Optional[str] = None
    expected: Optional[str] = None
    at: str
    error: Optional[str] = None


class ExitEvent(BaseModel):
    at: str
    kind: str
    from_candidate: Optional[str] = None
    to_candidate: Optional[str] = None
    reason: str = ""
    dropped_connections: int = 0


class ProxyStats(BaseModel):
    active_connections: int = 0
    total_connections: int = 0
    failed_connections: int = 0


class ExitProxyStatus(BaseModel):
    enabled: bool
    listening: bool
    listen_error: Optional[str] = None
    port: int
    current: Optional[str]
    select_mode: SelectMode
    pinned_candidate: Optional[str]
    candidates: list[CandidateStatus]
    warp_present: bool
    check_in_progress: bool
    last_check_at: Optional[str]
    last_check_error: Optional[str]
    self_test: Optional[SelfTest]
    stats: ProxyStats
    events: list[ExitEvent]
    script_installed: bool


class SwitchRequest(BaseModel):
    candidate: str = Field(..., min_length=1, max_length=64)


class CheckStartResponse(BaseModel):
    started: bool
    message: str = ""
