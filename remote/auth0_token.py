#!/usr/bin/env python3
"""Mint an Auth0 access token with the client-credentials (M2M) flow.

    python3 remote/auth0_token.py            # print the token
    python3 remote/auth0_token.py --decode   # print the token's claims too

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
from urllib.parse import urlsplit, urlunsplit

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
    dotenv_path = Path(__file__).with_name(".env")

    try:
        from dotenv import load_dotenv
    except ImportError:
        # Silence here would be confusing: values sitting in remote/.env would
        # simply be ignored and every variable would look unset.
        if dotenv_path.exists():
            print(
                f"warning: {dotenv_path.name} exists but python-dotenv is not "
                "installed, so it cannot be read.\n"
                "         pip3 install python-dotenv   (or export the variables)",
                file=sys.stderr,
            )
        return

    load_dotenv(dotenv_path=dotenv_path, override=False)


def _read_config() -> dict:
    """Return the Auth0 settings from the environment, or exit with a clear error."""
    _load_dotenv()

    missing = [name for name in REQUIRED_VARS if not os.environ.get(name)]
    if missing:
        dotenv_path = Path(__file__).with_name(".env")
        # Naming the file and whether it exists turns the two very different
        # causes - "no config file" and "config file missing a key" - into
        # distinguishable errors.
        hint = (
            f"{dotenv_path} does not exist.\n"
            "  cp remote/.env.example remote/.env   then fill it in"
            if not dotenv_path.exists()
            else f"Set them in {dotenv_path}."
        )
        sys.exit(
            "Missing required environment variable(s): " + ", ".join(missing) + "\n" + hint
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


def _normalize_tenant(tenant: str) -> str:
    """Return the tenant as an https origin, with any path stripped.

    Auth0 has required TLS since 2024-10-07 and answers plaintext requests with
    HTTP 426, so an http:// value is upgraded rather than passed through - there
    is no case where talking to Auth0 unencrypted is correct.

    Also drops any path, so pasting a full token URL
    ("https://tenant.auth0.com/oauth/token") does not produce a doubled path.

    :param tenant: Tenant as configured - bare host, or with a scheme.
    :return: An origin of the form "https://host".
    :rtype: str
    """
    tenant = tenant.strip()

    if tenant.startswith("http://"):
        print(
            "warning: AUTH0_TENANT starts with http:// - using https:// instead "
            "(Auth0 rejects plaintext with HTTP 426)",
            file=sys.stderr,
        )
        tenant = "https://" + tenant[len("http://"):]
    elif not tenant.startswith("https://"):
        tenant = f"https://{tenant}"

    parts = urlsplit(tenant)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _explain_status(status_code: int) -> str:
    """Return guidance matching the HTTP status Auth0 returned.

    Status-specific on purpose: printing the OAuth error codes for a transport
    failure like 426 sends you looking at the wrong thing entirely.

    :param status_code: The HTTP status from the token request.
    :return: A human-readable explanation.
    :rtype: str
    """
    if status_code == 426:
        return (
            "HTTP 426 is a transport problem, not a credentials problem.\n"
            "Auth0 has required TLS since 2024-10-07 and rejects plaintext http://.\n"
            "  - check AUTH0_TENANT in remote/.env: it must be a bare host\n"
            "    (tenant.us.auth0.com) or start with https://, never http://\n"
            "  - an http_proxy/HTTPS_PROXY that downgrades the connection\n"
            "    will also cause this"
        )
    if status_code in (401, 403):
        return (
            "Common causes:\n"
            "  access_denied       - the client is not authorised for this audience\n"
            "  unauthorized_client - the grant type is not enabled on the application\n"
            "  invalid_client      - wrong client_id or client_secret"
        )
    if status_code == 400:
        return (
            "Auth0 rejected the request body. Check that grant_type is\n"
            "'client_credentials' and that the audience is spelled exactly as given."
        )
    if status_code == 404:
        return (
            "No /oauth/token at that host - AUTH0_TENANT is probably wrong.\n"
            "It is the Auth0 tenant domain, not the API you are calling."
        )
    if status_code == 429:
        return "Rate limited by Auth0. Reuse one token per process rather than minting per call."
    return "See the response body above for what Auth0 objected to."


def get_token() -> str:
    """Mint and return an access token.

    Imported by test_connection.py, so the token logic lives in exactly one
    place.

    :return: The raw access_token string.
    :rtype: str
    """
    cfg = _read_config()

    tenant = _normalize_tenant(cfg["tenant"])

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
            + _explain_status(response.status_code)
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
