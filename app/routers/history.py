from typing import Optional

from fastapi import APIRouter, Query
from starlette.concurrency import run_in_threadpool

from app.schemas.history import HealthResponse, HistoryResponse, SymbolSearchResponse
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
