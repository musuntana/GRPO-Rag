from __future__ import annotations

import pytest
from ragos.core.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ProviderError,
    QuotaExceededError,
    RagOSError,
    SandboxError,
    TimeoutError,
    ValidationError,
)


@pytest.mark.parametrize(
    ("exc_cls", "expected_status", "expected_code"),
    [
        (AuthenticationError, 401, "AUTHENTICATION_REQUIRED"),
        (PermissionDeniedError, 403, "PERMISSION_DENIED"),
        (NotFoundError, 404, "NOT_FOUND"),
        (ConflictError, 409, "CONFLICT"),
        (ValidationError, 422, "VALIDATION_ERROR"),
        (QuotaExceededError, 429, "QUOTA_EXCEEDED"),
        (SandboxError, 500, "SANDBOX_ERROR"),
        (ProviderError, 502, "PROVIDER_ERROR"),
        (TimeoutError, 504, "TIMEOUT"),
    ],
)
def test_error_subclasses_map_to_http_status(
    exc_cls: type[RagOSError],
    expected_status: int,
    expected_code: str,
) -> None:
    exc = exc_cls("message for test") if exc_cls is not AuthenticationError else exc_cls()
    assert exc.status_code == expected_status
    assert exc.code == expected_code


def test_error_response_shape_includes_details() -> None:
    exc = NotFoundError("Tenant not found", resource="Tenant", id="abc")
    payload = exc.to_response_content()
    assert payload == {
        "error": {
            "code": "NOT_FOUND",
            "message": "Tenant not found",
            "details": {"resource": "Tenant", "id": "abc"},
        }
    }
