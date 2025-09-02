#!/usr/bin/env python3
"""Cross-platform runner to collect testcase ids from a suite and trigger suite run.
Usage:
  python3 scripts/run_suite.py HOST SUITE_ID [--concurrency N] [--browser chrome] [--dry-run]
Examples:
  python3 scripts/run_suite.py http://localhost:8000 41335e28-24ea-4b53-866c-99320415e283 --concurrency 4 --browser chrome
  py scripts\run_suite.py http://localhost:8000 41335... --concurrency 2 --browser firefox

This script uses only the Python standard library so it works on Windows, macOS and Linux.
"""
from __future__ import annotations
import argparse
import sys
import json
from urllib import request, parse, error
from typing import List


def norm_host(host: str) -> str:
    if not host:
        raise ValueError('host required')
    if not host.startswith('http://') and not host.startswith('https://'):
        host = 'http://' + host
    return host.rstrip('/')


def get_testcase_ids(host: str, suite_id: str, timeout: int = 10) -> List[str]:
    url = f"{host}/suites/{parse.quote(suite_id)}/testcases"
    req = request.Request(url, method='GET')
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8')
    except error.HTTPError as e:
        raise RuntimeError(f'GET {url} failed: {e.code} {e.reason} - {e.read().decode("utf-8", errors="ignore")}')
    except Exception as e:
        raise RuntimeError(f'GET {url} failed: {e}')

    try:
        j = json.loads(body)
    except Exception as e:
        raise RuntimeError(f'Invalid JSON from {url}: {e}\nBody: {body}')

    if not j.get('ok'):
        raise RuntimeError(f'Server returned error from {url}: {j}')

    tcs = j.get('testcases') or []
    ids = []
    for t in tcs:
        tid = t.get('id') if isinstance(t, dict) else None
        if tid:
            ids.append(str(tid))
    return ids


def trigger_suite_run(host: str, suite_id: str, tc_ids: List[str], concurrency: int = 1, browser: str = 'chrome', timeout: int = 30) -> str:
    url = f"{host}/suites/{parse.quote(suite_id)}/run"
    # form encode with doseq so repeated tc_ids are sent
    data = {
        'tc_ids': tc_ids,
        'concurrency': str(concurrency),
        'browser': browser,
    }
    body = parse.urlencode(data, doseq=True).encode('utf-8')
    req = request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode('utf-8')
    except error.HTTPError as e:
        # include server body for debugging
        msg = e.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f'POST {url} failed: {e.code} {e.reason} - {msg}')
    except Exception as e:
        raise RuntimeError(f'POST {url} failed: {e}')


def main(argv=None):
    p = argparse.ArgumentParser(description='Trigger suite run by collecting testcase ids from suite')
    p.add_argument('host', help='Server host (http://host:port or host:port)')
    p.add_argument('suite_id', help='Suite id')
    p.add_argument('--concurrency', '-c', type=int, default=1, help='Concurrency for run (default 1)')
    p.add_argument('--browser', '-b', default='chrome', help='Browser name (default chrome)')
    p.add_argument('--dry-run', action='store_true', help='Only print payload, do not POST')
    args = p.parse_args(argv)

    try:
        host = norm_host(args.host)
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        return 2

    try:
        ids = get_testcase_ids(host, args.suite_id)
    except Exception as e:
        print(f'Error fetching testcases: {e}', file=sys.stderr)
        return 3

    if not ids:
        print(f'No testcases found for suite {args.suite_id}', file=sys.stderr)
        return 4

    print(f'Found {len(ids)} testcases for suite {args.suite_id}')

    if args.dry_run:
        print('Dry run - payload:')
        print(json.dumps({'tc_ids': ids, 'concurrency': args.concurrency, 'browser': args.browser}, indent=2))
        return 0

    try:
        resp = trigger_suite_run(host, args.suite_id, ids, concurrency=args.concurrency, browser=args.browser)
        print('Run triggered. Server response:')
        print(resp)
    except Exception as e:
        print(f'Error triggering run: {e}', file=sys.stderr)
        return 5

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
