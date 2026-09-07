"""Pydantic-схемы разведки и вырезания средств доступа хостера."""

from typing import Literal, Optional

from pydantic import BaseModel, Field

Severity = Literal["info", "warning", "danger"]

MAX_PURGE_IDS = 200


class HosterFinding(BaseModel):
    id: str
    category: str
    title: str
    detail: str
    severity: Severity
    # Удаление может отрезать аварийный доступ к серверу (qemu-ga, cloud-init,
    # sshd, чужие ключи, лишние юзеры) — фронт помечает такие красным.
    access_critical: bool
    default_selected: bool
    remove_hint: str = ""


class HosterScanResponse(BaseModel):
    supported: bool = True
    hoster_hint: Optional[str] = None
    generated_at: str
    findings: list[HosterFinding]


class HosterPurgeRequest(BaseModel):
    finding_ids: list[str] = Field(default_factory=list, max_length=MAX_PURGE_IDS)
    confirm: bool = False


class HosterPurgeResultItem(BaseModel):
    id: str
    ok: bool
    message: str


class HosterPurgeResponse(BaseModel):
    results: list[HosterPurgeResultItem]
    reboot_recommended: bool = False
