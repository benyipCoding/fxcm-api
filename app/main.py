from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.routers.history import router as history_router
from app.services.fxcm_service import FXCMServiceError


app = FastAPI(
    title="FXCM Sidecar API",
    version="0.1.0",
    description="A minimal FastAPI sidecar for fetching FXCM historical candles.",
)


@app.exception_handler(FXCMServiceError)
async def handle_fxcm_service_error(
    request: Request,
    exc: FXCMServiceError,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.status_code,
            "message": exc.message,
            "data": exc.payload,
        },
    )


app.include_router(history_router)
