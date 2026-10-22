"""
Google Chat Helper Functions

Request execution, argument normalization, message parsing, and name
resolution for Chat senders and spaces via the People API.
"""

import asyncio
import inspect
import re
import logging
import ssl
from typing import Dict, List, Optional

from googleapiclient.errors import HttpError
from core.utils import UserInputError
from auth.permissions import require_action
from core.action_dispatch import normalize_action_params, call_action

logger = logging.getLogger(__name__)

_SENDER_CACHE_MAX_SIZE = 256
_sender_name_cache: Dict[str, str] = {}
_UNNAMED_SPACE_FALLBACKS = {
    "DIRECT_MESSAGE": "Direct message",
    "GROUP_CHAT": "Group chat",
}
_SPACE_NAME_MAX_MEMBERS = 3
_PEOPLE_BATCH_SIZE = 200  # people.getBatchGet limit
_MAX_CONSECUTIVE_MEMBER_LOOKUP_FAILURES = 2
_SCOPE_ERROR_REASON = "ACCESS_TOKEN_SCOPE_INSUFFICIENT"
_SEARCH_MESSAGES_RETRY_BASE_DELAY_SECONDS = 1


def _cache_sender(user_id: str, name: str) -> None:
    """Store a resolved sender name, evicting oldest entries if cache is full."""
    if len(_sender_name_cache) >= _SENDER_CACHE_MAX_SIZE:
        to_remove = list(_sender_name_cache.keys())[: _SENDER_CACHE_MAX_SIZE // 2]
        for k in to_remove:
            del _sender_name_cache[k]
    _sender_name_cache[user_id] = name


def _person_name(person: dict) -> Optional[str]:
    """Return a People API person's display name, falling back to their email."""
    names = person.get("names") or [{}]
    emails = person.get("emailAddresses") or [{}]
    return names[0].get("displayName") or emails[0].get("value")


async def _resolve_sender(people_service, sender_obj: dict) -> str:
    """Resolve a Chat message sender to a display name.

    Fast path: use displayName if the API already provided it.
    Slow path: look up the user via the People API directory and cache the result.
    """
    display_name = sender_obj.get("displayName")
    if display_name:
        return display_name

    user_id = sender_obj.get("name", "")  # e.g. "users/123456789"
    if not user_id:
        return "Unknown Sender"

    if user_id in _sender_name_cache:
        return _sender_name_cache[user_id]

    # Chat API uses "users/ID" but People API expects "people/ID"
    people_resource = user_id.replace("users/", "people/", 1)
    if people_service:
        try:
            person = await asyncio.to_thread(
                people_service.people()
                .get(resourceName=people_resource, personFields="names,emailAddresses")
                .execute
            )
            resolved = _person_name(person)
            if resolved:
                _cache_sender(user_id, resolved)
                return resolved
        except HttpError as e:
            logger.debug(f"People API lookup failed for {user_id}: {e}")
        except Exception as e:
            logger.debug(f"Unexpected error resolving {user_id}: {e}")

    return user_id


async def _lookup_people_names(people_service, user_ids: List[str]) -> Dict[str, str]:
    """Resolve Chat user ids to display names with batched People API calls."""
    names = {}
    missing = []
    for user_id in user_ids:
        if user_id in _sender_name_cache:
            names[user_id] = _sender_name_cache[user_id]
        else:
            missing.append(user_id)
    for start in range(0, len(missing), _PEOPLE_BATCH_SIZE):
        chunk = missing[start : start + _PEOPLE_BATCH_SIZE]
        try:
            response = await asyncio.to_thread(
                people_service.people()
                .getBatchGet(
                    resourceNames=[
                        user_id.replace("users/", "people/", 1) for user_id in chunk
                    ],
                    personFields="names,emailAddresses",
                )
                .execute
            )
        except Exception as e:
            logger.debug(f"People API batch lookup failed: {e}")
            continue
        for entry in response.get("responses", []):
            name = _person_name(entry.get("person") or {})
            requested = entry.get("requestedResourceName")
            if name and requested:
                user_id = requested.replace("people/", "users/", 1)
                names[user_id] = name
                _cache_sender(user_id, name)
    return names


def _is_scope_error(error: HttpError) -> bool:
    """Return True when an HttpError carries an ACCESS_TOKEN_SCOPE_INSUFFICIENT detail."""
    details = error.error_details
    return isinstance(details, list) and any(
        isinstance(d, dict) and d.get("reason") == _SCOPE_ERROR_REASON for d in details
    )


async def _list_memberships(chat_service, space_name: str) -> List[dict]:
    """Return every membership of a space, following nextPageToken."""
    memberships = []
    params = {"parent": space_name, "pageSize": 100}
    while True:
        response = await asyncio.to_thread(
            chat_service.spaces().members().list(**params).execute
        )
        memberships.extend(response.get("memberships", []))
        if not response.get("nextPageToken"):
            return memberships
        params["pageToken"] = response["nextPageToken"]


async def _name_spaces(
    chat_service, people_service, spaces: List[dict]
) -> Dict[str, str]:
    """Map space resource names to labels, naming unnamed spaces after their members."""
    labels = {}
    unnamed = []
    for space in spaces:
        space_name = space.get("name")
        if space_name in labels:
            continue
        if space.get("displayName"):
            labels[space_name] = space["displayName"]
        else:
            labels[space_name] = _UNNAMED_SPACE_FALLBACKS.get(
                space.get("spaceType"), "Unnamed Space"
            )
            unnamed.append(space_name)
    if not unnamed:
        return labels

    # Without the caller's id their own name would end up in every label.
    try:
        me = await asyncio.to_thread(
            people_service.people()
            .get(resourceName="people/me", personFields="metadata")
            .execute
        )
    except Exception as e:
        logger.debug(f"Could not look up the calling user: {e}")
        return labels
    if not me.get("resourceName"):
        return labels
    me_id = me["resourceName"].replace("people/", "users/", 1)

    others_by_space = {}
    consecutive_failures = 0
    for space_name in unnamed:
        try:
            memberships = await _list_memberships(chat_service, space_name)
        except HttpError as e:
            logger.debug(f"Could not list members of {space_name}: {e}")
            # Missing scope or throttling would fail every remaining space too.
            if e.resp.status == 429 or _is_scope_error(e):
                break
            continue
        except Exception as e:
            logger.debug(f"Could not list members of {space_name}: {e}")
            # Repeated network failures would make every remaining space wait too.
            consecutive_failures += 1
            if consecutive_failures >= _MAX_CONSECUTIVE_MEMBER_LOOKUP_FAILURES:
                break
            continue
        consecutive_failures = 0
        others = []
        for membership in memberships:
            member = membership.get("member", {})
            if member.get("type") == "HUMAN" and member.get("name") != me_id:
                others.append(member.get("name"))
        if others:
            others_by_space[space_name] = others

    shown_ids = []
    for others in others_by_space.values():
        for user_id in others[:_SPACE_NAME_MAX_MEMBERS]:
            if user_id not in shown_ids:
                shown_ids.append(user_id)
    people_names = await _lookup_people_names(people_service, shown_ids)

    for space_name, others in others_by_space.items():
        shown = others[:_SPACE_NAME_MAX_MEMBERS]
        label = ", ".join(people_names.get(user_id, user_id) for user_id in shown)
        remaining = len(others) - len(shown)
        if remaining:
            label += f" and {remaining} other{'s' if remaining > 1 else ''}"
        labels[space_name] = label
    return labels


def _none_if_null_sentinel(value: Optional[str]) -> Optional[str]:
    """Map the literal "null"/"None" some clients send for an omitted arg to None."""
    if value is not None and value.strip().lower() in ("null", "none"):
        return None
    return value


def _none_if_blank(value: Optional[str]) -> Optional[str]:
    """Treat a null sentinel or whitespace-only string as an omitted arg."""
    value = _none_if_null_sentinel(value)
    return value if value and value.strip() else None


async def _execute_chat_request(
    request_factory,
    *,
    request_label: str,
    retries: int = 1,
    semaphore: Optional[asyncio.Semaphore] = None,
):
    """Execute a Chat API request in a worker thread with optional SSL retries."""
    for attempt in range(retries):
        try:
            if semaphore is None:
                return await asyncio.to_thread(lambda: request_factory().execute())
            async with semaphore:
                return await asyncio.to_thread(lambda: request_factory().execute())
        except ssl.SSLError as e:
            if attempt == retries - 1:
                raise
            delay = _SEARCH_MESSAGES_RETRY_BASE_DELAY_SECONDS * (2**attempt)
            logger.warning(
                "[search_messages] SSL error during %s on attempt %s/%s: %s. Retrying in %s seconds.",
                request_label,
                attempt + 1,
                retries,
                e,
                delay,
            )
            await asyncio.sleep(delay)


def _extract_rich_links(msg: dict) -> List[str]:
    """Extract URLs from RICH_LINK annotations (smart chips).

    When a user pastes a Google Workspace URL in Chat and it renders as a
    smart chip, the URL is NOT in the text field; it's only available in
    the annotations array as a RICH_LINK with richLinkMetadata.uri.
    """
    text = msg.get("text", "")
    urls = []
    for ann in msg.get("annotations", []):
        if ann.get("type") == "RICH_LINK":
            uri = ann.get("richLinkMetadata", {}).get("uri", "")
            if uri and uri not in text:
                urls.append(uri)
    return urls


def _format_space(space: Dict) -> str:
    """One-line summary of a Chat space."""
    name = space.get("displayName") or space.get("name", "")
    space_id = space.get("name", "")
    stype = space.get("spaceType", "UNKNOWN")
    threaded = space.get("spaceThreadingState") or (
        "threaded" if space.get("threaded") else "unspecified threading"
    )
    return f"{name} (ID: {space_id}, type: {stype}, {threaded})"


async def create_chat_space(
    service,
    *,
    display_name: Optional[str] = None,
    space_type: str = "SPACE",
) -> str:
    """Create a named SPACE using user OAuth; group-chat import mode is unsupported."""
    if space_type != "SPACE" or not display_name or not display_name.strip():
        raise UserInputError("create_space requires display_name and space_type=SPACE")
    body: Dict = {"spaceType": space_type}
    if display_name:
        body["displayName"] = display_name
    space = await asyncio.to_thread(service.spaces().create(body=body).execute)
    return f"Space created: {_format_space(space)}"


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


async def find_chat_space(
    service, *, query: str, exact: bool = False, max_results: int = 50
) -> str:
    """Match display names across paginated spaces, stopping at max_results."""
    if not query.strip() or not 1 <= max_results <= 1000:
        raise UserInputError("query must be non-empty and max_results must be 1..1000")
    hits, token = [], None
    while True:
        response = await asyncio.to_thread(
            service.spaces()
            .list(pageSize=100, **({"pageToken": token} if token else {}))
            .execute
        )
        for space in response.get("spaces", []):
            name = (space.get("displayName") or "").casefold()
            if name == query.casefold() if exact else query.casefold() in name:
                hits.append(space)
                if len(hits) >= max_results:
                    return (
                        "\n".join(_format_space(s) for s in hits)
                        + f"\n(result limit: {max_results})"
                    )
        token = response.get("nextPageToken")
        if not token:
            return "\n".join(_format_space(s) for s in hits) or "No matching spaces."


async def find_or_create_dm_space(service, *, user_id: str) -> str:
    """Use native findDirectMessage and atomic setup instead of partial DM creation."""
    if not re.fullmatch(r"users/[^/\s]+", user_id):
        raise UserInputError("user_id must be a users/ID resource name")
    try:
        space = await asyncio.to_thread(
            service.spaces().findDirectMessage(name=user_id).execute
        )
        return f"Found existing DM: {_format_space(space)}"
    except HttpError as exc:
        if exc.resp.status != 404:
            raise
    space = await asyncio.to_thread(
        service.spaces()
        .setup(
            body={
                "space": {"spaceType": "DIRECT_MESSAGE"},
                "memberships": [{"member": {"name": user_id, "type": "HUMAN"}}],
            }
        )
        .execute
    )
    return f"Created DM: {_format_space(space)}"


async def dispatch_chat(service, family: str, action: str, params=None) -> str:
    actions = {
        "read": {
            "find_space": find_chat_space,
            "list_reactions": list_chat_reactions,
            "list_threads": list_chat_threads,
        },
        "manage": {
            "create_space": create_chat_space,
            "dm_space": find_or_create_dm_space,
            "delete_reaction": delete_chat_reaction,
        },
    }[family]
    if action not in actions:
        raise UserInputError(f"Unknown Chat {family} action: {action}")
    require_action("chat", "read" if family == "read" else action)
    extra = normalize_action_params(params)
    for field, pattern in (
        ("message_id", r"spaces/[^/\s]+/messages/[^/\s]+"),
        ("space_id", r"spaces/[^/\s]+"),
        ("reaction_id", r"spaces/[^/\s]+/messages/[^/\s]+/reactions/[^/\s]+"),
    ):
        if field in extra and (
            not isinstance(extra[field], str) or not re.fullmatch(pattern, extra[field])
        ):
            raise UserInputError(f"Invalid {field} resource name")
    if "page_size" in extra and (
        not isinstance(extra["page_size"], int) or not 1 <= extra["page_size"] <= 1000
    ):
        raise UserInputError("page_size must be 1..1000")
    if action == "create_space" and extra.get("space_type", "SPACE") != "SPACE":
        raise UserInputError("space_type must be SPACE")
    function = actions[action]
    try:
        inspect.signature(function).bind(service, **extra)
    except TypeError as exc:
        raise UserInputError(f"Invalid {action} parameters: {exc}") from exc
    return await call_action(function, service, **extra)
