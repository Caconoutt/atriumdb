#!/usr/bin/env python3
"""Find the AtriumDB API base URL by probing candidates derived from the audience.

    python remote/discover_api_url.py                    # start from AUTH0_AUDIENCE
    python remote/discover_api_url.py https://host/api   # or an explicit guess

An Auth0 audience is an identifier, not necessarily a reachable URL, so the
audience is only a starting point. This tries the usual path prefixes against
the endpoints an AtriumDB server is known to serve and reports what answers.

Read-only: every probe is a GET to a documented endpoint. Nothing is written.

Does not import the SDK, so it runs on macOS.
"""
import json
import os
import sys
from urllib.parse import urlsplit, urlunsplit

import requests

# Tried in order, appended to the origin of whatever base you start from. The
# empty string means "the origin itself".
CANDIDATE_PREFIXES = ("", "/api", "/api/v1", "/v1", "/atriumdb")

# Endpoints that identify an AtriumDB API server. /openapi.json is the most
# informative - it lists every route the app serves.
PROBES = ("/openapi.json", "/docs", "/auth/cli/code", "/measures/")

# Routes that only an AtriumDB API server would expose.
ATRIUMDB_MARKERS = ("/sdk/blocks", "/measures/", "/patients/", "/devices/")

TIMEOUT = 15


def _candidates(start: str) -> list:
    """Return base URLs to try: the value as given, then origin + each prefix."""
    parts = urlsplit(start if "://" in start else f"https://{start}")
    origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    seen, out = set(), []
    for candidate in [start.rstrip("/")] + [f"{origin}{p}" for p in CANDIDATE_PREFIXES]:
        candidate = candidate.rstrip("/")
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _get(url: str, token: str | None):
    """GET a URL, returning the response or None if it could not be reached."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        return requests.get(url, headers=headers, timeout=TIMEOUT)
    except requests.RequestException:
        return None


def _routes_from_openapi(response) -> list:
    """Return the path list from an OpenAPI document, or [] if it is not one."""
    try:
        return sorted(response.json().get("paths", {}))
    except (json.JSONDecodeError, ValueError, AttributeError):
        return []


def main():
    token = os.environ.get("ATRIUMDB_API_TOKEN", "").strip() or None
    if not token:
        # A token is optional - an unauthenticated 401 still proves a route
        # exists - but with one we learn more.
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from auth0_token import get_token

            token = get_token()
            print("minted a token from Auth0\n")
        except SystemExit:
            print("continuing without a token - 401s still reveal live routes\n")
            token = None

    start = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AUTH0_AUDIENCE", "").strip()
    if not start:
        sys.exit(
            "Nothing to probe. Pass a URL, or set AUTH0_AUDIENCE in remote/.env:\n"
            "  python remote/discover_api_url.py https://<host>/api"
        )

    print(f"starting from: {start}\n")
    found = []

    for base in _candidates(start):
        print(f"=== {base}")
        for path in PROBES:
            response = _get(f"{base}{path}", token)
            if response is None:
                print(f"  {path:<18} unreachable")
                continue

            note = ""
            if path == "/openapi.json" and response.status_code == 200:
                routes = _routes_from_openapi(response)
                if routes:
                    hits = [m for m in ATRIUMDB_MARKERS if any(r.startswith(m) for r in routes)]
                    note = f"  <- {len(routes)} routes"
                    if hits:
                        note += f", AtriumDB markers: {', '.join(hits)}"
                        found.append((base, routes))

            # 401/403 is a positive signal: the route exists and is protected.
            if response.status_code in (401, 403):
                note = note or "  <- exists, auth required"

            print(f"  {path:<18} {response.status_code}{note}")
        print()

    if found:
        base, routes = found[0]
        print("=" * 60)
        print(f"AtriumDB API found at: {base}")
        print("=" * 60)
        print("\nSet this in remote/.env:\n")
        print(f"  ATRIUMDB_API_URL={base}\n")
        print("routes served:")
        for route in routes:
            print(f"  {route}")
        return

    print("No AtriumDB API identified.")
    print("\nA 401 above means a route exists but the token was rejected or absent -")
    print("that base URL is still worth trying. If everything 404s, the base URL")
    print("is not derivable from the audience and the provider has to supply it.")


if __name__ == "__main__":
    main()
