import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from auth.oauth_config import (
    get_static_endpoint_token,
    is_static_endpoint_token_enabled,
)
from auth.service_decorator import _extract_oauth20_user_email


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("WORKSPACE_MCP_STATIC_TOKEN", raising=False)
    monkeypatch.delenv("USER_GOOGLE_EMAIL", raising=False)


def test_static_endpoint_token_absent_by_default():
    assert get_static_endpoint_token() is None
    assert is_static_endpoint_token_enabled() is False


def test_static_endpoint_token_reads_env(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_STATIC_TOKEN", "shared-secret")

    assert get_static_endpoint_token() == "shared-secret"
    assert is_static_endpoint_token_enabled() is True


def test_static_endpoint_token_strips_surrounding_whitespace(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_STATIC_TOKEN", "  shared-secret  ")

    assert get_static_endpoint_token() == "shared-secret"


def test_whitespace_only_token_is_not_treated_as_configured(monkeypatch):
    """A blank value must not be mistaken for a configured secret."""
    monkeypatch.setenv("WORKSPACE_MCP_STATIC_TOKEN", "   ")

    assert get_static_endpoint_token() is None
    assert is_static_endpoint_token_enabled() is False


def _sig():
    import inspect

    def tool(user_google_email: str = None, query: str = None):
        pass

    return inspect.signature(tool)


def test_caller_supplied_email_is_ignored_under_a_static_token(monkeypatch):
    """The shared secret is one principal, so it must not select another account."""
    monkeypatch.setenv("WORKSPACE_MCP_STATIC_TOKEN", "shared-secret")
    monkeypatch.setenv("USER_GOOGLE_EMAIL", "owner@example.com")

    kwargs = {"user_google_email": "victim@example.com", "query": "from:ceo"}
    resolved = _extract_oauth20_user_email((), kwargs, _sig())

    assert resolved == "owner@example.com"
    assert kwargs["user_google_email"] == "owner@example.com"


def test_omitted_email_resolves_to_the_pinned_account(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_STATIC_TOKEN", "shared-secret")
    monkeypatch.setenv("USER_GOOGLE_EMAIL", "owner@example.com")

    kwargs = {}
    assert _extract_oauth20_user_email((), kwargs, _sig()) == "owner@example.com"


def test_static_token_without_configured_email_is_rejected(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_STATIC_TOKEN", "shared-secret")

    with pytest.raises(Exception, match="USER_GOOGLE_EMAIL"):
        _extract_oauth20_user_email((), {"user_google_email": "a@example.com"}, _sig())


def test_without_a_static_token_the_caller_supplied_email_is_honoured(monkeypatch):
    """Unchanged behaviour for the existing loopback single-user path."""
    monkeypatch.setenv("USER_GOOGLE_EMAIL", "owner@example.com")

    kwargs = {"user_google_email": "other@example.com"}
    assert _extract_oauth20_user_email((), kwargs, _sig()) == "other@example.com"
