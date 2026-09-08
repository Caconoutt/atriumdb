#!/usr/bin/env python3
"""Prove the remote AtriumDB connection works, using measures only.

    python remote/test_connection.py

Builds an AtriumSDK in "api" mode against the remote server and calls
get_all_measures(), which the SDK serves from `GET {api_url}/measures/`.
Read-only and cheap - it lists the signal types the dataset holds and touches
no patient data and no waveform blocks.

The token comes from ATRIUMDB_API_TOKEN if set, otherwise it is minted by
auth0_token.get_token(). Setting the variable lets you iterate without hitting
Auth0 on every run:

    export ATRIUMDB_API_TOKEN=$(python remote/auth0_token.py)

Must run inside the container: AtriumSDK.__init__ raises OSError on macOS
before it looks at the connection type, so even a pure-remote client cannot
import it there.
"""
import os
import sys

# Imported before atriumdb so a missing credential fails fast, on any platform,
# rather than after the macOS guard has already stopped the script.
from auth0_token import decode_claims, get_token


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
            "(check with: python remote/auth0_token.py --decode)\n"
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


def main():
    api_url = _resolve_api_url()
    token = _resolve_token()

    claims = decode_claims(token)
    print(f"token audience : {claims.get('aud')}")
    print(f"api url        : {api_url}")

    # Imported here, after the credential checks, so the macOS guard is not the
    # first thing you hit while still sorting out configuration.
    try:
        from atriumdb import AtriumSDK
    except OSError as exc:
        sys.exit(
            f"{exc}\n\nThe SDK does not run on macOS. Run this script inside "
            "the container, where libTSC.so is built for Linux."
        )

    # validate_token=False skips both the GET /auth/cli/code round trip and the
    # local JWT check. That is deliberate for a first run: it keeps this test
    # about the data path, so a failure here cannot be blamed on token
    # validation. Wire up validation once the connection is proven.
    print("\nconnecting ...")
    sdk = AtriumSDK(
        metadata_connection_type="api",
        api_url=api_url,
        token=token,
        validate_token=False,
    )

    try:
        measures = sdk.get_all_measures()
    except Exception as exc:
        print(f"\nrequest failed: {exc}\n", file=sys.stderr)
        sys.exit(_explain(exc, api_url))

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


if __name__ == "__main__":
    main()
