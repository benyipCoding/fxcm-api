import asyncio
import hmac
import json
import os
from typing import Optional
from typing_extensions import Literal
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, StrictInt, constr
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from app.services.realtime import offer_subscriptions
from app.services.trading import demo_trading, require_demo_trading


def guard(x_fxcm_bridge_key: str = Header(default="")):
    require_demo_trading()
    expected = os.getenv("FXCM_TRADING_BRIDGE_KEY", "")
    if not expected or not hmac.compare_digest(expected, x_fxcm_bridge_key):
        raise HTTPException(403, "Trading bridge authentication required")


router = APIRouter(prefix="/trading", tags=["Demo trading"], dependencies=[Depends(guard)])


class Command(BaseModel):
    client_id: UUID
    account_id: constr(min_length=1, max_length=64)
    symbol: constr(min_length=1, max_length=64)
    action: Literal["open", "close", "risk"]
    side: Optional[Literal["Buy", "Sell"]] = None
    amount: Optional[StrictInt] = None
    trade_id: Optional[constr(min_length=1, max_length=64)] = None
    stop: Optional[float] = None
    limit: Optional[float] = None
    kind: Optional[Literal["stop", "limit"]] = None
    rate: Optional[float] = None

    class Config:
        extra = "forbid"
        allow_inf_nan = False


@router.get("/state")
def state(symbol: str = Query(..., min_length=1, max_length=64), account_id: Optional[str] = None):
    return demo_trading.snapshot(symbol, account_id)


@router.post("/commands")
def execute(command: Command):
    if command.action == "risk" and not command.kind:
        raise HTTPException(400, "缺少止损/止盈类型")
    payload = command.dict()
    payload["client_id"] = str(command.client_id)
    return demo_trading.execute(payload)


@router.get("/commands/{client_id}")
def command_result(client_id: UUID):
    return demo_trading.result(str(client_id))


@router.get("/stream")
async def stream(symbol: str = Query(..., min_length=1, max_length=64), account_id: Optional[str] = None):
    async def events():
        subscription = None
        generation = None
        try:
            while True:
                fx = await run_in_threadpool(demo_trading.service._ensure_connected)
                if fx is not generation:
                    if subscription is not None:
                        await run_in_threadpool(offer_subscriptions.release, subscription)
                    offer = demo_trading.offer(fx, symbol)
                    subscription = await run_in_threadpool(offer_subscriptions.acquire, fx, offer, demo_trading.service._connection_generation)
                    generation = fx
                snapshot = await run_in_threadpool(demo_trading.snapshot, symbol, account_id)
                yield "data: " + json.dumps(snapshot, allow_nan=False) + "\n\n"
                # Reads SDK tables in memory; this sends no broker polling request.
                await asyncio.sleep(0.75)
        except Exception:
            yield 'data: {"type":"error","message":"福汇账户连接中断，请重新连接核对持仓"}\n\n'
        finally:
            if subscription is not None:
                with anyio.CancelScope(shield=True):
                    await run_in_threadpool(offer_subscriptions.release, subscription)
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
