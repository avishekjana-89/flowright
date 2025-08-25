"""Utility helpers and path constants for the webapp package.

This module centralizes filesystem paths and small helpers used by the
FastAPI handlers in `main.py`. It is intentionally behavior-preserving:
we moved code from `main.py` here without changing semantics.
"""
import os
import json
import re
import csv
import io
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
STEPS_DIR = os.path.join(os.path.dirname(__file__), 'steps')
RUNS_DIR = os.path.join(os.path.dirname(__file__), 'runs')
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
FILES_DIR = os.path.join(os.path.dirname(__file__), 'files')
RUNNER_SCRIPT = os.path.join(ROOT, 'runner.py')
KEYWORDS_DIR = os.path.join(ROOT, 'keywords')
OBJECTS_DIR = os.path.join(os.path.dirname(__file__), 'objects')

# Ensure folders exist (idempotent)
os.makedirs(KEYWORDS_DIR, exist_ok=True)
os.makedirs(STEPS_DIR, exist_ok=True)
os.makedirs(RUNS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(FILES_DIR, exist_ok=True)
os.makedirs(OBJECTS_DIR, exist_ok=True)

# Allow placeholder keys to contain a wider set of characters (spaces, hyphens,
# dots, etc.) while still excluding the special prefixes GlobalVariables. and
# LocalVariables. The capturing group returns the key text inside the braces.
PLACEHOLDER_RE = re.compile(r"\{\{\s*(?!(?:GlobalVariables|LocalVariables)\.)([^\}]+?)\s*\}\}")


def step_path(name: str) -> str:
    return os.path.join(STEPS_DIR, name)


def _sanitize_name(name: str) -> str:
    # produce a filesystem-friendly name from arbitrary suite/test names
    if not name:
        return 'run'
    s = re.sub(r"[^a-zA-Z0-9_\- ]+", '', name)
    s = s.strip().replace(' ', '-')
    return s[:120]


def load_dataset_file(filename: str):
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if filename.lower().endswith('.json'):
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            raise ValueError('JSON dataset must be an array of objects')
    elif filename.lower().endswith('.csv'):
        with open(path, 'r', encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f)
            return [r for r in reader]
    else:
        raise ValueError('Unsupported dataset format: ' + filename)


def _atomic_write_bytes(path, data):
    """Write bytes or string to path atomically (temp file + replace).
    Accepts bytes, str, or None. Ensures parent dir exists.
    """
    try:
        dirn = os.path.dirname(path) or '.'
        os.makedirs(dirn, exist_ok=True)
        if data is None:
            b = b''
        elif isinstance(data, bytes):
            b = data
        elif isinstance(data, str):
            b = data.encode('utf-8')
        else:
            b = str(data).encode('utf-8')
        fd, tmp = tempfile.mkstemp(dir=dirn)
        try:
            with os.fdopen(fd, 'wb') as tf:
                tf.write(b)
                try:
                    tf.flush()
                    os.fsync(tf.fileno())
                except Exception:
                    pass
            os.replace(tmp, path)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
    except Exception:
        # propagate to caller so they can handle/log
        raise


def substitute_in_value(val, ctx):
    if not isinstance(val, str):
        return val

    def repl(m):
        key = m.group(1)
        parts = key.split('.')
        v = ctx
        for p in parts:
            if isinstance(v, dict) and p in v:
                v = v[p]
            else:
                return ''
        return str(v) if v is not None else ''

    return PLACEHOLDER_RE.sub(repl, val)


def substitute_step(step: dict, ctx: dict):
    # shallow recursive substitution for common types
    out = {}
    for k, v in step.items():
        if isinstance(v, str):
            out[k] = substitute_in_value(v, ctx)
        elif isinstance(v, dict):
            sub = {}
            for kk, vv in v.items():
                sub[kk] = substitute_in_value(vv, ctx) if isinstance(vv, str) else vv
            out[k] = sub
        elif isinstance(v, list):
            out[k] = [substitute_in_value(x, ctx) if isinstance(x, str) else x for x in v]
        else:
            out[k] = v
    return out


__all__ = [
    'ROOT', 'STEPS_DIR', 'RUNS_DIR', 'DATA_DIR', 'RUNNER_SCRIPT', 'KEYWORDS_DIR',
    'step_path', '_sanitize_name', 'load_dataset_file', 'substitute_in_value', 'substitute_step'
        , 'OBJECTS_DIR'
]
