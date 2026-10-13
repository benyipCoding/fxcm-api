"""Lazy, shared ForexConnect Offers subscriptions, exposed as SSE by the router."""
import asyncio
import contextlib
import logging
import os
from collections import deque
from threading import Lock
from time import monotonic

from forexconnect import Common, ForexConnect, fxcorepy
from starlette.concurrency import run_in_threadpool

from app.services.fxcm_service import FXCMServiceError, fxcm_history_service
from app.services.live_candles import LiveCandles

logger = logging.getLogger(__name__)


def realtime_enabled():
    return (os.getenv("APP_ENV", "prod").strip().lower() in {"local", "dev", "development"}
            and os.getenv("FXCM_REALTIME_ENABLED", "false").strip().lower() in {"true", "1"})


class OfferSubscriptions:
    """Reference count account quote subscriptions across intervals and tabs."""
    def __init__(self):
        self.lock = Lock()
        self.entries = {}
        self.pending_restores = {}

    def set_status(self, fx, offer_id, status):
        params = fxcorepy.O2GRequestParamsEnum
        request = fx.create_request({params.COMMAND: fxcorepy.Constants.Commands.SET_SUBSCRIPTION_STATUS,
                                     params.OFFER_ID: offer_id, params.SUBSCRIPTION_STATUS: status})
        fx.send_request(request)

    def acquire(self, fx, offer, generation):
        # Broker subscriptions outlive a stream generation. Keeping the symbol
        # key prevents a closing old feed from unsubscribing its replacement.
        key = offer.instrument
        with self.lock:
            entry = self.entries.get(key)
            if entry:
                if offer.subscription_status != "T":
                    self.set_status(fx, offer.offer_id, "T")
                entry["count"] += 1
                entry["fx"] = fx
                entry["offer_id"] = offer.offer_id
            else:
                original = self.pending_restores.get(key, offer.subscription_status)
                entry = {"count": 1, "original": original, "offer_id": offer.offer_id, "fx": fx}
                if offer.subscription_status != "T":
                    self.set_status(fx, offer.offer_id, "T")
                self.entries[key] = entry
                self.pending_restores.pop(key, None)
        return key

    def release(self, key):
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return
            entry["count"] -= 1
            if entry["count"]:
                return
            self.entries.pop(key)
            if entry["original"] == "T":
                return
            fx = entry["fx"]
            self.pending_restores[key] = entry["original"]
            if fxcm_history_service.is_connected(fx):
                offer = fxcm_history_service._find_offer(fx, key)
                # Respect a subscription changed elsewhere while we were open.
                if offer is not None and offer.subscription_status == "T":
                    self.set_status(fx, entry["offer_id"], entry["original"])
                self.pending_restores.pop(key, None)


offer_subscriptions = OfferSubscriptions()


class Feed:
    def __init__(self, symbol, interval):
        self.symbol, self.interval = symbol, interval
        self.lock = Lock()
        self.pending = deque(maxlen=20000)
        self.overflow = False
        self.listener = None
        self.fx = None
        self.book = None
        self.meta = None
        self.generation = None
        self.subscription = None
        self.sequence = 0

    def on_quote(self, listener, row_id, row):
        # Native SDK callbacks must be short and must not touch asyncio objects.
        if row.instrument != self.symbol:
            return
        try:
            dt = row.time
            if dt.tzinfo is None:
                from datetime import timezone
                dt = dt.replace(tzinfo=timezone.utc)
            tick = {"timestamp": dt.timestamp(), "bid": float(row.bid), "ask": float(row.ask)}
            with self.lock:
                self.sequence += 1
                tick["sequence"] = self.sequence
                if len(self.pending) == self.pending.maxlen:
                    self.overflow = True
                self.pending.append(tick)
        except Exception:
            logger.exception("Invalid FXCM offer update")

    def open(self):
        service = fxcm_history_service
        self.fx = service._ensure_connected()
        self.generation = service._connection_generation
        offer = service._find_offer(self.fx, self.symbol)
        if offer is None:
            raise FXCMServiceError(404, "FXCM instrument not found")
        self.symbol = offer.instrument
        self.subscription = offer_subscriptions.acquire(self.fx, offer, self.generation)
        # Subscribe BEFORE history: updates arriving during the snapshot are buffered.
        self.listener = Common.subscribe_table_updates(
            self.fx.get_table(ForexConnect.OFFERS), on_change_callback=self.on_quote)
        spec = service.INTERVAL_SPECS[self.interval]
        raw = service._get_history_or_raise(
            self.fx, instrument=self.symbol, timeframe=spec.provider_interval,
            start_date=None, end_date=None,
            quotes_count=service._resolve_quotes_count(self.interval, 5000))
        rows = service._aggregate_rows(service._normalize_history_rows(raw), self.interval)[-5000:]
        self.book = LiveCandles(service, self.interval, rows)
        self.meta = {"symbol": self.symbol, "interval": self.interval, "price_type": "mid"}
        # Replay buffered extrema then apply the newest offer. Never end with an
        # older buffered close than the quote observed after the snapshot.
        with self.lock:
            ticks = list(self.pending)
            self.pending.clear()
        for tick in ticks:
            self.book.update(tick)
        offer = service._find_offer(self.fx, self.symbol)
        if offer is not None:
            dt = offer.time
            from datetime import timezone
            with self.lock:
                self.sequence += 1
                sequence = self.sequence
            self.book.update({"timestamp": dt.replace(tzinfo=timezone.utc).timestamp() if dt.tzinfo is None else dt.timestamp(),
                              "bid": float(offer.bid), "ask": float(offer.ask), "sequence": sequence})
        return self.snapshot()

    def snapshot(self):
        return {"type": "snapshot", **self.meta, "candles": self.book.values(), "quote": self.book.quote}

    def poll(self):
        if (not fxcm_history_service.is_connected(self.fx) or self.fx is not fxcm_history_service._fx
                or self.generation != fxcm_history_service._connection_generation):
            raise RuntimeError("FXCM session disconnected")
        with self.lock:
            if self.overflow:
                raise RuntimeError("FXCM quote buffer overflow; resnapshot required")
            ticks = list(self.pending)
            self.pending.clear()
        changed = False
        quotes = []
        first_changed = self.book.rows[-1]["timestamp"]
        for tick in ticks:
            if self.book.update(tick):
                changed = True
                quotes.append(tick)
        if not changed:
            return None
        # Include every crossed bar, even if the client was slower than ticks.
        count = sum(row["timestamp"] >= first_changed for row in reversed(self.book.rows))
        values = self.book.values(count)
        return {"type": "candles", **self.meta, "candles": values, "quote": self.book.quote, "quotes": quotes}

    def close(self):
        try:
            if self.listener is not None:
                self.listener.unsubscribe()
                self.listener = None
        finally:
            if self.subscription is not None:
                try:
                    offer_subscriptions.release(self.subscription)
                except Exception:
                    logger.exception("Could not restore FXCM quote subscription")
                self.subscription = None
        # The session belongs to the historical service. Never log it out here.

    def fetch_closed(self):
        service = fxcm_history_service
        raw = service._get_history_or_raise(
            self.fx, instrument=self.symbol,
            timeframe=service.INTERVAL_SPECS[self.interval].provider_interval,
            start_date=None, end_date=None,
            quotes_count=service._resolve_quotes_count(self.interval, 4))
        return service._aggregate_rows(service._normalize_history_rows(raw), self.interval)

    def apply_closed(self, rows):
        # Apply the authoritative OHLC / tick volume only to completed bars;
        # never overwrite the forming candle with a delayed history response.
        replacements = {row["timestamp"]: row for row in rows
                        if row["timestamp"] < self.book.rows[-1]["timestamp"]}
        changed = []
        for index, row in enumerate(self.book.rows):
            replacement = replacements.get(row["timestamp"])
            if replacement is not None:
                self.book.rows[index] = replacement
                changed.append(self.book.service._select_prices(replacement, "mid"))
        if changed:
            return {"type": "candles", **self.meta, "candles": changed}


class RealtimeHub:
    def __init__(self):
        self.channels = {}

    async def subscribe(self, symbol, interval):
        key = (fxcm_history_service._normalize_lookup(symbol), interval)
        queue = asyncio.Queue(maxsize=32)
        channel = self.channels.get(key)
        if channel is not None and channel["task"].done():
            self.channels.pop(key, None)
            channel = None
        if channel is None:
            if len(self.channels) >= 16:
                raise FXCMServiceError(429, "Too many realtime instruments")
            channel = {"queues": set(), "feed": None, "task": None}
            self.channels[key] = channel
            channel["task"] = asyncio.create_task(self.run(channel, symbol, interval))
        channel["queues"].add(queue)
        if channel["feed"] is not None:
            queue.put_nowait(channel["feed"].snapshot())
        return key, queue

    def publish(self, channel, event):
        for queue in list(channel["queues"]):
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"type": "error", "message": "行情消费过慢，正在重新获取快照", "retryable": True})
            else:
                queue.put_nowait(event)

    async def run(self, channel, symbol, interval):
        feed = Feed(symbol, interval)
        repair = None
        try:
            opening = asyncio.create_task(run_in_threadpool(feed.open))
            try:
                snapshot = await asyncio.shield(opening)
            except asyncio.CancelledError:
                # asyncio cannot stop an SDK call. Wait for it before cleanup,
                # otherwise a late completion can leak a native table listener.
                with contextlib.suppress(Exception):
                    await opening
                raise
            channel["feed"] = feed
            self.publish(channel, snapshot)
            heartbeat = monotonic()
            while True:
                await asyncio.sleep(0.5)
                # Only drains callback memory; no polling of FXCM history.
                event = feed.poll()
                if event:
                    self.publish(channel, event)
                    if len(event["candles"]) > 1 and repair is None:
                        # Reconcile once per candle boundary, not once per tick.
                        repair = asyncio.create_task(run_in_threadpool(feed.fetch_closed))
                if repair is not None and repair.done():
                    try:
                        repaired = feed.apply_closed(repair.result())
                        if repaired:
                            self.publish(channel, repaired)
                    except Exception:
                        logger.exception("Could not reconcile completed realtime candles")
                    repair = None
                if monotonic() - heartbeat >= 10:
                    self.publish(channel, {"type": "heartbeat", "quote_time": feed.book.last_tick_time if feed.book.quote else None})
                    heartbeat = monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("FXCM realtime feed ended")
            self.publish(channel, {"type": "error", "message": str(exc),
                                   "retryable": not isinstance(exc, FXCMServiceError) or exc.status_code >= 500})
        finally:
            channel["feed"] = None
            if repair is not None:
                with contextlib.suppress(Exception):
                    await repair
            await run_in_threadpool(feed.close)

    async def unsubscribe(self, key, queue):
        channel = self.channels.get(key)
        if channel is None or queue not in channel["queues"]:
            return
        channel["queues"].discard(queue)
        if not channel["queues"]:
            self.channels.pop(key, None)
            channel["task"].cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await channel["task"]

    async def close(self):
        channels = list(self.channels.values())
        self.channels.clear()
        for channel in channels:
            channel["task"].cancel()
        await asyncio.gather(*(c["task"] for c in channels), return_exceptions=True)


realtime_hub = RealtimeHub()
