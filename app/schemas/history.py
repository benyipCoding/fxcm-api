from typing import List, Optional

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    service: str


class HistoryMeta(BaseModel):
    symbol: str
    provider_symbol: str
    requested_interval: str
    provider_interval: str
    price_type: str
    count: int
    currency: Optional[str] = None
    exchange: Optional[str] = None
    exchange_timezone: Optional[str] = None
    asset_type: Optional[str] = None
    subscription_status: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None


class HistoryCandle(BaseModel):
    datetime: str
    timestamp: int
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[int] = None
    bid_open: Optional[float] = None
    bid_high: Optional[float] = None
    bid_low: Optional[float] = None
    bid_close: Optional[float] = None
    ask_open: Optional[float] = None
    ask_high: Optional[float] = None
    ask_low: Optional[float] = None
    ask_close: Optional[float] = None


class HistoryResponse(BaseModel):
    meta: HistoryMeta
    values: List[HistoryCandle]


class SymbolSearchItem(BaseModel):
    symbol: str
    provider_symbol: str
    name: Optional[str] = None
    label: Optional[str] = None
    exchange: Optional[str] = None
    mic_code: Optional[str] = None
    timezone: Optional[str] = None
    market: Optional[str] = None
    asset_type: Optional[str] = None
    country: Optional[str] = None
    currency: Optional[str] = None
    provider_plan: Optional[str] = None


class SymbolSearchResponse(BaseModel):
    keyword: str
    count: int
    items: List[SymbolSearchItem]
