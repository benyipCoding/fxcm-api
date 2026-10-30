# FXCM Sidecar API

This directory now contains a minimal FastAPI sidecar that wraps the working ForexConnect integration from `test.py` and exposes it as HTTP endpoints.

## Python Version

Use the project virtual environment created from Python 3.7:

```powershell
.\.venv\Scripts\python.exe --version
```

## Install Dependencies

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run The Service

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8100 --reload
```

## Available Endpoints

- `GET /health`
- `GET /history?symbol=EUR/USD&interval=1h&outputsize=20`

## Environment Variables

The service reuses the existing `.env` file and expects:

- `USERNAME`
- `PASSWORD`
- `FXCM_URL`
- `FXCM_CONNECTION` (optional, defaults to `Demo`)
- `FXCM_SESSION_ID` (optional)
- `FXCM_PIN` (optional)

## Notes

- `price_type` supports `bid`, `ask`, and `mid`.
- `45min`, `2h`, `3h`, `4h`, `6h`, and `8h` are aggregated from smaller FXCM base intervals.
- Historical requests reuse the ForexConnect session. SDK history requests are serialized through its shared history communicator.

## Local realtime mode

Set `APP_ENV=local` and `FXCM_REALTIME_ENABLED=true` in this service's `.env` and in `shl-solver-backend/.env`. Both default to disabled for realtime; production and staging cannot enable it. Run this sidecar bound to `127.0.0.1:8100` with one worker. Start the frontend with `npm run dev`.

`GET /realtime/stream?symbol=EUR/USD&interval=1min` streams SSE snapshots, candle updates and heartbeats. It uses native ForexConnect Offers callbacks. Multiple clients for the same symbol/interval share a feed; subscriptions are released on disconnect. Missing quote subscriptions are enabled using `SET_SUBSCRIPTION_STATUS` and their original status is restored when the last consumer exits (best effort if the broker connection has failed).

See [the architecture, configuration and verification guide](../shl-solver-backend/docs/fxcm-realtime.md) for the full data flow, supported frontend behavior and limitations.

Tests: `.\.venv\Scripts\python.exe -m unittest discover -s tests -v` (no broker login).

## Local Demo trading

ForexConnect Demo execution is available at `/trading/state`, `/trading/stream`, `/trading/history`, `/trading/commands` and `/trading/commands/{client_id}`. `/trading/history` defaults to `scope=all`: it downloads the official Combined Account Statement (CAS) XML through `session.get_report_url` and returns all archived closed trades for the selected account, across symbols. `scope=recent` refreshes `CLOSED_TRADES` for the live terminal only; that table is not full account history. Both scopes support optional UTC `from_ts` / `to_ts` Unix-second filters (`from` inclusive, `to` exclusive). CAS dates are converted from New York time (including DST), summary rows are excluded, and broker net P/L is preserved. A failed statement returns an error instead of substituting recent trades. In addition to the realtime flags, enable `FXCM_TRADING_ENABLED=true` and configure a random `FXCM_TRADING_BRIDGE_KEY` shared with the backend. Requests require the `X-FXCM-Bridge-Key` header. Real connections and production environments are rejected. Use one worker bound to loopback.

Commands are recorded in the Git-ignored `.local/trading.sqlite3` before dispatch. Reusing a command ID never resends an order, including after process restart; uncertain results must be reconciled, not retried as new orders. Account snapshots come from SDK tables. See [the Demo trading guide](../shl-solver-backend/docs/fxcm-demo-trading.md) for execution, fees, permissions, setup and validation.

Full account statements run in a separate, read-only Python process with its own ForexConnect login and no quote subscriptions. In ForexConnect 1.6.43, calling `get_report_url` on a session with Python quote callbacks can stall for about 35 seconds and fail with `Token not received`; moving that call to another thread does not isolate those callbacks. Overlapping requests for the same account/range share a worker. Each worker has a 70-second deadline and is terminated on timeout, below the gateway's 90-second timeout. Errors distinguish authorization, download, parsing and worker failures without exposing signed report URLs. No recent-trade or stale-history fallback is used.
