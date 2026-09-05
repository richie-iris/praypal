"""conftest.py — Pytest fixtures and test environment setup."""
from __future__ import annotations

import os
import pytest

# Ensure tests run with predictable environment variables
os.environ["RT_ENV"] = "test"
os.environ["LOG_LEVEL"] = "DEBUG"
os.environ["SUPABASE_PROJECT_REF"] = "testprojectref"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-service-role-key"
os.environ["SUPABASE_ACCESS_TOKEN"] = "test-access-token"  # noqa: S105 - dummy value for the stubbed test lane


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure tests don't leak mutations to environment variables."""
    yield


@pytest.fixture(autouse=True)
def _unshadow_http_client():
    """monkeypatch.setattr(rt_http.http_client, "request", ...) restores the
    ORIGINAL as an instance attribute, which then shadows any later class-level
    patch of PooledHttpClient.request (order-dependent failures). Drop the
    shadow so every test sees the class method again."""
    yield
    try:
        import rt_http
        vars(rt_http.http_client).pop("request", None)
    except Exception:  # noqa: S110 - hygiene only; never fail a test from teardown
        pass
