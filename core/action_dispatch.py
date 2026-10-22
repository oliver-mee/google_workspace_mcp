"""Parameter normalization and validation shared by compact service actions."""

import json
from functools import lru_cache

from pydantic import ValidationError, validate_call

from core.utils import UserInputError


def normalize_action_params(params):
    if params is None:
        return {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError as exc:
            raise UserInputError(
                "params must be an object or JSON-encoded object"
            ) from exc
    if not isinstance(params, dict):
        raise UserInputError("params must be an object")
    return params.copy()


@lru_cache(maxsize=128)
def _validated(function):
    return validate_call(function, config={"arbitrary_types_allowed": True})


async def call_action(function, *args, **kwargs):
    try:
        return await _validated(function)(*args, **kwargs)
    except ValidationError as exc:
        raise UserInputError(f"Invalid action parameters: {exc}") from exc
