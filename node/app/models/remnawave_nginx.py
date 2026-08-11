from typing import Optional

from pydantic import BaseModel, Field


class ContainerState(BaseModel):
    exists: bool
    running: bool
    image: Optional[str] = None


class NginxDiscoverResponse(BaseModel):
    found: bool
    path: str
    dir_present: bool
    compose_present: bool
    nginx_conf_present: bool
    ssl_dir_present: bool
    mount_target: Optional[str] = None
    container: ContainerState
    config_hash: Optional[str] = None


class NginxConfigResponse(BaseModel):
    exists: bool
    content: Optional[str] = None
    hash: Optional[str] = None


class NginxStatusResponse(BaseModel):
    container: ContainerState
    config_hash: Optional[str] = None


class NginxApplyRequest(BaseModel):
    path: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    reload_after: bool = True
    restart: bool = False
    ensure_started: bool = False


class NginxApplyResponse(BaseModel):
    success: bool
    message: str
    validated: bool
    reloaded: bool
    hash: Optional[str] = None
    # Монтирование nginx.conf переведено с фрагмента на главный конфиг
    # (контейнер при этом пересоздан)
    remounted: bool = False


class NginxActionResponse(BaseModel):
    success: bool
    message: str
