from __future__ import annotations

from collections.abc import Iterable
from typing import Any, get_type_hints

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from ragos.core.errors import RagOSError, ValidationError

_SOURCES = frozenset({"body", "query", "path", "header", "cookie"})
_MAX_FIELD_DEPTH = 3
_ERROR_CODES = {
    "missing": "REQUIRED",
    "string_too_long": "TOO_LONG",
    "string_too_short": "TOO_SHORT",
    "greater_than_equal": "OUT_OF_RANGE",
    "less_than_equal": "OUT_OF_RANGE",
    "json_invalid": "MALFORMED_JSON",
}


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _model_field_names(model: type[BaseModel]) -> Iterable[str]:
    for name, info in model.model_fields.items():
        yield name
        for candidate in (info.alias, info.validation_alias):
            if isinstance(candidate, str):
                yield candidate


def _declared_paths(route: APIRoute) -> frozenset[tuple[str, ...]]:
    """Locations the route declares; nothing else may appear in an error `field`."""
    dependant = route.dependant
    paths: set[tuple[str, ...]] = set()
    for source, params in (
        ("query", dependant.query_params),
        ("path", dependant.path_params),
        ("header", dependant.header_params),
        ("cookie", dependant.cookie_params),
    ):
        paths.update((source, param.alias) for param in params)

    body_params = dependant.body_params
    if not body_params:
        return frozenset(paths)
    body_field = route.body_field
    embedded = len(body_params) > 1 or bool(
        body_field is not None and getattr(body_field.field_info, "embed", False)
    )
    try:
        hints = get_type_hints(route.endpoint, include_extras=True)
    except Exception:
        hints = {}
    for param in body_params:
        model = hints.get(param.name)
        if embedded:
            paths.add(("body", param.alias))
            if _is_model(model):
                paths.update(("body", param.alias, name) for name in _model_field_names(model))
        elif _is_model(model):
            paths.update(("body", name) for name in _model_field_names(model))
    return frozenset(paths)


def _field_path(loc: Any, declared: frozenset[tuple[str, ...]]) -> str:
    """Return the longest declared prefix of `loc`; dynamic or unknown segments are dropped."""
    if not isinstance(loc, (tuple, list)) or not loc:
        return "request"
    source = loc[0]
    if not isinstance(source, str) or source not in _SOURCES:
        return "request"
    parts: tuple[str, ...] = (source,)
    for segment in loc[1:_MAX_FIELD_DEPTH]:
        if not isinstance(segment, str):
            break
        candidate = (*parts, segment)
        if candidate not in declared:
            break
        parts = candidate
    return ".".join(parts)


def _error_code(error_type: str) -> str:
    if error_type in _ERROR_CODES:
        return _ERROR_CODES[error_type]
    if error_type.endswith(("_type", "_parsing")):
        return "INVALID_TYPE"
    return "INVALID_VALUE"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RagOSError)
    async def ragos_error_handler(request: Request, exc: RagOSError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "")
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_response_content(),
            headers={"X-Request-Id": request_id},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "")
        route = request.scope.get("route")
        declared = _declared_paths(route) if isinstance(route, APIRoute) else frozenset()
        errors = [
            {
                "field": _field_path(error.get("loc"), declared),
                "code": _error_code(str(error.get("type", ""))),
            }
            for error in exc.errors()
        ]
        error = ValidationError("Request validation failed", errors=errors)
        return JSONResponse(
            status_code=error.status_code,
            content=error.to_response_content(),
            headers={"X-Request-Id": request_id},
        )
