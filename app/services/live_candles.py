"""Provisional candles; provider history remains the authoritative stored data."""
import math
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytz


class LiveCandles:
    def __init__(self, history_service, interval, rows):
        if not rows:
            raise ValueError("No FXCM history available to seed live candles")
        self.service = history_service
        self.interval = interval
        self.rows = deepcopy(rows)
        self.last_tick_time = float("-inf")
        self.quote = None
        seed = datetime.fromtimestamp(rows[-1]["timestamp"], timezone.utc)
        # FXCM native D/W/M bars close at 17:00 New York (21/22 UTC).
        # Preserve other provider anchors rather than forcing this convention.
        self.calendar_zone = pytz.timezone("America/New_York") if seed.hour in (21, 22) else pytz.UTC
        self.anchor = seed.astimezone(self.calendar_zone).replace(tzinfo=None)

    def bucket(self, timestamp):
        # Anchor native periods to the provider's bar, including its week start.
        # Derived intervals use exactly the UTC buckets of the history service.
        if self.interval in {"1day", "1week", "1month"}:
            dt = datetime.fromtimestamp(timestamp, self.calendar_zone).replace(tzinfo=None)
            if self.interval == "1month":
                # FXCM month starts at the preceding day's trading close.
                offset = timedelta(hours=self.anchor.hour, minutes=self.anchor.minute)
                if self.anchor.day > 15:
                    offset -= timedelta(days=1)
                label = dt - offset
                start = label.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + offset
            else:
                days = 7 if self.interval == "1week" else 1
                start = self.anchor + ((dt - self.anchor) // timedelta(days=days)) * timedelta(days=days)
            return int(self.calendar_zone.localize(start).timestamp())
        seconds = self.service.INTERVAL_SPECS[self.interval].bucket_seconds
        anchor = self.rows[-1]["timestamp"]
        return anchor + int((timestamp - anchor) // seconds) * seconds

    def update(self, tick):
        timestamp, bid, ask = tick["timestamp"], tick["bid"], tick["ask"]
        if not all(math.isfinite(v) for v in (timestamp, bid, ask)):
            return False
        if bid <= 0 or ask < bid or timestamp < self.last_tick_time:
            return False
        if self.quote == tick:
            return False
        if (self.quote and timestamp == self.last_tick_time and "sequence" in tick
                and tick["sequence"] <= self.quote.get("sequence", -1)):
            return False
        start = self.bucket(timestamp)
        if start < self.rows[-1]["timestamp"]:
            return False
        self.last_tick_time = timestamp
        self.quote = dict(tick)
        last = self.rows[-1]
        if start > last["timestamp"]:
            # Match ForexConnect.get_history's default PREVIOUS_CLOSE mode.
            last = {
                "timestamp": start,
                "datetime": self.service._format_datetime(start),
                "volume": None,
                **{side + "_" + field: self.rows[-1][side + "_close"]
                   for side in ("bid", "ask") for field in ("open", "high", "low", "close")},
            }
            self.rows.append(last)
            self.rows = self.rows[-self.service.MAX_OUTPUT_BARS:]
        for side, price in (("bid", bid), ("ask", ask)):
            last[side + "_high"] = max(last[side + "_high"], price)
            last[side + "_low"] = min(last[side + "_low"], price)
            last[side + "_close"] = price
        # Offers.Volume is not exchange trade volume. Leave the forming bar's
        # volume unknown rather than inventing volume or double-counting the seed.
        last["volume"] = None
        return True

    def values(self, count=None):
        rows = self.rows if count is None else self.rows[-count:]
        return [self.service._select_prices(row, "mid") for row in rows]
