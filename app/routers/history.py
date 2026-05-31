from typing import Optional

from fastapi import APIRouter, Query
from starlette.concurrency import run_in_threadpool

from app.schemas.history import (
    BatchQuoteResponse,
    HealthResponse,
    HistoryResponse,
    MarketSymbolsResponse,
    QuoteResponse,
    SymbolSearchResponse,
)
from app.services.fxcm_service import fxcm_history_service


router = APIRouter(tags=["FXCM"])


@router.get("/health", response_model=HealthResponse, summary="Health check")
async def health_check() -> HealthResponse:
    return HealthResponse(status="ok", service="fxcm-sidecar")


@router.get(
    "/history",
    response_model=HistoryResponse,
    summary="Fetch historical candles from FXCM",
)
async def get_history(
    symbol: str = Query(..., description="Instrument symbol, for example EUR/USD."),
    interval: str = Query(
        "1h",
        description=(
            "Requested interval. Supported values: 1min, 5min, 15min, 30min, 45min, 1h, 2h, 4h, 8h, 1day, 1week, 1month."
        ),
    ),
    outputsize: int = Query(120, ge=1, le=5000),
    start_date: Optional[str] = Query(
        None,
        description="Optional ISO-like start date, for example 2026-05-01 or 2026-05-01T00:00:00Z.",
    ),
    end_date: Optional[str] = Query(
        None,
        description="Optional ISO-like end date, for example 2026-05-30 or 2026-05-30T12:00:00Z.",
    ),
    price_type: str = Query(
        "mid",
        regex="^(bid|ask|mid)$",
        description="Which OHLC prices should be surfaced as open/high/low/close.",
    ),
) -> HistoryResponse:
    return await run_in_threadpool(
        fxcm_history_service.fetch_history,
        symbol=symbol,
        interval=interval,
        outputsize=outputsize,
        start_date=start_date,
        end_date=end_date,
        price_type=price_type,
    )


@router.get(
    "/quote",
    response_model=QuoteResponse,
    summary="Fetch a latest FXCM quote snapshot",
)
async def get_quote(
    symbol: str = Query(..., description="Instrument symbol, for example EUR/USD."),
    interval: str = Query(
        "1day",
        description="Reference interval used to derive OHLC and previous close.",
    ),
    price_type: str = Query(
        "mid",
        regex="^(bid|ask|mid)$",
        description="Which quote side should be surfaced as close/open/high/low.",
    ),
) -> QuoteResponse:
    return await run_in_threadpool(
        fxcm_history_service.fetch_quote,
        symbol=symbol,
        interval=interval,
        price_type=price_type,
    )


@router.get(
    "/quotes/batch",
    response_model=BatchQuoteResponse,
    summary="Fetch latest FXCM quote snapshots in a single session",
)
async def get_quotes_batch(
    symbols: str = Query(
        ...,
        description="Comma-separated instrument symbols, for example EUR/USD,XAU/USD.",
    ),
    interval: str = Query(
        "1day",
        description="Reference interval used to derive OHLC and previous close.",
    ),
    price_type: str = Query(
        "mid",
        regex="^(bid|ask|mid)$",
        description="Which quote side should be surfaced as close/open/high/low.",
    ),
) -> BatchQuoteResponse:
    requested_symbols = [item.strip() for item in symbols.split(",")]
    return await run_in_threadpool(
        fxcm_history_service.fetch_quotes_batch,
        symbols=requested_symbols,
        interval=interval,
        price_type=price_type,
    )


@router.get(
    "/symbols/search",
    response_model=SymbolSearchResponse,
    summary="Search FXCM instruments",
)
async def search_symbols(
    keyword: str = Query(..., min_length=1, description="Instrument keyword."),
    outputsize: int = Query(10, ge=1, le=120),
) -> SymbolSearchResponse:
    return await run_in_threadpool(
        fxcm_history_service.search_instruments,
        keyword=keyword,
        outputsize=outputsize,
    )


@router.get(
    "/symbols/market",
    response_model=MarketSymbolsResponse,
    summary="List FXCM instruments for a market bucket",
)
async def list_symbols_by_market(
    market: str = Query(
        ...,
        regex="^(stocks|etf|mutual_funds|forex|crypto)$",
        description="Internal market bucket used by the main backend.",
    ),
    outputsize: int = Query(50, ge=1, le=200),
    country: Optional[str] = Query(
        None,
        description="Optional country filter, mainly useful for stock CFDs.",
    ),
) -> MarketSymbolsResponse:
    return await run_in_threadpool(
        fxcm_history_service.list_instruments_by_market,
        market=market,
        outputsize=outputsize,
        country=country,
    )
