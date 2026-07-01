from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from ragos.core.errors import RagOSError


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RagOSError)
    async def ragos_error_handler(request: Request, exc: RagOSError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "")
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_response_content(),
            headers={"X-Request-Id": request_id},
        )
