from typing import Optional

from pydantic import BaseModel, Field


class WildcardDeployRequest(BaseModel):
    fullchain_pem: str = Field(..., min_length=1)
    privkey_pem: str = Field(..., min_length=1)
    deploy_path: str = ""
    reload_command: str = ""
    fullchain_filename: str = "fullchain.pem"
    privkey_filename: str = "privkey.pem"
    custom_fullchain_path: Optional[str] = None
    custom_privkey_path: Optional[str] = None


class WildcardDeployResponse(BaseModel):
    success: bool
    message: str
    backup_path: Optional[str] = None
    reload_result: Optional[dict] = None


class WildcardStatusResponse(BaseModel):
    deployed: bool
    domain: Optional[str] = None
    expiry_date: Optional[str] = None
    days_left: Optional[int] = None
    cert_path: Optional[str] = None
