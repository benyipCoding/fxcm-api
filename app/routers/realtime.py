import json
from typing import Optional
import anyio

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.services.fxcm_service import FXCMHistoryService, FXCMServiceError, fxcm_history_service
from app.services.realtime import realtime_enabled, realtime_hub

router = APIRouter(prefix="/realtime", tags=["FXCM realtime"])


@router.get("/stream")
async def stream(symbol: str = Query(..., min_length=1, max_length=64), interval: str = "1h",
                 snapshot_size: int = Query(256, ge=2, le=FXCMHistoryService.MAX_OUTPUT_BARS),
                 seed_timestamp: Optional[float] = Query(None, gt=0),
                 seed_price: Optional[float] = Query(None, gt=0)):
    if not realtime_enabled():
        raise HTTPException(404, "Realtime is disabled outside local development")
    interval = fxcm_history_service._resolve_interval_name(interval)
    fxcm_history_service._get_interval_spec(interval)

    async def events():
        try:
            key, queue = await realtime_hub.subscribe(
                symbol, interval, snapshot_size,
                seed_timestamp=seed_timestamp if seed_price is not None else None,
                seed_price=seed_price if seed_timestamp is not None else None,
            )
        except FXCMServiceError as exc:
            yield "data: " + json.dumps({"type": "error", "message": exc.message, "retryable": False}) + "\n\n"
            return
        try:
            yield ": connected\n\n"
            while True:
                event = await queue.get()
                yield "data: " + json.dumps(event, allow_nan=False) + "\n\n"
                if event["type"] == "error":
                    break
        finally:
            # Starlette cancels the iterator on disconnect; shield cleanup so
            # unsubscribe / SDK subscription restoration can finish.
            with anyio.CancelScope(shield=True):
                await realtime_hub.unsubscribe(key, queue)

    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
