import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from time import monotonic
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import pandas as pd
from dateutil import parser
from forexconnect import ForexConnect

from app.core.config import get_settings


logger = logging.getLogger(__name__)


class FXCMServiceError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        payload: Optional[Any] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.payload = payload


@dataclass
class _TTLCacheEntry:
    expires_at: float
    value: Any


class _TTLCache:
    def __init__(self) -> None:
        self._entries: Dict[Tuple[Any, ...], _TTLCacheEntry] = {}
        self._lock = Lock()

    def get(self, key: Tuple[Any, ...]) -> Optional[Any]:
        now = monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._entries.pop(key, None)
                return None
            return deepcopy(entry.value)

    def set(self, key: Tuple[Any, ...], value: Any, *, ttl_seconds: int) -> None:
        with self._lock:
            self._entries[key] = _TTLCacheEntry(
                expires_at=monotonic() + ttl_seconds,
                value=deepcopy(value),
            )


@dataclass(frozen=True)
class IntervalSpec:
    provider_interval: str
    bucket_seconds: Optional[int]


@dataclass(frozen=True)
class OfferMetadata:
    asset_type: Optional[str]
    market: Optional[str]
    exchange: Optional[str]
    country: Optional[str]
    exchange_timezone: Optional[str]


class FXCMHistoryService:
    SUPPORTED_MARKETS = {"stocks", "etf", "mutual_funds", "forex", "crypto"}
    LOGIN_MAX_ATTEMPTS = 2
    BATCH_QUOTES_CACHE_TTL_SECONDS = 8
    MARKET_SYMBOLS_CACHE_TTL_SECONDS = 20
    INTERVAL_SPECS = {
        "1min": IntervalSpec(provider_interval="m1", bucket_seconds=60),
        "5min": IntervalSpec(provider_interval="m5", bucket_seconds=300),
        "15min": IntervalSpec(provider_interval="m15", bucket_seconds=900),
        "30min": IntervalSpec(provider_interval="m30", bucket_seconds=1800),
        "45min": IntervalSpec(provider_interval="m15", bucket_seconds=2700),
        "1h": IntervalSpec(provider_interval="H1", bucket_seconds=3600),
        "2h": IntervalSpec(provider_interval="H1", bucket_seconds=7200),
        "4h": IntervalSpec(provider_interval="H1", bucket_seconds=14400),
        "8h": IntervalSpec(provider_interval="H1", bucket_seconds=28800),
        "1day": IntervalSpec(provider_interval="D1", bucket_seconds=86400),
        "1week": IntervalSpec(provider_interval="W1", bucket_seconds=604800),
        "1month": IntervalSpec(provider_interval="M1", bucket_seconds=None),
    }
    INSTRUMENT_TYPE_METADATA = {
        1: OfferMetadata(
            asset_type="Physical Currency",
            market="Forex",
            exchange="FXCM",
            country=None,
            exchange_timezone="UTC",
        ),
        2: OfferMetadata(
            asset_type="Index",
            market="Indices",
            exchange="CFD",
            country=None,
            exchange_timezone="UTC",
        ),
        5: OfferMetadata(
            asset_type="Precious Metal",
            market="Precious Metals",
            exchange="CFD",
            country=None,
            exchange_timezone="UTC",
        ),
        7: OfferMetadata(
            asset_type="Index",
            market="Indices",
            exchange="CFD",
            country=None,
            exchange_timezone="UTC",
        ),
        8: OfferMetadata(
            asset_type="Common Stock",
            market="Stocks",
            exchange=None,
            country=None,
            exchange_timezone="UTC",
        ),
        9: OfferMetadata(
            asset_type="Digital Currency",
            market="Crypto",
            exchange="FXCM",
            country=None,
            exchange_timezone="UTC",
        ),
    }
    EXCHANGE_SUFFIXES = {
        "US": ("US", "United States"),
        "CA": ("CA", "Canada"),
        "DE": ("DE", "Germany"),
        "FR": ("FR", "France"),
        "UK": ("UK", "United Kingdom"),
        "HK": ("HK", "Hong Kong"),
        "AU": ("AU", "Australia"),
        "CH": ("CH", "Switzerland"),
        "EXT": (None, None),
    }

    def __init__(self) -> None:
        self.settings = get_settings()
        self._response_cache = _TTLCache()
        self._fx = ForexConnect()
        self._connection_lock = Lock()

    def _ensure_connected(self) -> ForexConnect:
        with self._connection_lock:
            try:
                # 若底层引擎已断开，会返回 False
                connected = self._fx.is_connected()
            except Exception:
                connected = False

            if not connected:
                logger.info(
                    "FXCM session is disconnected or not initialized. Initiating login..."
                )
                self._login(self._fx)
            return self._fx

    def fetch_history(
        self,
        *,
        symbol: str,
        interval: str = "1h",
        outputsize: int = 120,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        price_type: str = "mid",
    ) -> Dict[str, Any]:
        requested_interval = self._resolve_interval_name(interval)
        normalized_price_type = self._normalize_price_type(price_type)
        interval_spec = self._get_interval_spec(requested_interval)
        parsed_start = self._parse_request_datetime(start_date)
        parsed_end = self._parse_request_datetime(end_date)
        raw_quotes_count = self._resolve_quotes_count(requested_interval, outputsize)

        raw_history, offer = self._request_history(
            symbol=symbol,
            provider_interval=interval_spec.provider_interval,
            start_date=parsed_start,
            end_date=parsed_end,
            quotes_count=raw_quotes_count,
        )
        rows = self._normalize_history_rows(raw_history)
        rows = self._aggregate_rows(rows, requested_interval)
        rows = rows[-outputsize:]
        values = [self._select_prices(row, normalized_price_type) for row in rows]
        offer_meta = self._build_offer_metadata(offer)

        return {
            "meta": {
                "symbol": symbol,
                "provider_symbol": getattr(offer, "instrument", None) or symbol,
                "requested_interval": requested_interval,
                "provider_interval": interval_spec.provider_interval,
                "price_type": normalized_price_type,
                "count": len(values),
                "currency": getattr(offer, "contract_currency", None),
                "exchange": offer_meta.exchange,
                "exchange_timezone": offer_meta.exchange_timezone,
                "asset_type": offer_meta.asset_type,
                "subscription_status": getattr(offer, "subscription_status", None),
                "start_date": start_date,
                "end_date": end_date,
            },
            "values": values,
        }

    def search_instruments(
        self,
        *,
        keyword: str,
        outputsize: int = 10,
    ) -> Dict[str, Any]:
        normalized_keyword = self._normalize_lookup(keyword)
        if not normalized_keyword:
            raise FXCMServiceError(
                status_code=400,
                message="keyword is required",
                payload={"field": "keyword"},
            )

        fx = self._ensure_connected()
        offers = list(self._iter_offers(fx))

        exact_matches: List[Dict[str, Any]] = []
        fuzzy_matches: List[Dict[str, Any]] = []

        for offer in offers:
            item = self._build_search_item(offer)
            haystacks = [
                self._normalize_lookup(item.get("symbol")),
                self._normalize_lookup(item.get("provider_symbol")),
                self._normalize_lookup(item.get("name")),
                self._normalize_lookup(item.get("label")),
            ]
            if any(
                normalized_keyword == haystack for haystack in haystacks if haystack
            ):
                exact_matches.append(item)
                continue
            if any(
                normalized_keyword in haystack or haystack in normalized_keyword
                for haystack in haystacks
                if haystack
            ):
                fuzzy_matches.append(item)

        deduped = self._dedupe_items(
            self._sort_search_items(exact_matches)
            + self._sort_search_items(fuzzy_matches),
            limit=outputsize,
        )
        return {
            "keyword": keyword,
            "count": len(deduped),
            "items": deduped,
        }

    def fetch_quote(
        self,
        *,
        symbol: str,
        interval: str = "1day",
        price_type: str = "mid",
    ) -> Dict[str, Any]:
        requested_interval = self._resolve_interval_name(interval)
        normalized_price_type = self._normalize_price_type(price_type)

        fx = self._ensure_connected()
        return self._build_quote_payload(
            fx,
            symbol=symbol,
            requested_interval=requested_interval,
            price_type=normalized_price_type,
        )

    def fetch_quotes_batch(
        self,
        *,
        symbols: Sequence[str],
        interval: str = "1day",
        price_type: str = "mid",
    ) -> Dict[str, Any]:
        requested_symbols = [item.strip() for item in symbols if item and item.strip()]
        if not requested_symbols:
            raise FXCMServiceError(
                status_code=400,
                message="At least one symbol is required",
                payload={"field": "symbols"},
            )

        requested_interval = self._resolve_interval_name(interval)
        normalized_price_type = self._normalize_price_type(price_type)
        cache_key = (
            "quotes_batch",
            tuple(requested_symbols),
            requested_interval,
            normalized_price_type,
        )
        cached = self._response_cache.get(cache_key)
        if cached is not None:
            return cached

        fx = self._ensure_connected()
        offers_by_lookup = self._index_offers_by_lookup(self._iter_offers(fx))
        items: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        for requested_symbol in requested_symbols:
            try:
                quote = self._build_quote_payload(
                    fx,
                    symbol=requested_symbol,
                    requested_interval=requested_interval,
                    price_type=normalized_price_type,
                    offer=offers_by_lookup.get(
                        self._normalize_lookup(requested_symbol)
                    ),
                )
            except FXCMServiceError as exc:
                errors.append(
                    {
                        "requested_symbol": requested_symbol,
                        "code": exc.status_code,
                        "message": exc.message,
                        "data": exc.payload,
                    }
                )
                continue

            items.append({"requested_symbol": requested_symbol, **quote})

        response = {
            "requested_symbols": requested_symbols,
            "count": len(requested_symbols),
            "succeeded": len(items),
            "failed": len(errors),
            "items": items,
            "errors": errors,
        }
        self._response_cache.set(
            cache_key,
            response,
            ttl_seconds=self.BATCH_QUOTES_CACHE_TTL_SECONDS,
        )
        return response

    def list_instruments_by_market(
        self,
        *,
        market: str,
        outputsize: int = 50,
        country: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_market = market.strip().casefold()
        if normalized_market not in self.SUPPORTED_MARKETS:
            raise FXCMServiceError(
                status_code=400,
                message="Unsupported market",
                payload={
                    "market": market,
                    "supported": sorted(self.SUPPORTED_MARKETS),
                },
            )

        cache_key = (
            "market_symbols",
            normalized_market,
            outputsize,
            self._normalize_lookup(country),
        )
        cached = self._response_cache.get(cache_key)
        if cached is not None:
            return cached

        if normalized_market in {"etf", "mutual_funds"}:
            response = {"market": normalized_market, "count": 0, "items": []}
            self._response_cache.set(
                cache_key,
                response,
                ttl_seconds=self.MARKET_SYMBOLS_CACHE_TTL_SECONDS,
            )
            return response

        fx = self._ensure_connected()
        items = [
            self._build_search_item(offer)
            for offer in self._iter_offers(fx)
            if self._matches_market_key(offer, normalized_market)
        ]

        if country is not None:
            items = [
                item for item in items if self._matches_country_filter(item, country)
            ]

        items = self._sort_search_items(items)[:outputsize]
        response = {
            "market": normalized_market,
            "count": len(items),
            "items": items,
        }
        self._response_cache.set(
            cache_key,
            response,
            ttl_seconds=self.MARKET_SYMBOLS_CACHE_TTL_SECONDS,
        )
        return response

    def _request_history(
        self,
        *,
        symbol: str,
        provider_interval: str,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
        quotes_count: int,
    ) -> Tuple[Any, Any]:
        fx = self._ensure_connected()
        offer = self._find_offer(fx, symbol)
        provider_symbol = getattr(offer, "instrument", None) or symbol
        history = self._get_history_or_raise(
            fx,
            instrument=provider_symbol,
            timeframe=provider_interval,
            start_date=start_date,
            end_date=end_date,
            quotes_count=quotes_count,
        )
        return history, offer

    def _build_quote_payload(
        self,
        fx: ForexConnect,
        *,
        symbol: str,
        requested_interval: str,
        price_type: str,
        offer: Any = None,
    ) -> Dict[str, Any]:
        interval_spec = self._get_interval_spec(requested_interval)
        raw_quotes_count = max(2, self._resolve_quotes_count(requested_interval, 2))
        resolved_offer = offer or self._find_offer(fx, symbol)
        if resolved_offer is None:
            raise FXCMServiceError(
                status_code=404,
                message="FXCM instrument not found",
                payload={"symbol": symbol},
            )

        provider_symbol = getattr(resolved_offer, "instrument", None) or symbol
        raw_history = self._get_history_or_raise(
            fx,
            instrument=provider_symbol,
            timeframe=interval_spec.provider_interval,
            start_date=None,
            end_date=None,
            quotes_count=raw_quotes_count,
        )

        rows = self._normalize_history_rows(raw_history)
        rows = self._aggregate_rows(rows, requested_interval)
        price_rows = [self._select_prices(row, price_type) for row in rows]
        latest_row = price_rows[-1] if price_rows else {}
        previous_row = price_rows[-2] if len(price_rows) >= 2 else {}

        current_price = self._resolve_price(
            price_type,
            self._to_float(getattr(resolved_offer, "bid", None)),
            self._to_float(getattr(resolved_offer, "ask", None)),
        )
        if current_price is None:
            current_price = self._to_float(latest_row.get("close"))

        current_timestamp = self._to_offer_timestamp(
            getattr(resolved_offer, "time", None)
        )
        if current_timestamp is None:
            current_timestamp = self._to_int(latest_row.get("timestamp"))

        quote_datetime = (
            self._format_datetime(current_timestamp)
            if current_timestamp is not None
            else latest_row.get("datetime")
        )
        previous_close = self._to_float(previous_row.get("close"))
        change_value = None
        percent_change = None
        if current_price is not None and previous_close not in (None, 0):
            change_value = current_price - previous_close
            percent_change = (change_value / previous_close) * 100.0

        latest_open = self._to_float(latest_row.get("open"))
        latest_high = self._to_float(latest_row.get("high"))
        latest_low = self._to_float(latest_row.get("low"))
        if current_price is not None:
            if latest_open is None:
                latest_open = current_price
            if latest_high is None or current_price > latest_high:
                latest_high = current_price
            if latest_low is None or current_price < latest_low:
                latest_low = current_price

        offer_meta = self._build_offer_metadata(resolved_offer)

        return {
            "symbol": provider_symbol,
            "provider_symbol": provider_symbol,
            "name": provider_symbol,
            "exchange": offer_meta.exchange,
            "mic_code": None,
            "currency": getattr(resolved_offer, "contract_currency", None),
            "datetime": quote_datetime,
            "timestamp": current_timestamp,
            "last_quote_at": current_timestamp,
            "open": latest_open,
            "high": latest_high,
            "low": latest_low,
            "close": current_price,
            "change": change_value,
            "percent_change": percent_change,
            "previous_close": previous_close,
            "volume": self._to_int(latest_row.get("volume")),
            "average_volume": None,
            "is_market_open": self._is_market_open(resolved_offer),
            "fifty_two_week": {
                "low": None,
                "high": None,
                "range": None,
            },
        }

    def _get_history_or_raise(
        self,
        fx: ForexConnect,
        *,
        instrument: str,
        timeframe: str,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
        quotes_count: int,
    ) -> Any:
        try:
            return fx.get_history(
                instrument,
                timeframe,
                start_date,
                end_date,
                quotes_count,
            )
        except Exception as exc:
            error_text = str(exc)
            if "QuotesServerConnectionError" in error_text:
                raise FXCMServiceError(
                    status_code=502,
                    message="FXCM historical quote server is currently unavailable",
                    payload={
                        "detail": (
                            "FXCM 登录已成功，但历史行情服务器当前不可达。"
                            "如持续失败，请检查网络或稍后重试。"
                        )
                    },
                ) from exc

            raise FXCMServiceError(
                status_code=502,
                message="FXCM history request failed",
                payload={"detail": error_text},
            ) from exc

    def _normalize_history_rows(self, history: Any) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for raw_row in history:
            timestamp = self._to_timestamp(raw_row["Date"])
            if timestamp is None:
                continue

            item = {
                "timestamp": timestamp,
                "datetime": self._format_datetime(timestamp),
                "bid_open": self._to_float(raw_row["BidOpen"]),
                "bid_high": self._to_float(raw_row["BidHigh"]),
                "bid_low": self._to_float(raw_row["BidLow"]),
                "bid_close": self._to_float(raw_row["BidClose"]),
                "ask_open": self._to_float(raw_row["AskOpen"]),
                "ask_high": self._to_float(raw_row["AskHigh"]),
                "ask_low": self._to_float(raw_row["AskLow"]),
                "ask_close": self._to_float(raw_row["AskClose"]),
                "volume": self._to_int(raw_row["Volume"]),
            }
            rows.append(item)

        rows.sort(key=lambda item: item["timestamp"])
        return rows

    def _iter_offers(self, fx: ForexConnect) -> Sequence[Any]:
        try:
            offers = fx.get_table(ForexConnect.OFFERS)
            return [offer for offer in offers]
        except Exception as exc:
            raise FXCMServiceError(
                status_code=502,
                message="FXCM offers request failed",
                payload={"detail": str(exc)},
            ) from exc

    def _find_offer(self, fx: ForexConnect, symbol: str) -> Any:
        normalized_symbol = self._normalize_lookup(symbol)
        for offer in self._iter_offers(fx):
            instrument = getattr(offer, "instrument", None)
            if self._normalize_lookup(instrument) == normalized_symbol:
                return offer
        return None

    def _index_offers_by_lookup(
        self,
        offers: Sequence[Any],
    ) -> Dict[str, Any]:
        indexed: Dict[str, Any] = {}
        for offer in offers:
            key = self._normalize_lookup(getattr(offer, "instrument", None))
            if key and key not in indexed:
                indexed[key] = offer
        return indexed

    def _aggregate_rows(
        self,
        rows: Sequence[Mapping[str, Any]],
        requested_interval: str,
    ) -> List[Dict[str, Any]]:
        if requested_interval not in {"45min", "2h", "4h", "8h"}:
            return [dict(row) for row in rows if isinstance(row, Mapping)]

        interval_spec = self._get_interval_spec(requested_interval)
        bucket_seconds = interval_spec.bucket_seconds
        if bucket_seconds is None:
            return [dict(row) for row in rows if isinstance(row, Mapping)]

        grouped: Dict[int, List[Mapping[str, Any]]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            timestamp = self._to_int(row.get("timestamp"))
            if timestamp is None:
                continue
            bucket_start = timestamp - (timestamp % bucket_seconds)
            grouped.setdefault(bucket_start, []).append(row)

        aggregated: List[Dict[str, Any]] = []
        for bucket_start in sorted(grouped):
            bucket_rows = sorted(
                grouped[bucket_start],
                key=lambda item: self._to_int(item.get("timestamp")) or bucket_start,
            )
            first_row = bucket_rows[0]
            last_row = bucket_rows[-1]

            aggregated.append(
                {
                    "timestamp": bucket_start,
                    "datetime": self._format_datetime(bucket_start),
                    "bid_open": self._to_float(first_row.get("bid_open")),
                    "bid_high": self._max_value(bucket_rows, "bid_high"),
                    "bid_low": self._min_value(bucket_rows, "bid_low"),
                    "bid_close": self._to_float(last_row.get("bid_close")),
                    "ask_open": self._to_float(first_row.get("ask_open")),
                    "ask_high": self._max_value(bucket_rows, "ask_high"),
                    "ask_low": self._min_value(bucket_rows, "ask_low"),
                    "ask_close": self._to_float(last_row.get("ask_close")),
                    "volume": self._sum_value(bucket_rows, "volume"),
                }
            )

        return aggregated

    def _select_prices(
        self,
        row: Mapping[str, Any],
        price_type: str,
    ) -> Dict[str, Any]:
        bid_open = self._to_float(row.get("bid_open"))
        bid_high = self._to_float(row.get("bid_high"))
        bid_low = self._to_float(row.get("bid_low"))
        bid_close = self._to_float(row.get("bid_close"))
        ask_open = self._to_float(row.get("ask_open"))
        ask_high = self._to_float(row.get("ask_high"))
        ask_low = self._to_float(row.get("ask_low"))
        ask_close = self._to_float(row.get("ask_close"))

        return {
            "datetime": row.get("datetime"),
            "timestamp": self._to_int(row.get("timestamp")) or 0,
            "open": self._resolve_price(price_type, bid_open, ask_open),
            "high": self._resolve_price(price_type, bid_high, ask_high),
            "low": self._resolve_price(price_type, bid_low, ask_low),
            "close": self._resolve_price(price_type, bid_close, ask_close),
            "volume": self._to_int(row.get("volume")),
            "bid_open": bid_open,
            "bid_high": bid_high,
            "bid_low": bid_low,
            "bid_close": bid_close,
            "ask_open": ask_open,
            "ask_high": ask_high,
            "ask_low": ask_low,
            "ask_close": ask_close,
        }

    def _build_search_item(self, offer: Any) -> Dict[str, Any]:
        instrument = getattr(offer, "instrument", None) or ""
        instrument_type = getattr(offer, "instrument_type", None)
        offer_meta = self._build_offer_metadata(offer)
        exchange, country = self._resolve_exchange_and_country(
            instrument, instrument_type
        )

        return {
            "symbol": instrument,
            "provider_symbol": instrument,
            "name": instrument,
            "label": instrument,
            "exchange": exchange or offer_meta.exchange,
            "mic_code": None,
            "timezone": offer_meta.exchange_timezone,
            "market": offer_meta.market,
            "asset_type": offer_meta.asset_type,
            "country": country or offer_meta.country,
            "currency": getattr(offer, "contract_currency", None),
            "provider_plan": getattr(offer, "subscription_status", None),
        }

    def _build_offer_metadata(self, offer: Any) -> OfferMetadata:
        instrument_type = getattr(offer, "instrument_type", None)
        base_meta = self.INSTRUMENT_TYPE_METADATA.get(
            instrument_type,
            OfferMetadata(
                asset_type=None,
                market=None,
                exchange=None,
                country=None,
                exchange_timezone="UTC",
            ),
        )
        exchange, country = self._resolve_exchange_and_country(
            getattr(offer, "instrument", None),
            instrument_type,
        )
        return OfferMetadata(
            asset_type=base_meta.asset_type,
            market=base_meta.market,
            exchange=exchange or base_meta.exchange,
            country=country or base_meta.country,
            exchange_timezone=base_meta.exchange_timezone,
        )

    def _resolve_exchange_and_country(
        self,
        instrument: Optional[str],
        instrument_type: Optional[int],
    ) -> Tuple[Optional[str], Optional[str]]:
        if not instrument or "." not in instrument:
            return None, None

        suffix = instrument.rsplit(".", 1)[-1].upper()
        exchange_code, country = self.EXCHANGE_SUFFIXES.get(suffix, (suffix, None))
        if instrument_type == 8 and exchange_code is None:
            exchange_code = "CFD"
        return exchange_code, country

    def _resolve_quotes_count(self, interval: str, outputsize: int) -> int:
        interval_spec = self._get_interval_spec(interval)
        bucket_seconds = interval_spec.bucket_seconds
        provider_interval_spec = self._get_interval_spec_by_provider(
            interval_spec.provider_interval
        )
        provider_bucket_seconds = provider_interval_spec.bucket_seconds

        if (
            bucket_seconds is None
            or provider_bucket_seconds is None
            or provider_bucket_seconds <= 0
        ):
            return outputsize

        multiplier = max(1, int(bucket_seconds / provider_bucket_seconds))
        if multiplier <= 1:
            return outputsize
        return min(5000, max(outputsize * multiplier + multiplier * 2, outputsize))

    def _parse_request_datetime(self, value: Optional[str]) -> Optional[datetime]:
        if value is None:
            return None

        try:
            parsed = parser.parse(value)
        except (TypeError, ValueError) as exc:
            raise FXCMServiceError(
                status_code=400,
                message="Invalid datetime parameter",
                payload={"value": value},
            ) from exc

        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    def _resolve_interval_name(self, interval: str) -> str:
        normalized_interval = interval.strip().lower()
        aliases = {
            "1m": "1min",
            "5m": "5min",
            "15m": "15min",
            "30m": "30min",
            "45m": "45min",
            "60m": "1h",
            "h1": "1h",
            "d1": "1day",
            "w1": "1week",
            "mo1": "1month",
        }
        return aliases.get(normalized_interval, normalized_interval)

    def _get_interval_spec(self, interval: str) -> IntervalSpec:
        interval_spec = self.INTERVAL_SPECS.get(interval)
        if interval_spec is None:
            raise FXCMServiceError(
                status_code=400,
                message="Unsupported interval",
                payload={
                    "interval": interval,
                    "supported": sorted(self.INTERVAL_SPECS.keys()),
                },
            )
        return interval_spec

    def _get_interval_spec_by_provider(self, provider_interval: str) -> IntervalSpec:
        for interval_spec in self.INTERVAL_SPECS.values():
            if interval_spec.provider_interval == provider_interval:
                return interval_spec
        return IntervalSpec(provider_interval=provider_interval, bucket_seconds=None)

    def _normalize_price_type(self, price_type: str) -> str:
        normalized_price_type = price_type.strip().lower()
        if normalized_price_type not in {"bid", "ask", "mid"}:
            raise FXCMServiceError(
                status_code=400,
                message="Unsupported price_type",
                payload={"price_type": price_type},
            )
        return normalized_price_type

    def _resolve_price(
        self,
        price_type: str,
        bid_value: Optional[float],
        ask_value: Optional[float],
    ) -> Optional[float]:
        if price_type == "bid":
            return bid_value if bid_value is not None else ask_value
        if price_type == "ask":
            return ask_value if ask_value is not None else bid_value
        if bid_value is not None and ask_value is not None:
            return (bid_value + ask_value) / 2.0
        return bid_value if bid_value is not None else ask_value

    def _dedupe_items(
        self,
        items: Sequence[Mapping[str, Any]],
        *,
        limit: int,
    ) -> List[Dict[str, Any]]:
        deduped: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for item in items:
            symbol = self._normalize_lookup(item.get("symbol"))
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            deduped.append(dict(item))
            if len(deduped) >= limit:
                break
        return deduped

    def _sort_search_items(
        self,
        items: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        status_priority = {"T": 0, "V": 1, "D": 2}
        return sorted(
            (dict(item) for item in items if isinstance(item, Mapping)),
            key=lambda item: (
                status_priority.get(item.get("provider_plan"), 9),
                self._normalize_lookup(item.get("symbol")),
            ),
        )

    def _matches_market_key(self, offer: Any, market: str) -> bool:
        offer_meta = self._build_offer_metadata(offer)
        market_value = self._normalize_lookup(offer_meta.market)
        asset_type_value = self._normalize_lookup(offer_meta.asset_type)

        if market == "forex":
            return market_value == self._normalize_lookup("Forex")
        if market == "crypto":
            return market_value == self._normalize_lookup("Crypto")
        if market == "stocks":
            return asset_type_value == self._normalize_lookup("Common Stock")
        if market == "etf":
            return asset_type_value == self._normalize_lookup("ETF")
        if market == "mutual_funds":
            return asset_type_value == self._normalize_lookup("Mutual Fund")
        return False

    def _matches_country_filter(
        self,
        item: Mapping[str, Any],
        country: str,
    ) -> bool:
        normalized_country = self._normalize_lookup(country)
        if not normalized_country:
            return True

        candidates = [
            item.get("country"),
            item.get("exchange"),
            item.get("provider_symbol"),
        ]
        return any(
            normalized_country in self._normalize_lookup(candidate)
            or self._normalize_lookup(candidate) in normalized_country
            for candidate in candidates
            if candidate not in (None, "")
        )

    def _login(self, fx: ForexConnect) -> None:
        last_exception = None

        for attempt in range(1, self.LOGIN_MAX_ATTEMPTS + 1):
            try:
                fx.login(
                    self.settings.username,
                    self.settings.password,
                    self.settings.url,
                    self.settings.connection,
                    self.settings.session_id,
                    self.settings.pin,
                    self._on_session_status_changed,
                )
                return
            except Exception as exc:
                last_exception = exc
                error_text = str(exc)
                if "Wait timeout exceeded" in error_text:
                    logger.warning(
                        "FXCM login attempt timed out",
                        extra={
                            "attempt": attempt,
                            "max_attempts": self.LOGIN_MAX_ATTEMPTS,
                        },
                    )
                    if attempt < self.LOGIN_MAX_ATTEMPTS:
                        continue
                    raise FXCMServiceError(
                        status_code=504,
                        message="FXCM login timed out",
                        payload={
                            "detail": error_text,
                            "attempts": self.LOGIN_MAX_ATTEMPTS,
                        },
                    ) from exc

                raise FXCMServiceError(
                    status_code=502,
                    message="FXCM login failed",
                    payload={"detail": error_text},
                ) from exc

        if last_exception is not None:
            raise FXCMServiceError(
                status_code=504,
                message="FXCM login timed out",
                payload={
                    "detail": str(last_exception),
                    "attempts": self.LOGIN_MAX_ATTEMPTS,
                },
            ) from last_exception

    def _logout(self, fx: ForexConnect) -> None:
        try:
            fx.logout()
        except Exception:
            logger.warning("FXCM logout failed", exc_info=True)

    def _max_value(
        self,
        rows: Sequence[Mapping[str, Any]],
        field: str,
    ) -> Optional[float]:
        values = [
            self._to_float(item.get(field))
            for item in rows
            if self._to_float(item.get(field)) is not None
        ]
        return max(values) if values else None

    def _min_value(
        self,
        rows: Sequence[Mapping[str, Any]],
        field: str,
    ) -> Optional[float]:
        values = [
            self._to_float(item.get(field))
            for item in rows
            if self._to_float(item.get(field)) is not None
        ]
        return min(values) if values else None

    def _sum_value(
        self,
        rows: Sequence[Mapping[str, Any]],
        field: str,
    ) -> Optional[int]:
        values = [
            self._to_int(item.get(field))
            for item in rows
            if self._to_int(item.get(field)) is not None
        ]
        return sum(values) if values else None

    def _format_datetime(self, timestamp: int) -> str:
        # Windows cannot convert pre-epoch timestamps via fromtimestamp().
        epoch_utc = datetime(1970, 1, 1, tzinfo=timezone.utc)
        return (epoch_utc + timedelta(seconds=timestamp)).isoformat()

    def _to_offer_timestamp(self, value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            ts = pd.Timestamp(value)
        except Exception:
            return None

        if ts.tz is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return int(ts.timestamp())

    def _is_market_open(self, offer: Any) -> bool:
        return getattr(offer, "trading_status", None) == "O"

    def _normalize_lookup(self, value: Any) -> str:
        if value in (None, ""):
            return ""
        return "".join(ch for ch in str(value).upper() if ch.isalnum())

    def _to_timestamp(self, value: Any) -> Optional[int]:
        try:
            ts = pd.Timestamp(value)
        except Exception:
            return None

        if pd.isna(ts):
            return None

        if ts.tz is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return int(ts.timestamp())

    def _to_float(self, value: Any) -> Optional[float]:
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    def _to_int(self, value: Any) -> Optional[int]:
        try:
            return None if value is None else int(value)
        except (TypeError, ValueError):
            return None

    def _on_session_status_changed(self, session: Any, status: Any) -> None:
        logger.info("FXCM session status: %s", status)
        # 0 = Disconnected
        if status == 0:
            with self._connection_lock:
                try:
                    logger.info(
                        "FXCM session disconnected. Re-initializing ForexConnect..."
                    )
                    self._fx = ForexConnect()
                except Exception as exc:
                    logger.warning(
                        f"Failed to cleanly recreate ForexConnect instance: {exc}"
                    )


fxcm_history_service = FXCMHistoryService()
