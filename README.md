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
- `45min`, `2h`, `4h`, and `8h` are aggregated from smaller FXCM base intervals.
- The current implementation logs in per request for simplicity and correctness. Once the 3.11 backend is wired in, you can optimize connection reuse if needed.
