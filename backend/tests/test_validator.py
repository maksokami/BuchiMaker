"""
Unit tests: core/validator.py

TDD approach – each test was written to describe the contract first.
Tests cover: sanitize_name, validate_identifier, SafeName Pydantic type,
and InputValidationMiddleware.
"""

import pytest
from pydantic import BaseModel, ValidationError

from app.core.validator import (
    InputValidationMiddleware,
    SafeIdentifier,
    SafeName,
    raise_if_invalid_name,
    sanitize_name,
    validate_identifier,
    validate_path_within_base,
)


# ---------------------------------------------------------------------------
# sanitize_name
# ---------------------------------------------------------------------------

class TestSanitizeName:
    """Tests for sanitize_name()."""

    def test_valid_simple_name(self):
        """Alphanumeric + underscores should pass."""
        assert sanitize_name("my_dashboard") == "my_dashboard"

    def test_valid_name_with_spaces_and_dashes(self):
        """Spaces and dashes are permitted by the Constitution."""
        assert sanitize_name("My Dashboard-2024") == "My Dashboard-2024"

    def test_empty_string_raises(self):
        """Empty string must raise ValueError."""
        with pytest.raises(ValueError, match="must not be empty"):
            sanitize_name("")

    def test_whitespace_only_raises(self):
        """Whitespace-only string should raise ValueError."""
        with pytest.raises(ValueError, match="must not be empty"):
            sanitize_name("   ")

    def test_sql_injection_attempt_raises(self):
        """SQL injection characters are not allowed."""
        with pytest.raises(ValueError, match="invalid characters"):
            sanitize_name("name'; DROP TABLE --")

    def test_semicolon_raises(self):
        """Semicolons are not allowed."""
        with pytest.raises(ValueError, match="invalid characters"):
            sanitize_name("bad;name")

    def test_slash_raises(self):
        """Slashes are not allowed."""
        with pytest.raises(ValueError, match="invalid characters"):
            sanitize_name("path/to/thing")

    def test_custom_field_label_in_error(self):
        """Error message should include the custom field label."""
        with pytest.raises(ValueError, match="widget name"):
            sanitize_name("bad!", field="widget name")


# ---------------------------------------------------------------------------
# validate_identifier
# ---------------------------------------------------------------------------

class TestValidateIdentifier:
    """Tests for validate_identifier()."""

    def test_valid_identifier(self):
        """Alphanumeric + underscore/dash only."""
        assert validate_identifier("sales_2024") == "sales_2024"

    def test_spaces_not_allowed(self):
        """Spaces are forbidden in identifiers."""
        with pytest.raises(ValueError):
            validate_identifier("my dashboard")

    def test_empty_identifier_raises(self):
        """Empty string is rejected."""
        with pytest.raises(ValueError):
            validate_identifier("")


# ---------------------------------------------------------------------------
# validate_path_within_base
# ---------------------------------------------------------------------------

class TestValidatePathWithinBase:
    """Tests for validate_path_within_base()."""

    def test_accepts_direct_child(self, tmp_path):
        target = tmp_path / "orders.csv"
        assert validate_path_within_base(str(target), str(tmp_path)) == str(target)

    def test_accepts_nested_child(self, tmp_path):
        target = tmp_path / "nested" / "orders.csv"
        assert validate_path_within_base(str(target), str(tmp_path)) == str(target)

    def test_accepts_glob_pattern_inside_base(self, tmp_path):
        target = str(tmp_path / "agg" / "**" / "*.parquet")
        assert validate_path_within_base(target, str(tmp_path)) == target

    def test_rejects_sibling_path(self, tmp_path):
        sibling = tmp_path.parent / "not_the_data_dir" / "orders.csv"
        with pytest.raises(ValueError, match="data directory"):
            validate_path_within_base(str(sibling), str(tmp_path))

    def test_rejects_directory_traversal(self, tmp_path):
        traversal = str(tmp_path / ".." / "orders.csv")
        with pytest.raises(ValueError, match="data directory"):
            validate_path_within_base(traversal, str(tmp_path))

    def test_rejects_absolute_path_elsewhere(self, tmp_path):
        with pytest.raises(ValueError, match="data directory"):
            validate_path_within_base("/etc/passwd", str(tmp_path))

    def test_custom_field_label_in_error(self, tmp_path):
        with pytest.raises(ValueError, match="^custom_field "):
            validate_path_within_base("/etc/passwd", str(tmp_path), field="custom_field")


# ---------------------------------------------------------------------------
# Pydantic SafeName annotated type
# ---------------------------------------------------------------------------

class _NamedModel(BaseModel):
    name: SafeName


class TestSafeNamePydantic:
    """Tests for the SafeName annotated Pydantic type."""

    def test_valid_name_parsed(self):
        """Valid names are accepted by Pydantic."""
        m = _NamedModel(name="My Widget")
        assert m.name == "My Widget"

    def test_invalid_name_raises_validation_error(self):
        """Invalid names raise Pydantic ValidationError."""
        with pytest.raises(ValidationError):
            _NamedModel(name="bad!")


# ---------------------------------------------------------------------------
# raise_if_invalid_name (HTTP-aware wrapper)
# ---------------------------------------------------------------------------

class TestRaiseIfInvalidName:
    """Tests for raise_if_invalid_name()."""

    def test_valid_name_does_not_raise(self):
        """No exception for a valid name."""
        raise_if_invalid_name("valid-name")

    def test_invalid_name_raises_http_422(self):
        """HTTPException 422 is raised for invalid names."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            raise_if_invalid_name("bad!")
        assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# InputValidationMiddleware
# ---------------------------------------------------------------------------

class TestInputValidationMiddleware:
    """Integration tests for InputValidationMiddleware via TestClient."""

    def test_oversized_request_returns_413(self):
        """Requests with Content-Length > limit get 413."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        small_app = FastAPI()
        small_app.add_middleware(InputValidationMiddleware, max_body_bytes=10)

        @small_app.post("/upload")
        async def upload():
            return {"ok": True}

        client = TestClient(small_app, raise_server_exceptions=False)
        # Send a request with a large Content-Length header
        response = client.post(
            "/upload",
            content=b"x" * 20,
            headers={"Content-Length": "20"},
        )
        assert response.status_code == 413

    def test_small_request_passes_through(self):
        """Requests within the size limit are forwarded to the handler."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        small_app = FastAPI()
        small_app.add_middleware(InputValidationMiddleware, max_body_bytes=1024)

        @small_app.post("/upload")
        async def upload():
            return {"ok": True}

        client = TestClient(small_app)
        response = client.post("/upload", content=b"hello")
        assert response.status_code == 200
