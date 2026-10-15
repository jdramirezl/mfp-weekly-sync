"""Machine-readable error codes and the exception hierarchy.

Error codes are stable strings so agent-driven workflows can branch on them.
They are surfaced in `--json` output under the top-level `error_code` field and
map 1:1 to a process exit code (see cli.py / EXIT_CODES).
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """Stable, machine-readable error identifiers."""

    OK = "OK"
    INVALID_PLAN = "INVALID_PLAN"
    PLAN_NOT_FOUND = "PLAN_NOT_FOUND"
    UNRESOLVED_FOODS = "UNRESOLVED_FOODS"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_FAILED = "AUTH_FAILED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    WRITE_UNCERTAIN = "WRITE_UNCERTAIN"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    CONFIG_ERROR = "CONFIG_ERROR"
    USER_ABORTED = "USER_ABORTED"
    UNKNOWN = "UNKNOWN"


# Map error codes to process exit codes. 0 == success. Keep these stable.
EXIT_CODES: dict[ErrorCode, int] = {
    ErrorCode.OK: 0,
    ErrorCode.INVALID_PLAN: 2,
    ErrorCode.PLAN_NOT_FOUND: 3,
    ErrorCode.UNRESOLVED_FOODS: 4,
    ErrorCode.AUTH_REQUIRED: 5,
    ErrorCode.AUTH_FAILED: 6,
    ErrorCode.SESSION_EXPIRED: 7,
    ErrorCode.BACKEND_UNAVAILABLE: 8,
    ErrorCode.WRITE_UNCERTAIN: 9,
    ErrorCode.CONFLICT: 10,
    ErrorCode.RATE_LIMITED: 11,
    ErrorCode.CONFIG_ERROR: 12,
    ErrorCode.USER_ABORTED: 13,
    ErrorCode.UNKNOWN: 1,
}


class MfpSyncError(Exception):
    """Base application error carrying a machine-readable code."""

    code: ErrorCode = ErrorCode.UNKNOWN

    def __init__(self, message: str, code: ErrorCode | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    def exit_code(self) -> int:
        return EXIT_CODES.get(self.code, 1)


class InvalidPlanError(MfpSyncError):
    code = ErrorCode.INVALID_PLAN


class PlanNotFoundError(MfpSyncError):
    code = ErrorCode.PLAN_NOT_FOUND


class UnresolvedFoodsError(MfpSyncError):
    code = ErrorCode.UNRESOLVED_FOODS


class AuthRequiredError(MfpSyncError):
    code = ErrorCode.AUTH_REQUIRED


class AuthFailedError(MfpSyncError):
    code = ErrorCode.AUTH_FAILED


class SessionExpiredError(MfpSyncError):
    code = ErrorCode.SESSION_EXPIRED


class BackendUnavailableError(MfpSyncError):
    code = ErrorCode.BACKEND_UNAVAILABLE


class WriteUncertainError(MfpSyncError):
    """Raised when a write was attempted but its outcome could not be confirmed."""

    code = ErrorCode.WRITE_UNCERTAIN


class ConflictError(MfpSyncError):
    code = ErrorCode.CONFLICT


class ConfigError(MfpSyncError):
    code = ErrorCode.CONFIG_ERROR
