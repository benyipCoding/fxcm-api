from typing import Any, List, Optional

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


class FiftyTwoWeekResponse(BaseModel):
    low: Optional[float] = None
    high: Optional[float] = None
    range: Optional[str] = None


class QuoteResponse(BaseModel):
    symbol: str
    provider_symbol: str
    name: Optional[str] = None
    exchange: Optional[str] = None
    mic_code: Optional[str] = None
    currency: Optional[str] = None
    datetime: Optional[str] = None
    timestamp: Optional[int] = None
    last_quote_at: Optional[int] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    change: Optional[float] = None
    percent_change: Optional[float] = None
    previous_close: Optional[float] = None
    volume: Optional[int] = None
    average_volume: Optional[int] = None
    is_market_open: Optional[bool] = None
    fifty_two_week: FiftyTwoWeekResponse


class BatchQuoteItem(QuoteResponse):
    requested_symbol: str


class BatchQuoteError(BaseModel):
    requested_symbol: str
    code: int
    message: str
    data: Optional[Any] = None


class BatchQuoteResponse(BaseModel):
    requested_symbols: List[str]
    count: int
    succeeded: int
    failed: int
    items: List[BatchQuoteItem]
    errors: List[BatchQuoteError]


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


class MarketSymbolsResponse(BaseModel):
    market: str
    count: int
    items: List[SymbolSearchItem]
