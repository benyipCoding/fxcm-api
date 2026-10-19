import asyncio
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("PASSWORD", "test")
os.environ.setdefault("FXCM_URL", "http://www.fxcorporate.com/Hosts.jsp")

from forexconnect import fxcorepy
from app.services.fxcm_service import FXCMHistoryService
from app.services.live_candles import LiveCandles
from app.services.realtime import Feed, OfferSubscriptions, RealtimeHub, realtime_enabled


def epoch(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def row(value, bid=10, ask=12):
    return {"timestamp": int(epoch(value)), "datetime": value + "+00:00", "volume": 100,
            **{side + "_" + field: price for side, price in (("bid", bid), ("ask", ask))
               for field in ("open", "high", "low", "close")}}


def book(interval="1min", time="2026-10-13T13:00:00"):
    return LiveCandles(FXCMHistoryService.__new__(FXCMHistoryService), interval, [row(time)])


class CandleTest(unittest.TestCase):
    def test_intrabar_high_low_close_and_mid_parity(self):
        live = book()
        t = epoch("2026-10-13T13:00:01")
        live.update({"timestamp": t, "bid": 13, "ask": 15})
        live.update({"timestamp": t + 1, "bid": 9, "ask": 11})
        value = live.values()[0]
        self.assertEqual((value["open"], value["high"], value["low"], value["close"]), (11, 14, 10, 10))
        self.assertIsNone(value["volume"])

    def test_rollover_previous_close_no_synthetic_gap_bars(self):
        live = book()
        live.update({"timestamp": epoch("2026-10-13T13:03:10"), "bid": 13, "ask": 15})
        self.assertEqual(len(live.rows), 2)
        self.assertEqual(live.values()[-1]["open"], 11)
        self.assertEqual(live.values()[-1]["timestamp"], epoch("2026-10-13T13:03:00"))

    def test_invalid_duplicate_out_of_order_quotes_do_not_rewind(self):
        live = book()
        tick = {"timestamp": epoch("2026-10-13T13:00:10"), "bid": 13, "ask": 15}
        self.assertTrue(live.update(tick))
        self.assertFalse(live.update(tick))
        self.assertFalse(live.update({**tick, "timestamp": tick["timestamp"] - 1, "bid": 10}))
        self.assertFalse(live.update({**tick, "bid": float("nan")}))
        self.assertFalse(live.update({**tick, "ask": 1}))
        self.assertEqual(live.values()[0]["close"], 14)

    def test_new_york_daily_boundary_across_dst(self):
        live = book("1day", "2026-10-31T21:00:00")
        self.assertEqual(live.bucket(epoch("2026-11-01T21:30:00")), epoch("2026-10-31T21:00:00"))
        self.assertEqual(live.bucket(epoch("2026-11-01T22:00:00")), epoch("2026-11-01T22:00:00"))

    def test_week_uses_provider_saturday_anchor(self):
        live = book("1week", "2026-10-31T21:00:00")
        self.assertEqual(live.bucket(epoch("2026-11-07T21:59:00")), epoch("2026-10-31T21:00:00"))
        self.assertEqual(live.bucket(epoch("2026-11-07T22:00:00")), epoch("2026-11-07T22:00:00"))

    def test_month_is_calendar_month_at_previous_day_close(self):
        live = book("1month", "2026-09-30T21:00:00")
        self.assertEqual(live.bucket(epoch("2026-10-31T20:59:00")), epoch("2026-09-30T21:00:00"))
        self.assertEqual(live.bucket(epoch("2026-11-30T22:00:00")), epoch("2026-11-30T22:00:00"))

    def test_derived_intervals_match_existing_utc_aggregation(self):
        for interval, seconds in (("45min", 2700), ("2h", 7200), ("4h", 14400), ("8h", 28800)):
            live = book(interval, "2026-10-13T00:00:00")
            time = epoch("2026-10-13T19:38:14")
            self.assertEqual(live.bucket(time), int(time) // seconds * seconds)

    def test_authoritative_close_repairs_volume_without_rewinding_live_bar(self):
        feed = Feed("EUR/USD", "1min")
        feed.book = book()
        feed.meta = {"symbol": "EUR/USD", "interval": "1min"}
        feed.book.update({"timestamp": epoch("2026-10-13T13:01:01"), "bid": 13, "ask": 15})
        event = feed.apply_closed([row("2026-10-13T13:00:00"), row("2026-10-13T13:01:00", 1, 2)])
        self.assertEqual(event["candles"][0]["volume"], 100)
        self.assertEqual(feed.book.values()[-1]["close"], 14)


class LifecycleTest(unittest.TestCase):
    def test_production_and_unknown_env_never_enabled(self):
        for env in ("prod", "production", "staging", "", "unknown"):
            with patch.dict(os.environ, {"APP_ENV": env, "FXCM_REALTIME_ENABLED": "true"}):
                self.assertFalse(realtime_enabled())
        with patch.dict(os.environ, {"APP_ENV": "local", "FXCM_REALTIME_ENABLED": "false"}):
            self.assertFalse(realtime_enabled())
        with patch.dict(os.environ, {"APP_ENV": "local", "FXCM_REALTIME_ENABLED": "true"}):
            self.assertTrue(realtime_enabled())

    def test_reference_count_restores_original_subscription_once(self):
        subscriptions = OfferSubscriptions()
        subscriptions.set_status = Mock()
        fx = Mock()
        fx.session.session_status = fxcorepy.AO2GSessionStatus.O2GSessionStatus.CONNECTED
        offer = SimpleNamespace(instrument="EUR/USD", subscription_status="D", offer_id="1")
        key = subscriptions.acquire(fx, offer, 1)
        offer.subscription_status = "T"
        subscriptions.acquire(fx, offer, 1)
        self.assertEqual(subscriptions.set_status.call_count, 1)
        subscriptions.release(key)
        self.assertEqual(subscriptions.set_status.call_count, 1)
        offer.subscription_status = "T"
        with patch("app.services.realtime.fxcm_history_service._find_offer", return_value=offer):
            subscriptions.release(key)
        self.assertEqual(subscriptions.set_status.call_count, 2)
        self.assertEqual(subscriptions.set_status.call_args[0][-1], "D")
        self.assertFalse(subscriptions.entries)

    def test_reconnect_generation_cannot_unsubscribe_replacement_feed(self):
        subscriptions = OfferSubscriptions()
        subscriptions.set_status = Mock()
        old_fx, new_fx = Mock(), Mock()
        new_fx.session.session_status = fxcorepy.AO2GSessionStatus.O2GSessionStatus.CONNECTED
        offer = SimpleNamespace(instrument="EUR/USD", subscription_status="D", offer_id="1")
        old_key = subscriptions.acquire(old_fx, offer, 1)
        offer.subscription_status = "T"
        new_key = subscriptions.acquire(new_fx, offer, 2)
        subscriptions.release(old_key)
        self.assertEqual(subscriptions.set_status.call_count, 1)
        with patch("app.services.realtime.fxcm_history_service._find_offer", return_value=offer):
            subscriptions.release(new_key)
        subscriptions.set_status.assert_called_with(new_fx, "1", "D")

    def test_disconnect_callback_does_not_take_login_lock(self):
        service = FXCMHistoryService.__new__(FXCMHistoryService)
        service._connection_generation = 0
        service._connection_lock = Mock()
        service._on_session_status_changed(None, fxcorepy.AO2GSessionStatus.O2GSessionStatus.DISCONNECTED)
        self.assertTrue(service._needs_connection_reset)
        self.assertEqual(service._connection_generation, 1)
        self.assertEqual(service._connection_lock.mock_calls, [])

    def test_existing_connected_sdk_session_is_reused(self):
        service = FXCMHistoryService()
        service._fx = Mock()
        service._fx.session.session_status = fxcorepy.AO2GSessionStatus.O2GSessionStatus.CONNECTED
        service._login = Mock()
        self.assertIs(service._ensure_connected(), service._fx)
        service._login.assert_not_called()

    def test_queue_overflow_signals_resnapshot_and_retains_cleanup_membership(self):
        async def run():
            hub = RealtimeHub()
            queue = asyncio.Queue(maxsize=1)
            queue.put_nowait({"type": "candles"})
            channel = {"queues": {queue}}
            hub.publish(channel, {"type": "candles"})
            self.assertEqual((await queue.get())["type"], "error")
            self.assertIn(queue, channel["queues"])
        asyncio.run(run())

    def test_multiple_tabs_share_one_feed_and_last_close_cleans_it(self):
        async def run():
            hub = RealtimeHub(idle_seconds=0)
            async def worker(*args):
                await asyncio.sleep(3600)
            with patch.object(hub, "run", side_effect=worker) as worker_mock:
                key, first = await hub.subscribe("EUR/USD", "1min")
                _, second = await hub.subscribe("EURUSD", "1min")
                self.assertEqual(worker_mock.call_count, 1)
                await hub.unsubscribe(key, first)
                self.assertEqual(len(hub.channels), 1)
                await hub.unsubscribe(key, second)
                self.assertFalse(hub.channels)
        asyncio.run(run())

    def test_quick_snapshot_limits_provider_history_without_changing_full_recovery(self):
        service = FXCMHistoryService.__new__(FXCMHistoryService)
        service._ensure_connected = Mock(return_value=Mock())
        service._connection_generation = 1
        offer = SimpleNamespace(instrument="EUR/USD", time=datetime(2026, 10, 13, 13, 0, 1), bid=10, ask=12)
        service._find_offer = Mock(return_value=offer)
        service._get_history_or_raise = Mock(return_value=[])
        service._normalize_history_rows = Mock(return_value=[row("2026-10-13T13:00:00")])
        service._aggregate_rows = Mock(side_effect=lambda rows, interval: rows)
        with patch("app.services.realtime.fxcm_history_service", service), \
                patch("app.services.realtime.offer_subscriptions.acquire", return_value="EUR/USD"), \
                patch("app.services.realtime.Common.subscribe_table_updates"):
            for size in (256, 5000):
                feed = Feed("EUR/USD", "1min", size)
                snapshot = feed.open()
                self.assertEqual(service._get_history_or_raise.call_args[1]["quotes_count"], size)
                self.assertEqual(snapshot["type"], "snapshot")
                self.assertEqual(snapshot["quote"]["bid"], 10)

    def test_chart_seed_starts_realtime_without_provider_history(self):
        service = FXCMHistoryService.__new__(FXCMHistoryService)
        service._ensure_connected = Mock(return_value=Mock())
        service._connection_generation = 1
        offer = SimpleNamespace(instrument="EUR/USD", subscription_status="T", offer_id="1",
                                time=datetime(2026, 10, 19, 15, 0, 1), bid=10, ask=12)
        service._find_offer = Mock(return_value=offer)
        service._get_history_or_raise = Mock(side_effect=AssertionError("history should not be requested"))
        with patch("app.services.realtime.fxcm_history_service", service), \
                patch("app.services.realtime.offer_subscriptions.acquire", return_value="EUR/USD"), \
                patch("app.services.realtime.Common.subscribe_table_updates"):
            feed = Feed("EUR/USD", "1min", seed_timestamp=epoch("2026-10-19T14:59:00"), seed_price=11)
            snapshot = feed.open()
        self.assertEqual(snapshot["candles"][0]["close"], 11)
        self.assertEqual(snapshot["quote"]["bid"], 10)
        service._get_history_or_raise.assert_not_called()

    def test_recent_chart_reuses_feed_and_expired_idle_feed_is_cleaned(self):
        async def run():
            hub = RealtimeHub(idle_seconds=0.04)
            async def worker(channel, *args):
                channel["feed"] = Mock()
                channel["feed"].poll.return_value = None
                channel["feed"].snapshot.return_value = {"type": "snapshot", "candles": [1]}
                hub.publish(channel, channel["feed"].snapshot())
                await asyncio.sleep(3600)
            with patch.object(hub, "run", side_effect=worker) as calls:
                try:
                    key, first = await hub.subscribe("EUR/USD", "1min")
                    await asyncio.wait_for(first.get(), 1)
                    await hub.unsubscribe(key, first)
                    self.assertIn(key, hub.channels)
                    _, second = await hub.subscribe("EUR/USD", "1min")
                    self.assertEqual((await second.get())["type"], "snapshot")
                    self.assertEqual(calls.call_count, 1)
                    await asyncio.sleep(0.06)
                    self.assertIn(key, hub.channels)  # Old expiry cannot close an active client.
                    await hub.unsubscribe(key, second)
                    await asyncio.sleep(0.06)
                    self.assertNotIn(key, hub.channels)
                finally:
                    await hub.close()
        asyncio.run(run())

    def test_idle_limit_never_evicts_active_clients_and_shutdown_cancels_timers(self):
        async def run():
            hub = RealtimeHub(idle_seconds=30, max_idle=1)
            async def worker(*args):
                await asyncio.sleep(3600)
            with patch.object(hub, "run", side_effect=worker):
                active_key, active = await hub.subscribe("EUR/USD", "1min")
                old_key, old = await hub.subscribe("EUR/USD", "1h")
                new_key, new = await hub.subscribe("EUR/USD", "4h")
                await hub.unsubscribe(old_key, old)
                old_expiry = hub.channels[old_key]["expiry"]
                await hub.unsubscribe(new_key, new)
                self.assertNotIn(old_key, hub.channels)
                self.assertTrue(old_expiry.cancelled())
                self.assertIn(active_key, hub.channels)
                self.assertIn(new_key, hub.channels)
                expiry = hub.channels[new_key]["expiry"]
                await hub.close()
                self.assertTrue(expiry.cancelled())
                self.assertFalse(hub.channels)
                self.assertFalse(hub.retiring)
        asyncio.run(run())

    def test_stale_session_is_not_reused_and_deep_snapshot_has_separate_capacity(self):
        async def run():
            hub = RealtimeHub()
            async def worker(channel, *args):
                channel["feed"] = Mock()
                channel["feed"].poll.return_value = None
                channel["feed"].snapshot.return_value = {"type": "snapshot"}
                hub.publish(channel, {"type": "snapshot"})
                await asyncio.sleep(3600)
            with patch.object(hub, "run", side_effect=worker) as calls:
                try:
                    key, first = await hub.subscribe("EUR/USD", "1min")
                    await asyncio.wait_for(first.get(), 1)
                    hub.channels[key]["feed"].poll.side_effect = RuntimeError("disconnected")
                    _, second = await hub.subscribe("EUR/USD", "1min")
                    self.assertEqual((await first.get())["type"], "error")
                    self.assertEqual((await asyncio.wait_for(second.get(), 1))["type"], "snapshot")
                    self.assertEqual(calls.call_count, 2)
                    deep_key, deep = await hub.subscribe("EUR/USD", "1min", 5000)
                    self.assertNotEqual(key, deep_key)
                    await asyncio.wait_for(deep.get(), 1)
                    self.assertEqual(calls.call_count, 3)
                finally:
                    await hub.close()
        asyncio.run(run())

    def test_idle_bar_rollover_defers_history_calibration_until_a_viewer_returns(self):
        async def run():
            hub = RealtimeHub(idle_seconds=3)
            feed = Mock()
            feed.open.return_value = feed.snapshot.return_value = {"type": "snapshot"}
            events = [{"type": "candles", "candles": [1, 2]}]
            feed.poll.side_effect = lambda: events.pop() if events else None
            feed.apply_closed.return_value = None
            with patch("app.services.realtime.Feed", return_value=feed):
                try:
                    key, queue = await hub.subscribe("EUR/USD", "1min")
                    await asyncio.wait_for(queue.get(), 1)
                    await hub.unsubscribe(key, queue)
                    await asyncio.sleep(0.6)
                    feed.fetch_closed.assert_not_called()
                    self.assertTrue(hub.channels[key]["needs_repair"])
                    _, resumed = await hub.subscribe("EUR/USD", "1min")
                    self.assertEqual((await resumed.get())["type"], "snapshot")
                    await asyncio.sleep(0.6)
                    feed.fetch_closed.assert_called_once()
                finally:
                    await hub.close()
            feed.close.assert_called_once()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
