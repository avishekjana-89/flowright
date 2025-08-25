"""Context-local variables proxy for Flowwright.

This module provides a ContextVar-backed mapping proxy so code can use
`from runner import variables` (or `from variables import variables`) and
perform mapping operations while keeping values isolated per async task.
"""
from __future__ import annotations

import contextvars
from typing import Any

# internal context var
_variables_ctx = contextvars.ContextVar('flowwright_variables', default=None)


class VariablesProxy:
    def _get(self):
        v = _variables_ctx.get()
        if v is None:
            raise NameError("variables is not set for the current test")
        return v

    def __getitem__(self, key: str) -> Any:
        return self._get()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._get()[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._get().get(key, default)

    def items(self):
        return self._get().items()

    def keys(self):
        return self._get().keys()

    def values(self):
        return self._get().values()

    def update(self, *args, **kwargs):
        return self._get().update(*args, **kwargs)

    def clear(self):
        return self._get().clear()


# public proxy instance
variables = VariablesProxy()


def set_variables_dict(d: dict) -> contextvars.Token:
    """Set a new dict for the current context and return the token."""
    return _variables_ctx.set(d)


def reset_variables_token(token: contextvars.Token) -> None:
    """Reset the context var using the given token."""
    _variables_ctx.reset(token)


# explicit public API
__all__ = ['variables', 'set_variables_dict', 'reset_variables_token']
