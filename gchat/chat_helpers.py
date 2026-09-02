"""
Shared logic for Google Chat workspace-management operations.

Implements the actions behind the ``chat_manage`` dispatcher tool. Kept out of
``chat_tools.py`` so the tool file holds only the signature and dispatch, and
the operations stay independently testable.
"""

import logging
import asyncio
from typing import Dict, Optional

from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)


def _format_space(space: Dict) -> str:
    """One-line summary of a Chat space."""
    name = space.get("displayName") or space.get("name", "")
    space_id = space.get("name", "")
    stype = space.get("spaceType", "UNKNOWN")
    threaded = "threaded" if space.get("threaded") else "flat"
    return f"{name} (ID: {space_id}, type: {stype}, {threaded})"


async def list_chat_spaces(
    service, *, page_size: int = 100, space_type: str = "all"
) -> str:
    """List spaces, optionally filtered to rooms or DMs."""
    params = {"pageSize": page_size}
    if space_type == "room":
        params["filter"] = "spaceType = SPACE"
    elif space_type == "dm":
        params["filter"] = "spaceType = DIRECT_MESSAGE"
    response = await asyncio.to_thread(service.spaces().list(**params).execute)
    spaces = response.get("spaces", [])
    if not spaces:
        return f"No Chat spaces found (type: {space_type})."
    lines = [f"Found {len(spaces)} Chat spaces (type: {space_type}):"]
    lines += [f"- {_format_space(s)}" for s in spaces]
    return "\n".join(lines)


async def find_chat_space(
    service, *, query: str, exact: bool = False, max_results: int = 50
) -> str:
    """Find spaces by display name (client-side match over spaces.list)."""
    response = await asyncio.to_thread(service.spaces().list(pageSize=100).execute)
    spaces = response.get("spaces", [])
    q = query.lower()
    if exact:
        hits = [s for s in spaces if (s.get("displayName") or "").lower() == q]
    else:
        hits = [s for s in spaces if q in (s.get("displayName") or "").lower()]
    if not hits:
        return f"No space matching '{query}' (exact={exact})."
    if len(hits) > max_results:
        hits = hits[:max_results]
        truncated = True
    else:
        truncated = False
    lines = [f"Found {len(hits)} space(s) matching '{query}':"]
    lines += [f"- {_format_space(s)}" for s in hits]
    if truncated:
        lines.append(f"(truncated at {max_results})")
    return "\n".join(lines)


async def create_chat_space(
    service,
    *,
    display_name: Optional[str] = None,
    space_type: str = "SPACE",
    external_user_allowed: bool = False,
) -> str:
    """Create a chat space (SPACE or GROUP_CHAT)."""
    body: Dict = {"spaceType": space_type}
    if display_name:
        body["displayName"] = display_name
    if external_user_allowed:
        body["externalUserAllowed"] = True
    space = await asyncio.to_thread(service.spaces().create(body=body).execute)
    return f"Space created: {_format_space(space)}"


async def find_or_create_dm_space(service, *, user_id: str) -> str:
    """Find an existing direct-message space with the given user, else create one.

    ``user_id`` is the Chat user resource name such as ``users/123456789``.
    An existing DM is found by listing DIRECT_MESSAGE spaces and checking the
    member list; a missing DM is created with spaceType DIRECT_MESSAGE and the
    user added as a member.
    """
    user_id = user_id.strip()
    if not user_id.startswith("users/"):
        user_id = f"users/{user_id}"

    response = await asyncio.to_thread(
        service.spaces().list(pageSize=100, filter="spaceType = DIRECT_MESSAGE").execute
    )
    for space in response.get("spaces", []):
        space_name = space.get("name", "")
        if not space_name:
            continue
        try:
            members = await asyncio.to_thread(
                service.spaces()
                .members()
                .list(parent=space_name, pageSize=1000)
                .execute
            )
        except HttpError as e:
            logger.debug("members.list failed for %s: %s", space_name, e)
            continue
        member_names = {
            m.get("member", {}).get("name", "") for m in members.get("memberships", [])
        }
        if user_id in member_names:
            return f"Found existing DM: {_format_space(space)}"

    space = await asyncio.to_thread(
        service.spaces().create(body={"spaceType": "DIRECT_MESSAGE"}).execute
    )
    space_name = space.get("name", "")
    try:
        await asyncio.to_thread(
            service.spaces()
            .members()
            .create(
                parent=space_name,
                body={"member": {"name": user_id}},
            )
            .execute
        )
    except HttpError as e:
        logger.warning("Adding member to new DM failed: %s", e)
        return f"Created DM space {space_name} but could not add member {user_id}: {e}"
    return f"Created new DM with {user_id}: {_format_space(space)}"


async def create_chat_reaction(service, *, message_id: str, emoji_unicode: str) -> str:
    """Add an emoji reaction to a message."""
    reaction = await asyncio.to_thread(
        service.spaces()
        .messages()
        .reactions()
        .create(
            parent=message_id,
            body={"emoji": {"unicode": emoji_unicode}},
        )
        .execute
    )
    reaction_name = reaction.get("name", "")
    return f"Reacted with {emoji_unicode} on message {message_id}. Reaction ID: {reaction_name}"


async def delete_chat_reaction(service, *, reaction_id: str) -> str:
    """Delete a reaction by its resource name."""
    await asyncio.to_thread(
        service.spaces().messages().reactions().delete(name=reaction_id).execute
    )
    return f"Deleted reaction {reaction_id}."


async def list_chat_reactions(service, *, message_id: str, page_size: int = 50) -> str:
    """List emoji reactions on a message."""
    response = await asyncio.to_thread(
        service.spaces()
        .messages()
        .reactions()
        .list(parent=message_id, pageSize=page_size)
        .execute
    )
    reactions = response.get("reactions", [])
    if not reactions:
        return f"No reactions on message {message_id}."
    lines = [f"Reactions on {message_id}:"]
    for r in reactions:
        emoji = r.get("emoji", {})
        symbol = (
            emoji.get("unicode") or f":{emoji.get('customEmoji', {}).get('uid', '?')}:"
        )
        lines.append(f"- {symbol} ({r.get('name', '')})")
    return "\n".join(lines)


async def list_chat_threads(service, *, space_id: str, page_size: int = 50) -> str:
    """List distinct threads in a space by grouping messages on thread.name.

    The Chat API has no threads endpoint, so threads are derived from the most
    recent messages: one line per thread, showing the thread resource name and
    how many of the fetched messages belong to it. Also reports the default
    (``0``-suffixed) main-thread marker when present.
    """
    response = await asyncio.to_thread(
        service.spaces()
        .messages()
        .list(parent=space_id, pageSize=page_size, orderBy="createTime desc")
        .execute
    )
    messages = response.get("messages", [])
    if not messages:
        return f"No messages found in space {space_id}."

    thread_groups: Dict[str, int] = {}
    for msg in messages:
        thread = msg.get("thread", {})
        key = thread.get("name") or "MAIN"
        thread_groups[key] = thread_groups.get(key, 0) + 1

    lines = [f"Threads in {space_id} (from latest {len(messages)} messages):"]
    for key, count in sorted(thread_groups.items(), key=lambda x: -x[1]):
        label = "default thread" if key == "MAIN" else key
        lines.append(f"- {label}: {count} message(s)")
    return "\n".join(lines)
