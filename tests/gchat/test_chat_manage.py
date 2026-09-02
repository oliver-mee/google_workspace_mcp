"""
Unit tests for the chat_read / chat_manage dispatcher tools.
"""

import sys
import os
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.server import server
import gchat.chat_tools  # noqa: F401  (registers chat_manage via @server.tool)


def _unwrap(tool):
    """Unwrap a FunctionTool + decorator chain to the original async function."""
    fn = getattr(tool, "fn", tool)
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _space(name="spaces/SPACE1", display="Test Space", stype="SPACE", threaded=True):
    return {
        "name": name,
        "displayName": display,
        "spaceType": stype,
        "threaded": threaded,
    }


@pytest.mark.asyncio
async def test_chat_manage_schema_exposes_action_enum():
    """The write dispatcher schema must enumerate only its write actions."""
    tools = await server.list_tools(run_middleware=False)
    tool = next(tool for tool in tools if tool.name == "chat_manage")

    actions = tool.parameters["properties"]["action"]
    assert set(actions["enum"]) == {
        "create_space",
        "dm_space",
        "create_reaction",
        "delete_reaction",
    }


@pytest.mark.asyncio
async def test_chat_read_schema_exposes_action_enum():
    """The read dispatcher schema must enumerate only its read actions."""
    tools = await server.list_tools(run_middleware=False)
    tool = next(tool for tool in tools if tool.name == "chat_read")

    actions = tool.parameters["properties"]["action"]
    assert set(actions["enum"]) == {
        "find_space",
        "list_spaces",
        "list_reactions",
        "list_threads",
    }


@pytest.mark.asyncio
async def test_chat_read_and_manage_have_distinct_scopes():
    """The scope gate must be per-tool: read loads under readonly, write under full."""

    from gchat.chat_tools import chat_manage, chat_read

    async def required(fn):
        return getattr(fn, "_required_google_scopes", None)

    read_scopes = await required(chat_read)
    write_scopes = await required(chat_manage)
    assert read_scopes and write_scopes
    assert set(read_scopes) & set(write_scopes) == set()
    assert any("readonly" in s for s in read_scopes)
    assert any("readonly" not in s for s in write_scopes)


# ---------------------------------------------------------------------------
# Helper-level tests (logic without the auth decorators)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_chat_spaces():
    from gchat.chat_helpers import list_chat_spaces

    service = Mock()
    service.spaces().list().execute.return_value = {
        "spaces": [
            _space(),
            _space(name="spaces/2", display="Other", stype="DIRECT_MESSAGE"),
        ]
    }

    result = await list_chat_spaces(service, page_size=100, space_type="all")
    assert "2 Chat spaces" in result
    assert "Test Space" in result


@pytest.mark.asyncio
async def test_find_chat_space():
    from gchat.chat_helpers import find_chat_space

    service = Mock()
    service.spaces().list().execute.return_value = {
        "spaces": [
            _space(display="Marketing"),
            _space(name="spaces/2", display="Engineering"),
        ]
    }

    result = await find_chat_space(service, query="mar", exact=False)
    assert "Marketing" in result

    result = await find_chat_space(service, query="marketing", exact=True)
    assert "Marketing" in result

    result = await find_chat_space(service, query="nope", exact=False)
    assert "No space matching" in result


@pytest.mark.asyncio
async def test_create_chat_space():
    from gchat.chat_helpers import create_chat_space

    service = Mock()
    service.spaces().create().execute.return_value = _space(
        name="spaces/NEW", display="New Room", stype="SPACE"
    )

    result = await create_chat_space(
        service, display_name="New Room", space_type="SPACE"
    )
    assert "Space created" in result
    assert "spaces/NEW" in result

    # display_name None should not send a displayName
    service.spaces().create.reset_mock()
    service.spaces().create().execute.return_value = _space(
        name="spaces/NEW2", stype="GROUP_CHAT"
    )
    await create_chat_space(service, display_name=None, space_type="GROUP_CHAT")
    sent_body = service.spaces().create.call_args.kwargs["body"]
    assert sent_body == {"spaceType": "GROUP_CHAT"}


@pytest.mark.asyncio
async def test_find_or_create_dm_space_existing():
    from gchat.chat_helpers import find_or_create_dm_space

    service = Mock()
    service.spaces().list().execute.return_value = {
        "spaces": [_space(name="spaces/DM1", display="", stype="DIRECT_MESSAGE")]
    }
    service.spaces().members().list().execute.return_value = {
        "memberships": [{"member": {"name": "users/42"}}]
    }

    result = await find_or_create_dm_space(service, user_id="users/42")
    assert "Found existing DM" in result
    service.spaces().create.assert_not_called()


@pytest.mark.asyncio
async def test_find_or_create_dm_space_new():
    from gchat.chat_helpers import find_or_create_dm_space

    service = Mock()
    service.spaces().list().execute.return_value = {"spaces": []}
    service.spaces().create().execute.return_value = _space(
        name="spaces/DMNEW", stype="DIRECT_MESSAGE"
    )
    service.spaces().members().create().execute.return_value = {}

    result = await find_or_create_dm_space(service, user_id="users/99")
    assert "Created new DM" in result
    # bare id gets users/ prefix
    sent = service.spaces().members().create.call_args.kwargs["body"]
    assert sent["member"]["name"] == "users/99"


@pytest.mark.asyncio
async def test_reactions_roundtrip():
    from gchat.chat_helpers import (
        create_chat_reaction,
        delete_chat_reaction,
        list_chat_reactions,
    )

    service = Mock()
    service.spaces().messages().reactions().create().execute.return_value = {
        "name": "spaces/S/messages/M/reactions/R"
    }
    result = await create_chat_reaction(
        service, message_id="spaces/S/messages/M", emoji_unicode="👍"
    )
    assert "Reacted with" in result

    service.spaces().messages().reactions().delete().execute.return_value = {}
    result = await delete_chat_reaction(
        service, reaction_id="spaces/S/messages/M/reactions/R"
    )
    assert "Deleted" in result

    service.spaces().messages().reactions().list().execute.return_value = {
        "reactions": [
            {"name": "spaces/S/messages/M/reactions/R1", "emoji": {"unicode": "👍"}},
            {
                "name": "spaces/S/messages/M/reactions/R2",
                "emoji": {"customEmoji": {"uid": "party"}},
            },
        ]
    }
    result = await list_chat_reactions(service, message_id="spaces/S/messages/M")
    assert "👍" in result
    assert "party" in result


@pytest.mark.asyncio
async def test_list_chat_threads_groups_by_thread():
    from gchat.chat_helpers import list_chat_threads

    service = Mock()
    service.spaces().messages().list().execute.return_value = {
        "messages": [
            {"name": "spaces/S/messages/M1", "thread": {"name": "spaces/S/threads/T1"}},
            {"name": "spaces/S/messages/M2", "thread": {"name": "spaces/S/threads/T1"}},
            {"name": "spaces/S/messages/M3", "thread": {"name": "spaces/S/threads/T2"}},
            {"name": "spaces/S/messages/M4"},
        ]
    }

    result = await list_chat_threads(service, space_id="spaces/S", page_size=50)
    assert "T1: 2 message(s)" in result
    assert "T2: 1 message(s)" in result
    assert "default thread: 1 message(s)" in result


# ---------------------------------------------------------------------------
# Dispatcher-level tests (via the unwrapped tools, mocked service injected)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_manage_dispatches_create_space():

    tool = next(
        t
        for t in await server.list_tools(run_middleware=False)
        if t.name == "chat_manage"
    )
    fn = _unwrap(tool)

    service = Mock()
    service.spaces().create().execute.return_value = _space(
        name="spaces/NEW", display="New Room"
    )

    result = await fn(
        service=service,
        user_google_email="user@example.com",
        action="create_space",
        display_name="New Room",
        space_type="SPACE",
    )
    assert "Space created" in result
    assert "spaces/NEW" in result


@pytest.mark.asyncio
async def test_chat_read_dispatches_list_threads():

    tool = next(
        t
        for t in await server.list_tools(run_middleware=False)
        if t.name == "chat_read"
    )
    fn = _unwrap(tool)

    service = Mock()
    service.spaces().messages().list().execute.return_value = {
        "messages": [
            {"name": "spaces/S/messages/M1", "thread": {"name": "spaces/S/threads/T1"}},
        ]
    }

    result = await fn(
        service=service,
        user_google_email="user@example.com",
        action="list_threads",
        space_id="spaces/S",
    )
    assert "T1: 1 message(s)" in result


@pytest.mark.asyncio
async def test_chat_manage_unknown_action():

    tool = next(
        t
        for t in await server.list_tools(run_middleware=False)
        if t.name == "chat_manage"
    )
    fn = _unwrap(tool)

    result = await fn(
        service=Mock(),
        user_google_email="user@example.com",
        action="does_not_exist",
    )
    assert "Unknown action" in result
