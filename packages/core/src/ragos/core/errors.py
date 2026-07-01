from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RagOSError(Exception):
    code: str
    message: str
    status_code: int = 500
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_response_content(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class AuthenticationError(RagOSError):
    def __init__(self, message: str = "Authentication required", **details: Any) -> None:
        super().__init__(
            code="AUTHENTICATION_REQUIRED",
            message=message,
            status_code=401,
            details=details,
        )


class PermissionDeniedError(RagOSError):
    def __init__(self, message: str = "Permission denied", **details: Any) -> None:
        super().__init__(
            code="PERMISSION_DENIED",
            message=message,
            status_code=403,
            details=details,
        )


class ValidationError(RagOSError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(
            code="VALIDATION_ERROR",
            message=message,
            status_code=422,
            details=details,
        )


class NotFoundError(RagOSError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(
            code="NOT_FOUND",
            message=message,
            status_code=404,
            details=details,
        )


class ConflictError(RagOSError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(
            code="CONFLICT",
            message=message,
            status_code=409,
            details=details,
        )


class QuotaExceededError(RagOSError):
    def __init__(self, message: str = "Quota exceeded", **details: Any) -> None:
        super().__init__(
            code="QUOTA_EXCEEDED",
            message=message,
            status_code=429,
            details=details,
        )


class ProviderError(RagOSError):
    def __init__(self, message: str = "Upstream provider failure", **details: Any) -> None:
        super().__init__(
            code="PROVIDER_ERROR",
            message=message,
            status_code=502,
            details=details,
        )


class SandboxError(RagOSError):
    def __init__(self, message: str = "Sandbox execution failure", **details: Any) -> None:
        super().__init__(
            code="SANDBOX_ERROR",
            message=message,
            status_code=500,
            details=details,
        )


class TimeoutError(RagOSError):
    def __init__(self, message: str = "Operation timed out", **details: Any) -> None:
        super().__init__(
            code="TIMEOUT",
            message=message,
            status_code=504,
            details=details,
        )
