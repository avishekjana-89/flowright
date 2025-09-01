"""Small collection of runner helper utilities factored out of runner.py.

Contains logging and placeholder substitution utilities used by the runner.
"""
import os
import json
import re
import asyncio
import time
import datetime
from typing import Any

# Load profile key/values from env (if provided by webapp)
try:
    PROFILE_KV = json.loads(os.getenv('RUNNER_PROFILE_JSON') or '{}')
except Exception:
    PROFILE_KV = {}


def log(msg: Any, *args, **kwargs) -> None:
    """Simple logger that prefixes messages with asyncio task id when available."""
    try:
        task = asyncio.current_task()
        task_id = id(task) if task is not None else 0
    except Exception:
        task_id = 0
    try:
        ts = time.time()
        # Format timestamp as human-readable local datetime with milliseconds
        try:
            human_ts = datetime.datetime.fromtimestamp(ts).isoformat(sep=' ', timespec='milliseconds')
        except Exception:
            # Fallback for older Python versions or unusual environments
            human_ts = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        prefix = f"\n[{human_ts}] task:{task_id}"
    except Exception:
        prefix = f"\ntask:{task_id}"
    try:
        if args or kwargs:
            print(prefix + ' ' + (msg.format(*args, **kwargs) if isinstance(msg, str) else str(msg)))
        else:
            print(prefix + ' ' + str(msg))
    except Exception:
        try:
            print(prefix + ' ' + str(msg))
        except Exception:
            pass


def _substitute_global_vars_in_value(val: Any) -> Any:
    """Replace {{GlobalVariables.KEY}} occurrences in a string using PROFILE_KV."""
    if not isinstance(val, str):
        return val

    def repl(m):
        key = m.group(1)
        return str(PROFILE_KV.get(key, ''))

    return re.sub(r"\{\{\s*GlobalVariables\.([a-zA-Z0-9_]+)\s*\}\}", repl, val)


def substitute_globals_in_step(step: Any) -> Any:
    """Walk a single step dict and substitute global vars in string fields."""
    if isinstance(step, dict):
        out = {}
        for k, v in step.items():
            if isinstance(v, str):
                out[k] = _substitute_global_vars_in_value(v)
            elif isinstance(v, dict):
                sub = {}
                for kk, vv in v.items():
                    sub[kk] = _substitute_global_vars_in_value(vv) if isinstance(vv, str) else vv
                out[k] = sub
            elif isinstance(v, list):
                out[k] = [_substitute_global_vars_in_value(x) if isinstance(x, str) else x for x in v]
            else:
                out[k] = v
        return out
    return step


# Local variable substitution uses the same pattern but pulls from a per-run
# variables mapping provided by the `variables` module.
try:
    from variables import variables
except Exception:
    variables = None


def _substitute_local_vars_in_value(val: Any) -> Any:
    """Replace {{LocalVariables.KEY}} occurrences in a string using the per-test variables proxy."""
    if not isinstance(val, str):
        return val

    def repl(m):
        key = m.group(1)
        try:
            if variables is None:
                return ''
            return str(variables.get(key, '') or '')
        except NameError:
            return ''

    return re.sub(r"\{\{\s*LocalVariables\.([a-zA-Z0-9_\-]+)\s*\}\}", repl, val)


def substitute_locals_in_step(step: Any) -> Any:
    """Walk a single step dict and substitute local vars in string fields."""
    if isinstance(step, dict):
        out = {}
        for k, v in step.items():
            if isinstance(v, str):
                out[k] = _substitute_local_vars_in_value(v)
            elif isinstance(v, dict):
                sub = {}
                for kk, vv in v.items():
                    sub[kk] = _substitute_local_vars_in_value(vv) if isinstance(vv, str) else vv
                out[k] = sub
            elif isinstance(v, list):
                out[k] = [_substitute_local_vars_in_value(x) if isinstance(x, str) else x for x in v]
            else:
                out[k] = v
        return out
    return step


__all__ = [
    'log',
    'substitute_globals_in_step',
    'substitute_locals_in_step',
    'get_target_context',
    'resolve_selector_refs_in_step',
    'PROFILE_KV',
]


async def get_target_context(page, step):
    """Return a context (page or Frame) for the given step's frameInfo.

    If step contains an "inIframe" flag and a "frameInfo" list, walk the
    frames in order and return the innermost Frame object. Otherwise return
    the original page.
    """
    if step.get("inIframe"):
        frame_info = step.get("frameInfo", [])
        context = page
        for f in sorted(frame_info, key=lambda x: x.get("index", 0)):
            selector = f.get("frameSelector")
            if not selector:
                continue
            elem = None
            try:
                elem = await context.locator(selector).element_handle()
            except Exception:
                elem = None
            if not elem:
                try:
                    elem = await context.query_selector(selector)
                except Exception:
                    elem = None
            if not elem:
                raise Exception(f"Frame element {selector} not found")
            new_frame = await elem.content_frame()
            if not new_frame:
                raise Exception(f"Frame {selector} has no content_frame()")
            context = new_frame
        return context
    return page


def resolve_selector_refs_in_step(step, seen=None, parent_obj_id=None):
    """Resolve selectorRef placeholders (strings starting with '$') in a step dict.

    Mutates `step` in-place. Uses an optional `seen` cache mapping object ids
    to loaded locators to avoid repeated file reads.
    """
    if not isinstance(step, dict):
        return
    if seen is None:
        seen = {}

    obj_id = step.get('object-folder-id') or step.get('object_folder_id') or parent_obj_id

    def _load_locators_for(obj_id_val):
        if obj_id_val is None:
            return {}
        if obj_id_val in seen:
            return seen[obj_id_val] or {}
        locs = {}
        try:
            # Try to import webapp helpers if available
            try:
                from webapp.utils import OBJECTS_DIR as _OBJECTS_DIR
                from webapp.db import get_object_folder as _get_object_folder
            except Exception:
                _OBJECTS_DIR = None
                _get_object_folder = None

            if _OBJECTS_DIR and _get_object_folder:
                of = _get_object_folder(obj_id_val)
                if of and of.get('name'):
                    obj_dir = os.path.join(_OBJECTS_DIR, of['name'])
                    loc_path = os.path.join(obj_dir, 'locators.json')
                    if os.path.exists(loc_path):
                        try:
                            with open(loc_path, 'r', encoding='utf-8') as lf:
                                locs = json.load(lf) or {}
                        except Exception:
                            locs = {}
        except Exception:
            locs = {}
        seen[obj_id_val] = locs
        return locs

    sel_ref = step.get('selectorRef')
    if isinstance(sel_ref, str) and sel_ref.startswith('$') and obj_id:
        locs = _load_locators_for(obj_id)
        entry = (locs or {}).get(sel_ref)
        if entry and isinstance(entry, dict):
            selectors = entry.get('selectors') or []
            if isinstance(selectors, list) and selectors:
                step['selectors'] = selectors

    for k, v in list(step.items()):
        if k in ('targetSelector', 'frameSelector') and isinstance(v, str) and v.startswith('$'):
            use_obj = obj_id or parent_obj_id
            if use_obj:
                locs = _load_locators_for(use_obj)
                entry = (locs or {}).get(v)
                if entry and isinstance(entry, dict):
                    sels = entry.get('selectors') or []
                    if isinstance(sels, list) and sels:
                        try:
                            step[k] = sels[0]
                        except Exception:
                            pass

        elif k == 'selectors' and isinstance(v, list) and len(v) > 0:
            first = v[0]
            if isinstance(first, str) and first.startswith('$') and obj_id:
                locs = _load_locators_for(obj_id)
                entry = (locs or {}).get(first)
                if entry and isinstance(entry, dict):
                    sels = entry.get('selectors') or []
                    if isinstance(sels, list) and sels:
                        try:
                            step[k] = sels
                        except Exception:
                            pass

        if isinstance(v, dict):
            resolve_selector_refs_in_step(v, seen=seen, parent_obj_id=obj_id)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    resolve_selector_refs_in_step(item, seen=seen, parent_obj_id=obj_id)
