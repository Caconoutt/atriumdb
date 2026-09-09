#!/usr/bin/env python3
"""Prove the remote AtriumDB connection works, using measures only.

    python3 remote/test_connection.py          # through the SDK
    python3 remote/test_connection.py --raw    # plain HTTP, no SDK needed

Lists the signal types the dataset holds. Read-only and cheap - it touches no
patient data and pulls no waveform blocks, so it is the smallest thing that
proves auth, routing and the API are all working.

Two modes, hitting the same endpoint:

  default  builds an AtriumSDK in "api" mode and calls get_all_measures(),
           which the SDK serves from `GET {api_url}/measures/`. This is the
           path real code takes, so it also proves the SDK is installed and
           libTSC.so loads.

  --raw    issues that same GET with requests and parses the JSON. Needs
           neither the atriumdb package nor libTSC.so, so it runs on macOS and
           on any machine where the C library has not been built. Use it to
           separate "is the connection good?" from "is my install good?".

The token comes from ATRIUMDB_API_TOKEN if set, otherwise it is minted by
auth0_token.get_token(). Setting the variable lets you iterate without hitting
Auth0 on every run:

    export ATRIUMDB_API_TOKEN=$(python3 remote/auth0_token.py)
"""
import argparse
import os
import sys

import requests

# Imported before atriumdb so a missing credential fails fast, on any platform,
# rather than after the macOS guard has already stopped the script.
from auth0_token import _load_dotenv, decode_claims, get_token


def _resolve_api_url() -> str:
    """Return the API base URL, or exit with a clear error.

    This is NOT the Auth0 audience. The audience is an identifier Auth0 stamps
    into the token; the base URL is where the AtriumDB API actually listens.
    They are often different, and the provider has to tell you this one.
    """
    api_url = os.environ.get("ATRIUMDB_API_URL", "").strip()
    if not api_url:
        sys.exit(
            "ATRIUMDB_API_URL is not set - the base URL of the AtriumDB API "
            "(not the Auth0 audience).\nSet it in remote/.env, e.g. "
            "ATRIUMDB_API_URL=https://<host>/api/v1"
        )
    return api_url.rstrip("/")


def _resolve_token() -> str:
    """Return a bearer token, minting one only if the environment has none."""
    token = os.environ.get("ATRIUMDB_API_TOKEN", "").strip()
    if token:
        print("using ATRIUMDB_API_TOKEN from the environment")
        return token

    print("minting a token from Auth0 ...")
    return get_token()


def _explain(error: Exception, api_url: str) -> str:
    """Turn an SDK request failure into the likely cause.

    ``AtriumSDK._request`` raises a bare ValueError carrying the status code,
    so the status is recovered from the message text.
    """
    text = str(error)

    if "401" in text or "403" in text:
        return (
            "The API rejected the token.\n"
            "  - the token's 'aud' claim must match what the server expects "
            "(check with: python3 remote/auth0_token.py --decode)\n"
            "  - the token may have expired; mint a fresh one\n"
            "  - the client may not be authorised for this API in Auth0"
        )
    if "404" in text:
        return (
            f"No /measures/ endpoint at {api_url}.\n"
            "  - the base URL may need a path prefix, e.g. /api or /api/v1\n"
            "  - or this host is not an AtriumDB API server\n"
            f"  - try opening {api_url}/docs in a browser to see what it serves"
        )
    return "Unexpected failure - the full error is above."


def _fetch_measures_raw(api_url: str, token: str) -> dict:
    """Fetch measures with a plain GET, exactly as the SDK would.

    Mirrors ``AtriumSDK._request("GET", "measures/")``: same URL, same bearer
    header. The SDK converts the JSON object's string keys to ints, so this
    does too, keeping the output identical between the two modes.

    :param api_url: API base URL, no trailing slash.
    :param token: Bearer token.
    :return: measure_id -> measure info.
    :rtype: dict
    """
    url = f"{api_url}/measures/"
    print(f"GET {url}")

    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
    except requests.RequestException as exc:
        # A wrong host, a closed port or a blocked egress path is a plausible
        # failure here, and the raw urllib3 traceback buries the one useful line.
        sys.exit(
            f"Could not reach {url}\n  {type(exc).__name__}: {exc}\n\n"
            "  - check ATRIUMDB_API_URL, including scheme and any path prefix\n"
            "  - check this machine can reach the host (firewall, VPN, proxy)"
        )

    if response.status_code != 200:
        print(f"\nHTTP {response.status_code}\n{response.text[:500]}\n", file=sys.stderr)
        # Reuse the SDK-mode explanation by handing it a message of the same
        # shape, so both modes give identical guidance for identical failures.
        sys.exit(_explain(ValueError(f"status code {response.status_code}"), api_url))

    return {int(measure_id): info for measure_id, info in response.json().items()}


def _fetch_measures_sdk(api_url: str, token: str) -> dict:
    """Fetch measures through an AtriumSDK in api mode."""
    try:
        from atriumdb import AtriumSDK
    except ModuleNotFoundError:
        sys.exit(
            "The atriumdb package is not installed in this environment.\n"
            "  pip3 install -e \"./sdk[remote]\"\n\n"
            "Or skip the SDK entirely for this check:\n"
            "  python3 remote/test_connection.py --raw"
        )
    except OSError as exc:
        # The macOS guard in AtriumSDK.__init__ fires at import time.
        sys.exit(
            f"{exc}\n\nThe SDK does not run on macOS. Either run this on the "
            "server / in the container, or use:\n"
            "  python3 remote/test_connection.py --raw"
        )

    # validate_token=False skips both the GET /auth/cli/code round trip and the
    # local JWT check. That is deliberate for a first run: it keeps this test
    # about the data path, so a failure here cannot be blamed on token
    # validation. Wire up validation once the connection is proven.
    sdk = AtriumSDK(
        metadata_connection_type="api",
        api_url=api_url,
        token=token,
        validate_token=False,
    )

    try:
        return sdk.get_all_measures()
    except Exception as exc:
        print(f"\nrequest failed: {exc}\n", file=sys.stderr)
        sys.exit(_explain(exc, api_url))


def _print_measures(measures: dict):
    """Print the measures as a table, or explain an empty result."""
    print(f"connected - {len(measures)} measure(s) available\n")

    if not measures:
        print("The API returned no measures. The connection works, but the "
              "dataset behind it is empty or this client cannot see it.")
        return

    header = f"{'id':>5}  {'tag':<32} {'freq (nHz)':>16}  units"
    print(header)
    print("-" * len(header))
    for measure_id in sorted(measures):
        info = measures[measure_id]
        print(
            f"{measure_id:>5}  {str(info.get('tag', '')):<32} "
            f"{info.get('freq_nhz', ''):>16}  {info.get('unit', '')}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--raw",
        action="store_true",
        help="call the API with plain HTTP instead of the SDK "
             "(no atriumdb package or libTSC.so required)",
    )
    args = parser.parse_args()

    # Must happen before anything reads os.environ: get_token() loads remote/.env
    # itself, but the API URL is resolved first and would otherwise be looked up
    # against an environment the file has not been merged into yet.
    _load_dotenv()

    api_url = _resolve_api_url()
    token = _resolve_token()

    claims = decode_claims(token)
    print(f"token audience : {claims.get('aud')}")
    print(f"api url        : {api_url}")
    print(f"mode           : {'raw HTTP' if args.raw else 'AtriumSDK (api mode)'}")

    print("\nconnecting ...")
    measures = (
        _fetch_measures_raw(api_url, token)
        if args.raw
        else _fetch_measures_sdk(api_url, token)
    )

    _print_measures(measures)


if __name__ == "__main__":
    main()
