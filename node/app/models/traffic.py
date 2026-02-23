"""Pydantic models for traffic API responses"""

from typing import Optional

from pydantic import BaseModel


class InterfaceSpeed(BaseModel):
    rx_bytes: int
    tx_bytes: int
    rx_bytes_per_sec: float
    tx_bytes_per_sec: float


class TotalSpeed(BaseModel):
    rx_bytes_per_sec: float
    tx_bytes_per_sec: float


class CurrentTrafficResponse(BaseModel):
    interfaces: dict[str, InterfaceSpeed]
    total: TotalSpeed


class InterfaceTraffic(BaseModel):
    rx_bytes: int
    tx_bytes: int


class HourlyTrafficPoint(BaseModel):
    hour: str
    rx_bytes: int
    tx_bytes: int
    interfaces: dict[str, InterfaceTraffic]


class HourlyTrafficResponse(BaseModel):
    hours: int
    interface: Optional[str]
    data: list[HourlyTrafficPoint]
    total_rx: int
    total_tx: int


class DailyTrafficPoint(BaseModel):
    date: str
    rx_bytes: int
    tx_bytes: int
    interfaces: dict[str, InterfaceTraffic]


class DailyTrafficResponse(BaseModel):
    days: int
    interface: Optional[str]
    data: list[DailyTrafficPoint]
    total_rx: int
    total_tx: int


class MonthlyTrafficPoint(BaseModel):
    month: str
    rx_bytes: int
    tx_bytes: int
    interfaces: dict[str, InterfaceTraffic]


class MonthlyTrafficResponse(BaseModel):
    months: int
    interface: Optional[str]
    data: list[MonthlyTrafficPoint]
    total_rx: int
    total_tx: int
