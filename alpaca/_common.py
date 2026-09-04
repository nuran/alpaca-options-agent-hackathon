"""
Shared request/host/formatting helpers for the tools/alpaca/ scripts.

Vendored from NuKa's tools/alpaca/_common.py so this repository stands alone.
Re-sync instructions are in this repo's CLAUDE.md.

Every script defaults to the paper trading host. Reaching the live host requires an
explicit --live flag, which is checked in one place: trading_host().
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(__file__))
from env import load_env_var

import requests

PAPER_HOST = "https://paper-api.alpaca.markets"
LIVE_HOST = "https://api.alpaca.markets"
DATA_HOST = "https://data.alpaca.markets"

TIMEOUT = 30


def auth_headers():
    """The two headers every Alpaca REST call needs."""
    return {
        "APCA-API-KEY-ID": load_env_var('ALPACA_API_KEY'),
        "APCA-API-SECRET-KEY": load_env_var('ALPACA_SECRET_KEY'),
        "accept": "application/json",
    }


def trading_host(live=False):
    """Paper unless explicitly told otherwise."""
    return LIVE_HOST if live else PAPER_HOST


def request(method, url, params=None, payload=None):
    """Make one Alpaca REST call and return the decoded body, or exit with a clear error."""
    headers = auth_headers()
    if payload is not None:
        headers["content-type"] = "application/json"

    try:
        response = requests.request(
            method, url, headers=headers, params=params, json=payload, timeout=TIMEOUT
        )
    except Exception as e:
        print(f"ERROR: request failed: {e}")
        sys.exit(1)

    if response.status_code == 401:
        print("ERROR (401): Invalid ALPACA_API_KEY / ALPACA_SECRET_KEY, or paper keys "
              "were sent to the live host (or vice versa).")
        print(response.text)
        sys.exit(1)
    elif response.status_code == 403:
        print("ERROR (403): Forbidden -- usually insufficient buying power, an options "
              "level too low for this order, or a blocked account. Check "
              "`python3 tools/alpaca/account.py`.")
        print(response.text)
        sys.exit(1)
    elif response.status_code == 404:
        print(f"ERROR (404): Not found -- {url.rsplit('/', 1)[-1]} may be untradable, "
              f"already closed, or misspelled.")
        print(response.text)
        sys.exit(1)
    elif response.status_code == 422:
        print("ERROR (422): Unprocessable -- malformed order parameters. Common causes: "
              "a bad OCC option symbol, time_in_force other than 'day' on an option, or "
              "notional combined with a non-market order type.")
        print(response.text)
        sys.exit(1)
    elif response.status_code == 429:
        reset = response.headers.get("X-RateLimit-Reset", "unknown")
        print(f"ERROR (429): Rate limited (200 req/min). Quota resets at epoch {reset}.")
        sys.exit(1)
    elif not response.ok:
        print(f"ERROR ({response.status_code}): {response.text}")
        sys.exit(1)

    if not response.text.strip():
        return {}
    return response.json()


def paginate(url, params, key, limit=None):
    """
    Follow Alpaca's next_page_token until exhausted, collecting `key` from each page.

    Handles both response shapes: a bare list under `key`, and a dict-of-lists keyed
    by symbol (which the multi-symbol market data endpoints return).
    """
    params = dict(params)
    collected = None
    while True:
        body = request("GET", url, params=params)
        page = body.get(key)

        if isinstance(page, dict):
            if collected is None:
                collected = {}
            for symbol, rows in page.items():
                collected.setdefault(symbol, []).extend(rows)
            total = sum(len(v) for v in collected.values())
        else:
            if collected is None:
                collected = []
            collected.extend(page or [])
            total = len(collected)

        token = body.get("next_page_token")
        if not token or (limit is not None and total >= limit):
            break
        params["page_token"] = token

    return collected if collected is not None else []


def take_flag(args, flag):
    """Remove a boolean flag from args, returning whether it was present."""
    if flag in args:
        args.remove(flag)
        return True
    return False


def take_value(args, flag, default=None, cast=str):
    """Remove `--flag value` from args and return the cast value, or default."""
    if flag not in args:
        return default
    idx = args.index(flag)
    try:
        raw = args[idx + 1]
    except IndexError:
        print(f"ERROR: {flag} requires a value")
        sys.exit(1)
    if raw.startswith('--'):
        print(f"ERROR: {flag} requires a value")
        sys.exit(1)
    try:
        value = cast(raw)
    except ValueError:
        print(f"ERROR: {flag} requires a {cast.__name__} value, got '{raw}'")
        sys.exit(1)
    del args[idx:idx + 2]
    return value


def take_all_values(args, flag):
    """Remove every occurrence of `--flag value` and return the values in order."""
    values = []
    while flag in args:
        idx = args.index(flag)
        try:
            values.append(args[idx + 1])
        except IndexError:
            print(f"ERROR: {flag} requires a value")
            sys.exit(1)
        del args[idx:idx + 2]
    return values


def reject_unknown(args, usage):
    """Any leftover --flag is a typo; fail loudly rather than silently ignoring it."""
    for arg in args:
        if arg.startswith('--'):
            print(f"ERROR: unrecognized argument: {arg}")
            print(usage)
            sys.exit(1)


def emit(result, as_json):
    """Print raw JSON and exit, when --json was passed. Otherwise return."""
    if as_json:
        print(json.dumps(result, indent=2))
        sys.exit(0)


def money(value, width=14):
    """Right-aligned currency, tolerant of None and string-typed numbers."""
    if value is None or value == "":
        return "-".rjust(width)
    try:
        return f"${float(value):,.2f}".rjust(width)
    except (TypeError, ValueError):
        return str(value).rjust(width)


def pct(value, width=8):
    """Alpaca returns percentages as decimal fractions (0.0123 == 1.23%)."""
    if value is None or value == "":
        return "-".rjust(width)
    try:
        return f"{float(value) * 100:+.2f}%".rjust(width)
    except (TypeError, ValueError):
        return str(value).rjust(width)
