#!/usr/bin/env python3
"""Mint an Auth0 access token with the client-credentials (M2M) flow.

    python remote/auth0_token.py            # print the token
    python remote/auth0_token.py --decode   # print the token's claims too

This is the Python form of the curl command the API provider gave you: it POSTs
{client_id, client_secret, audience, grant_type} to the tenant's /oauth/token
and hands back the access_token.

Deliberately does NOT import atriumdb, so it runs anywhere - including macOS,
where the SDK refuses to load. Its only job is to answer "are my credentials
good?"; whether the data path works is test_connection.py's question.

Values come from the environment, or from remote/.env if python-dotenv is
installed. Never hard-code the secret in this file.
"""
import argparse
import base64
import json
import os
import sys
from pathlib import Path

import requests

# Auth0 issues no refresh token for client_credentials, so a token is simply
# re-minted when it expires. 24h is the usual lifetime.
TOKEN_PATH = "/oauth/token"

REQUIRED_VARS = ("AUTH0_TENANT", "AUTH0_CLIENT_ID", "AUTH0_CLIENT_SECRET", "AUTH0_AUDIENCE")


def _load_dotenv():
    """Load remote/.env if python-dotenv is available, else do nothing.

    Optional on purpose: the script must still work in a container where the
    values arrive as real environment variables and no .env file exists.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=False)


def _read_config() -> dict:
    """Return the Auth0 settings from the environment, or exit with a clear error."""
    _load_dotenv()

    missing = [name for name in REQUIRED_VARS if not os.environ.get(name)]
    if missing:
        sys.exit(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + "\nCopy remote/.env.example to remote/.env and fill it in."
        )

    return {
        "tenant": os.environ["AUTH0_TENANT"].strip().rstrip("/"),
        "client_id": os.environ["AUTH0_CLIENT_ID"],
        "client_secret": os.environ["AUTH0_CLIENT_SECRET"],
        "audience": os.environ["AUTH0_AUDIENCE"],
        # Supplied by the provider; kept configurable rather than hard-coded so a
        # different grant can be tried without editing the script.
        "grant_type": os.environ.get("AUTH0_GRANT_TYPE", "client_credentials"),
    }


def get_token() -> str:
    """Mint and return an access token.

    Imported by test_connection.py, so the token logic lives in exactly one
    place.

    :return: The raw access_token string.
    :rtype: str
    """
    cfg = _read_config()

    # The tenant may be given bare ("example.us.auth0.com") or with a scheme;
    # normalise so both work.
    tenant = cfg["tenant"]
    if not tenant.startswith(("http://", "https://")):
        tenant = f"https://{tenant}"

    response = requests.post(
        f"{tenant}{TOKEN_PATH}",
        headers={"content-type": "application/json"},
        json={
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "audience": cfg["audience"],
            "grant_type": cfg["grant_type"],
        },
        timeout=30,
    )

    if response.status_code != 200:
        # Auth0 puts the useful part in the body, not the status line.
        sys.exit(
            f"Auth0 rejected the request (HTTP {response.status_code}).\n"
            f"{response.text}\n\n"
            "Common causes:\n"
            "  access_denied      - the client is not authorised for this audience\n"
            "  unauthorized_client- the grant type is not enabled on the application\n"
            "  invalid_client     - wrong client_id or client_secret"
        )

    payload = response.json()
    if "access_token" not in payload:
        sys.exit(f"Auth0 returned 200 but no access_token:\n{payload}")

    return payload["access_token"]


def decode_claims(token: str) -> dict:
    """Return a JWT's payload claims without verifying the signature.

    Verification is the API server's job - it checks the signature against the
    tenant's JWKS. This is only so you can eyeball 'aud' and 'exp', which is
    where client-credentials setups usually go wrong.

    :param token: The raw JWT.
    :return: The decoded payload claims.
    :rtype: dict
    """
    try:
        payload_segment = token.split(".")[1]
    except IndexError:
        return {}

    # JWT uses base64url with the padding stripped; put it back before decoding.
    padded = payload_segment + "=" * (-len(payload_segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--decode",
        action="store_true",
        help="also print the token's claims (aud, exp, iss, scope)",
    )
    args = parser.parse_args()

    token = get_token()
    print(token)

    if args.decode:
        claims = decode_claims(token)
        print("\n--- claims ---", file=sys.stderr)
        for key in ("iss", "aud", "sub", "exp", "iat", "scope", "gty"):
            if key in claims:
                print(f"{key:>6}: {claims[key]}", file=sys.stderr)

        exp = claims.get("exp")
        if exp:
            import datetime

            expires = datetime.datetime.fromtimestamp(exp, datetime.timezone.utc)
            print(f"\nexpires: {expires:%Y-%m-%d %H:%M:%S} UTC", file=sys.stderr)


if __name__ == "__main__":
    main()
