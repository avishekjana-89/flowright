"""Minimal keyword registry and helpers for runner.py

This module provides:
- KeywordRegistry, a thread-safe registry
- registry, the global registry instance
- keyword decorator for registering callables
- import_by_path(path) to import callables like 'pkg.module:callable'
- run_keyword_async(callable, page, step) to invoke sync/async callables

Keep this module intentionally small and dependency-free (uses stdlib only).
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import threading
import logging
from typing import Any, Callable
import os
import importlib.util
import re

logger = logging.getLogger(__name__)


class KeywordRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._handlers: dict[str, dict[str, Any]] = {}

    def register(self, name: str, func: Callable, metadata: dict | None = None):
        metadata = metadata or {}
        with self._lock:
            if name in self._handlers and not metadata.get("override"):
                raise KeyError(f"Keyword already registered: {name}")
            self._handlers[name] = {"func": func, "meta": metadata}

    def get(self, name: str) -> Callable:
        with self._lock:
            return self._handlers[name]["func"]

    def list(self) -> dict:
        with self._lock:
            return {n: info["meta"] for n, info in self._handlers.items()}


# global registry instance
registry = KeywordRegistry()


def keyword(name: str | None = None, **metadata: Any):
    """Decorator to register a function as a keyword.

    Registered callables receive (page, step) by convention in this runner.
    """

    def deco(func: Callable):
        kw_name = name or getattr(func, "__name__", None)
        try:
            registry.register(kw_name, func, metadata)
        except KeyError:
            logger.warning("Keyword %s already registered; overwriting", kw_name)
            # allow overwrite intentionally for development convenience
            registry._handlers[kw_name] = {"func": func, "meta": metadata}
        return func

    return deco


def import_by_path(path: str) -> Callable:
    """Import a callable given a path like 'pkg.module:callable' or 'pkg.module.attr'.

    Returns the callable object.
    """
    if ":" in path:
        module_path, attr = path.split(":", 1)
    elif path.count(".") >= 1:
        parts = path.rsplit(".", 1)
        module_path, attr = parts[0], parts[1]
    else:
        raise ImportError(f"Invalid import path: {path}")

    mod = importlib.import_module(module_path)
    obj = getattr(mod, attr)
    return obj


def _safe_module_name_from_path(path: str) -> str:
    """Create a safe module name for importlib.spec_from_file_location from a file path.

    Replaces non-alphanumeric chars with underscores and prefixes to avoid collisions.
    """
    base = os.path.basename(path)
    name = os.path.splitext(base)[0]
    # replace invalid chars with underscore
    name = re.sub(r"[^0-9a-zA-Z_]", "_", name)
    return f"keywords_{name}"


def load_keywords_from_dir(dir_path: str) -> dict:
    """Load all .py files from dir_path into the current process so module-level
    keyword registrations (via @keyword) run and populate the registry.

    Returns a dict with keys: 'loaded': [modules], 'errors': {path: str(exception)}
    """
    results = {"loaded": [], "errors": {}}
    if not dir_path or not os.path.isdir(dir_path):
        return results

    for fname in os.listdir(dir_path):
        if not fname.endswith('.py'):
            continue
        fpath = os.path.join(dir_path, fname)
        try:
            mod_name = _safe_module_name_from_path(fpath)
            spec = importlib.util.spec_from_file_location(mod_name, fpath)
            if spec is None or spec.loader is None:
                results["errors"][fpath] = "could not create spec"
                continue
            module = importlib.util.module_from_spec(spec)
            # execute module code; module-level @keyword calls will register handlers
            spec.loader.exec_module(module)
            results["loaded"].append(mod_name)
        except Exception as e:
            results["errors"][fpath] = str(e)
    return results


async def run_keyword_async(target_callable: Callable, page: Any, step: dict) -> Any:
    """Invoke a keyword callable (sync or async). Returns whatever the callable returns.

    Convention: keyword callables accept (page, step).
    """
    func = target_callable
    try:
        if inspect.iscoroutinefunction(func):
            return await func(page, step)

        # Call sync functions directly on the event loop thread. Previously we
        # ran sync functions in a thread executor which caused Playwright
        # objects (async-only) to be used from another thread and fail. If a
        # sync function returns an awaitable (user accidentally returned a
        # coroutine), await it.
        result = func(page, step)
        if asyncio.iscoroutine(result) or inspect.isawaitable(result):
            return await result
        return result
    except Exception:
        # Let caller handle/log exceptions; re-raise for transparency
        raise
