"""
Input sanitisation and validation utilities.

Constitution requirement (Security):
  "All front-end inputs must be back-end validated. Sanitize all
   application-level objects that users can define like widgets, dashboard
   names, calculated field names, etc.
   Only alphanumeric characters, spaces, dashes, and underscores."

This module provides:
  - A reusable ``sanitize_name`` function.
  - Pydantic validators / annotated types for use in request models.
  - An ``InputValidationMiddleware`` that rejects requests with oversized
    payloads before they reach route handlers.
"""

import re
from pathlib import Path
from typing import Annotated

from fastapi import HTTPException, status
from pydantic import AfterValidator
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Allowed pattern: alphanumeric, space, dash, underscore (Constitution §Security)
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9 _\-]+$")

# Maximum accepted request body size (bytes) – configurable but safe default
MAX_BODY_BYTES: int = 2 * 1024 * 1024  # 2 MB


def sanitize_name(value: str, field: str = "name") -> str:
    """Validate that *value* contains only safe characters.

    Args:
        value: The string to validate.
        field: Human-readable field label for error messages.

    Returns:
        The original value if it passes validation.

    Raises:
        ValueError: When the value contains disallowed characters or is empty.
    """
    if not value or not value.strip():
        raise ValueError(f"{field} must not be empty.")
    if not _SAFE_NAME_RE.match(value):
        raise ValueError(
            f"{field} '{value}' contains invalid characters. "
            "Only alphanumeric characters, spaces, dashes (-), and "
            "underscores (_) are allowed."
        )
    return value


def validate_safe_name(value: str) -> str:
    """Pydantic *AfterValidator* wrapper for ``sanitize_name``.

    Args:
        value: Field value coming from the request body.

    Returns:
        Validated string.

    Raises:
        ValueError: Forwarded from ``sanitize_name``.
    """
    return sanitize_name(value)


# Annotated type – drop-in replacement for ``str`` in Pydantic request models
SafeName = Annotated[str, AfterValidator(validate_safe_name)]


def validate_identifier(value: str) -> str:
    """Validate a strict SQL-safe identifier (no spaces).

    Suitable for dashboard IDs, widget IDs, table names, etc.

    Args:
        value: Candidate identifier string.

    Returns:
        Validated identifier.

    Raises:
        ValueError: If the value does not match ``[a-zA-Z0-9_-]+``.
    """
    pattern = re.compile(r"^[a-zA-Z0-9_\-]+$")
    if not value or not pattern.match(value):
        raise ValueError(
            f"Identifier '{value}' is invalid. "
            "Only alphanumeric characters, dashes, and underscores are allowed."
        )
    return value


SafeIdentifier = Annotated[str, AfterValidator(validate_identifier)]


def validate_path_within_base(filepath: str, base_dir: str, field: str = "filepath") -> str:
    """Ensure ``filepath`` resolves to a location inside ``base_dir``.

    Data-source connectors (CSV/JSON/Parquet) let a caller point at any
    file the backend process can read. Without this check, a caller could
    register a data source at an absolute path outside the intended data
    directory (``../`` traversal, ``/etc/passwd``, another mounted secret,
    etc.) and have its contents exposed as a queryable DuckDB table.

    Both ``filepath`` and ``base_dir`` are resolved with ``Path.resolve()``,
    which normalises ``.``/``..`` segments *and* follows symlinks for any
    path component that exists on disk — so a symlink planted inside
    ``base_dir`` that points outside it is also rejected. Glob metacharacters
    (e.g. Parquet's ``**/*.parquet``) are left untouched by ``resolve()``
    since they don't exist as real path components, so glob patterns are
    supported as long as their literal, non-wildcard prefix stays inside
    ``base_dir``.

    Args:
        filepath: Candidate file path or glob pattern.
        base_dir: Directory the path must resolve inside.
        field: Field label used in the error message.

    Returns:
        The original, unmodified ``filepath`` once validated.

    Raises:
        ValueError: If ``filepath`` resolves outside ``base_dir``.
    """
    resolved_base = Path(base_dir).resolve()
    resolved_target = Path(filepath).resolve()
    if not resolved_target.is_relative_to(resolved_base):
        raise ValueError(
            f"{field} '{filepath}' must be an absolute path inside the "
            f"configured data directory ({resolved_base})."
        )
    return filepath


class InputValidationMiddleware(BaseHTTPMiddleware):
    """Reject requests that exceed the configured payload size limit.

    This is a lightweight guard that runs *before* body parsing.  It does
    not attempt to decode JSON – it only checks the ``Content-Length``
    header.  Requests without ``Content-Length`` that are not GET/HEAD/DELETE
    are passed through (the framework body parser will enforce its own limit).

    Args:
        app: The ASGI application to wrap.
        max_body_bytes: Maximum allowed ``Content-Length`` in bytes.
    """

    def __init__(self, app, max_body_bytes: int = MAX_BODY_BYTES):
        super().__init__(app)
        self.max_body_bytes = max_body_bytes

    async def dispatch(self, request: Request, call_next) -> Response:
        """Check request size and delegate to the next handler.

        Args:
            request: Incoming HTTP request.
            call_next: ASGI callable for the next middleware / route handler.

        Returns:
            HTTP response, or a 413 JSON error if the payload is too large.
        """
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > self.max_body_bytes:
            return JSONResponse(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                content={
                    "detail": (
                        f"Request body exceeds the maximum allowed size of "
                        f"{self.max_body_bytes // (1024 * 1024)} MB."
                    )
                },
            )
        return await call_next(request)


def raise_if_invalid_name(value: str, field: str = "name") -> None:
    """Convenience wrapper that raises an HTTP 422 on invalid names.

    Intended for use inside route handlers where a human-readable HTTP
    error is preferable to a raw ValueError.

    Args:
        value: The string to validate.
        field: Human-readable field label for the error message.

    Raises:
        HTTPException: 422 Unprocessable Content when validation fails.
    """
    try:
        sanitize_name(value, field)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
