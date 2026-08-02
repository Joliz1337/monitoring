"""Pydantic-схемы для применения профиля firewall."""

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ProfileRule(BaseModel):
    port: int = Field(..., ge=1, le=65535)
    protocol: Literal["tcp", "udp", "any"] = "tcp"
    action: Literal["allow", "deny"] = "allow"
    from_ip: Optional[str] = None
    direction: Literal["in", "out"] = "in"
    comment: Optional[str] = ""

    @field_validator("from_ip", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        if v in ("", "any", "anywhere", "Anywhere"):
            return None
        return v


class ProfileApplyRequest(BaseModel):
    rules: list[ProfileRule]
    default_incoming: Literal["allow", "deny", "reject"] = "deny"
    default_outgoing: Literal["allow", "deny", "reject"] = "allow"
    force: bool = False


class ProfileApplyResponse(BaseModel):
    success: bool
    message: str
    rules_hash: Optional[str] = None
    rolled_back: bool = False
    error_log: Optional[str] = None


class ProfileStateResponse(BaseModel):
    active: bool
    default_incoming: str
    default_outgoing: str
    rules: list[dict]
    rules_hash: str
