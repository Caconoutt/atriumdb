# AtriumDB is a timeseries database software designed to best handle the unique features and
# challenges that arise from clinical waveform data.
#     Copyright (C) 2023  The Hospital for Sick Children
#
#     This program is free software: you can redistribute it and/or modify
#     it under the terms of the GNU General Public License as published by
#     the Free Software Foundation, either version 3 of the License, or
#     (at your option) any later version.
#
#     This program is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU General Public License for more details.
#
#     You should have received a copy of the GNU General Public License
#     along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Auth0 machine-to-machine tokens for the API-mode SDK.

To *mint* a token is to ask Auth0 for a new one: a single POST carrying the
client id and secret, answered with a signed string that is good for a fixed
period. The token then stands in for the secret on every request to the AtriumDB
API::

    1.  us -> Auth0             client_id + client_secret
            <- access_token (signed, ~24h)

    2.  us -> AtriumDB API      Authorization: Bearer <access_token>
            <- data

The secret only ever goes to Auth0; the API verifies the signature against
Auth0's public keys and never sees it. So this module's whole job is: hold one
valid token in memory, and make sure the SDK is carrying it.

The server-side counterpart of ``remote/auth0_token.py``, which is a CLI script:
that one reads a ``.env``, prints to stdout and ``sys.exit``s on bad config,
which is right for answering "are my credentials good?" by hand and wrong for a
process that must keep running.

Three things make this more than a copy of that script.

**Client credentials issues no refresh token.** ``AtriumSDK._refresh_token``
posts ``grant_type=refresh_token``, which Auth0 rejects for an M2M client. So the
SDK is built with ``validate_token=False``, which means ``_request`` never
refreshes and simply uses whatever is in ``sdk.token``. Re-minting is entirely
this module's job.

**One token per process, not one per call.** Auth0 meters M2M token requests.
The token is held in memory for its lifetime and re-minted only near expiry;
it is never written to disk.

**The websocket caches the token it connected with.** ``_websocket_connect`` sets
the ``Authorization`` header once and the SDK keeps that connection open for the
life of the object, so updating ``sdk.token`` alone would leave ``get_data``
transferring over a socket authenticated with the expired one.
:func:`apply_token` closes the websocket when the token changes, exactly as
``_refresh_token`` does, and the next call reconnects.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

import requests

if TYPE_CHECKING:
    from atriumdb import AtriumSDK

    from atriumdb_dashboard.config import DataConfig

_LOGGER = logging.getLogger(__name__)

__all__ = ["Auth0TokenProvider", "AuthError"]

#: Path Auth0 serves the token endpoint at, on every tenant.
_TOKEN_PATH = "/oauth/token"

#: Re-mint when the token has less than this long to live. Generous compared
#: with the SDK's own 30s margin because a single request can run for minutes
#: over a slow link, and a token that expires mid-request fails the websocket
#: transfer rather than merely the next HTTP call.
_REFRESH_MARGIN_S = 300

#: Assumed lifetime when Auth0's response omits ``expires_in``. Deliberately
#: short: under-estimating costs one extra mint, over-estimating costs a failed
#: request.
_FALLBACK_LIFETIME_S = 3600

#: Auth0 has required TLS since 2024-10-07 and answers plaintext with HTTP 426.
_TOKEN_REQUEST_TIMEOUT_S = 30


class AuthError(RuntimeError):
    """Raised when a token cannot be obtained.

    Carries Auth0's own error body, which is where the useful detail lives — the
    status line alone rarely distinguishes ``access_denied`` from
    ``invalid_client``. Never carries the client secret.
    """


def _normalize_tenant(tenant: str) -> str:
    """Return the tenant as an ``https`` origin with any path stripped.

    Upgrades ``http://`` rather than passing it through: there is no case where
    talking to Auth0 unencrypted is correct, and the resulting HTTP 426 is an
    obscure way to discover a one-character configuration error. Dropping the
    path means pasting a full token URL does not produce a doubled ``/oauth/token``.
    """
    tenant = tenant.strip()

    if tenant.startswith("http://"):
        _LOGGER.warning(
            "%s starts with http://; using https:// instead (Auth0 rejects "
            "plaintext with HTTP 426).",
            "AUTH0_TENANT",
        )
        tenant = "https://" + tenant[len("http://"):]
    elif not tenant.startswith("https://"):
        tenant = f"https://{tenant}"

    parts = urlsplit(tenant)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _explain_status(status_code: int) -> str:
    """Return guidance matching the HTTP status Auth0 returned.

    Status-specific because printing the OAuth error codes for a transport
    failure like 426 sends the reader looking at entirely the wrong thing.
    """
    if status_code == 426:
        return (
            "HTTP 426 is a transport problem, not a credentials problem: Auth0 "
            "rejects plaintext http://. Check AUTH0_TENANT, and any proxy that "
            "might downgrade the connection."
        )
    if status_code in (401, 403):
        return (
            "access_denied: the client is not authorised for this audience. "
            "unauthorized_client: client_credentials is not enabled on the "
            "application. invalid_client: wrong client_id or client_secret."
        )
    if status_code == 400:
        return (
            "Auth0 rejected the request body. Check that the grant type is "
            "client_credentials and that the audience is spelled exactly as given."
        )
    if status_code == 404:
        return (
            "No /oauth/token at that host — AUTH0_TENANT is probably wrong. It is "
            "the Auth0 tenant domain, not the API being called."
        )
    if status_code == 429:
        return (
            "Rate limited by Auth0. Tokens must be held for their lifetime, not "
            "minted per call."
        )
    return "See the response body above for what Auth0 objected to."


def _token_expiry(token: str, issued_at: float, expires_in: int | None) -> float:
    """Return the epoch second at which this token stops being usable.

    Three sources, tried in this order:

    1. **The ``exp`` claim inside the JWT** — an absolute epoch second. Preferred
       because ``exp`` is the value the API server itself validates against, so
       trusting it closes the clock-skew window in which this process would still
       consider fresh a token the server has already rejected.
    2. **``issued_at + expires_in``** from Auth0's JSON response, for an opaque
       token carrying no readable claim.
    3. **:data:`_FALLBACK_LIFETIME_S`**, when neither is available.

    Tier 1 should always win for Auth0 M2M, which issues JWTs; the rest exist so
    an unexpected token shape degrades instead of crashing. Tier 3 genuinely is a
    guess, and is the one weak spot here: a real lifetime shorter than it would
    not be noticed until the API began rejecting requests.

    The claim is read **without verifying the signature**, which is correct at
    this layer — verification is the API server's job, and this only reads an
    expiry the token advertises about itself. It is not a security decision.

    :param token: The raw access token.
    :param issued_at: When it was requested, epoch seconds.
    :param expires_in: Auth0's advertised lifetime in seconds, if given.
    :return: Epoch second the token stops being usable.
    """
    try:
        payload_segment = token.split(".")[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        exp = claims.get("exp")
        if isinstance(exp, (int, float)):
            return float(exp)
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        # An opaque (non-JWT) token is legitimate; fall through to expires_in.
        pass

    if expires_in:
        return issued_at + float(expires_in)
    return issued_at + _FALLBACK_LIFETIME_S


class Auth0TokenProvider:
    """Mints, caches and re-mints one M2M token for the process.

    The lock guards *wasted work*, not data corruption — which is what makes it
    different from :data:`~atriumdb_dashboard.pipeline.data_sdk_lock`, the other
    lock in this package.

    The endpoints run in FastAPI's threadpool, so several requests can call
    :meth:`get_token` at the same moment. Without the lock, five threads arriving
    just inside the refresh margin would each see "stale", each POST to Auth0,
    and each overwrite ``self._token`` — five tokens minted where one was needed.
    Auth0 rate-limits M2M token requests, and every replacement also makes
    :func:`apply_token` close the websocket, so the surplus mints would tear down
    connections underneath transfers still in flight.

    With the lock, the first thread mints and caches; the rest wait briefly, find
    a fresh token, and return it. It is the classic check-then-act race: deciding
    the token is stale and replacing it have to be one atomic step, or every
    thread acts on the same stale observation.

    :param config: The resolved :class:`~atriumdb_dashboard.config.DataConfig`.
    """

    def __init__(self, config: "DataConfig"):
        self._config = config
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expiry: float = 0.0

        if config.static_token:
            # Supplied pre-minted, for iterating. Its real expiry still governs:
            # a stale ATRIUMDB_API_TOKEN left in a shell should be replaced by a
            # freshly minted one rather than used until requests start failing.
            self._token = config.static_token
            self._expiry = _token_expiry(config.static_token, time.time(), None)

    @property
    def expiry(self) -> float:
        """Epoch second the cached token expires; ``0.0`` when none is held."""
        return self._expiry

    def get_token(self) -> str:
        """Return a valid token, minting or re-minting only when needed.

        "Still valid" means more than :data:`_REFRESH_MARGIN_S` of life left, per
        the expiry :func:`_token_expiry` derived when the token was cached.
        Otherwise this re-mints through :meth:`_mint_locked`, which requires the
        lock this method is already holding.

        :return: The raw ``access_token``.
        :raises AuthError: If no token is held and none can be minted.
        """
        with self._lock:
            if self._token and time.time() < self._expiry - _REFRESH_MARGIN_S:
                return self._token
            return self._mint_locked()

    def _mint_locked(self) -> str:
        """Request a token from Auth0 and cache it.

        The ``_locked`` suffix is a **precondition, not an action**: this method
        does not take ``self._lock``, and the caller must already hold it. Only
        :meth:`get_token` calls it, and it does so from inside its ``with
        self._lock`` block.

        Split that way because the two jobs are different: :meth:`get_token`
        holds the lock and decides *whether* a mint is needed; this does the
        mint. Folding them together would mean either acquiring the lock twice
        or leaving the freshness check unguarded — and the check is the part that
        must be guarded, since several threadpool workers reaching an expiring
        token at once would otherwise fire one Auth0 request each, against a
        metered endpoint.

        :return: The raw ``access_token``, which is also cached on the instance
            along with the expiry derived by :func:`_token_expiry`.
        :raises AuthError: On any non-200 from Auth0, on a network failure, or
            when no credentials are configured and no token is already held. Note
            this covers failures *minting* a token; a token that Auth0 issues but
            the AtriumDB API then rejects surfaces separately, as the SDK's own
            ``ValueError`` from ``_request``.
        """
        config = self._config

        if not config.can_mint:
            if self._token:
                # A static token past its margin and no credentials to replace
                # it. Returning it is still the best available action: it may
                # have life left, and failing here would take out endpoints that
                # would otherwise work.
                _LOGGER.warning(
                    "API token is at or past expiry and no Auth0 credentials are "
                    "configured to mint a replacement; using the existing token."
                )
                return self._token
            raise AuthError(
                "No Auth0 credentials configured and no ATRIUMDB_API_TOKEN supplied."
            )

        tenant = _normalize_tenant(config.tenant or "")
        issued_at = time.time()

        try:
            response = requests.post(
                f"{tenant}{_TOKEN_PATH}",
                headers={"content-type": "application/json"},
                json={
                    "client_id": config.client_id,
                    "client_secret": config.client_secret,
                    "audience": config.audience,
                    "grant_type": config.grant_type,
                },
                timeout=_TOKEN_REQUEST_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            # Never interpolate the payload: it carries the client secret.
            raise AuthError(f"Could not reach Auth0 at {tenant}: {exc}") from exc

        if response.status_code != 200:
            raise AuthError(
                f"Auth0 rejected the token request (HTTP {response.status_code}): "
                f"{response.text}\n{_explain_status(response.status_code)}"
            )

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise AuthError(f"Auth0 returned 200 but no access_token: {payload}")

        self._token = token
        self._expiry = _token_expiry(token, issued_at, payload.get("expires_in"))
        _LOGGER.info(
            "Minted Auth0 access token; valid for %d minute(s).",
            max(0, int((self._expiry - issued_at) / 60)),
        )
        return token


def apply_token(sdk: "AtriumSDK", token: str) -> None:
    """Put ``token`` on ``sdk``, dropping the websocket if it changed.

    The second half is the part that is easy to miss. ``_websocket_connect``
    sets ``Authorization: Bearer <token>`` once, and the SDK deliberately holds
    that connection open for the life of the object. Assigning ``sdk.token``
    alone therefore fixes the HTTP calls and leaves the block transfers
    authenticating with the old token, which the server will eventually refuse
    — surfacing as the SDK's ``RuntimeError("API token has expired")`` from deep
    inside a block read.

    Closing it here mirrors what ``AtriumSDK._refresh_token`` does before
    re-minting, and the next :func:`fetch_nan_filled_window` reconnects.

    :param sdk: The API-mode SDK to update.
    :param token: The token it should use from now on.
    """
    if getattr(sdk, "token", None) == token:
        return

    sdk.token = token

    websock_conn = getattr(sdk, "websock_conn", None)
    if websock_conn is not None:
        try:
            websock_conn.close()
        except Exception:  # noqa: BLE001 - a dead socket is the expected case
            # The connection is being discarded either way; a failure to close
            # it cleanly must not propagate into a request.
            _LOGGER.debug("Closing the stale websocket failed; discarding it anyway.",
                          exc_info=True)
        sdk.websock_conn = None
        _LOGGER.debug("Token changed; dropped the websocket so it reconnects.")
