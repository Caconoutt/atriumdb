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

"""The two SDK providers the dashboard's routers depend on.

Two backends, because the four endpoints ask two different kinds of question:

``get_meta_sdk``
    Direct-DB — SQLite locally, MariaDB in UAT. Everything that is a database
    question: encounters, units, patients, the demographic cohort filter,
    measure definitions, block-index sums. Needs ``sdk.sql_handler``, which is
    ``None`` in api mode.

``get_data_sdk``
    API mode over an Auth0 M2M token. Everything that is a signal question:
    waveform blocks and interval coverage. Also answers the metadata lookups the
    statistics and time-series endpoints make, because those endpoints use one
    SDK for everything.

**One SDK per endpoint. No endpoint uses both.** ``/cohorts`` and
``/measures/hours`` are ``get_meta_sdk``; ``/cohorts/statistics`` and
``/cohorts/timeseries`` are ``get_data_sdk``, metadata lookups included. Both
backends read the same metadata store, so ``patient_id`` and ``measure_id`` mean
the same thing on either side and the choice is free rather than forced — but
keeping each endpoint on one SDK means a call cannot be routed to the wrong
place, and every resolver below keeps its single ``sdk`` parameter.

Each provider is cached for the process: building an instance re-loads the C
library and re-reads the settings table, and these run as FastAPI ``Depends``,
so without the cache that would happen once per request. The consequence is that
one MariaDB connection and one websocket are shared across every request — see
the thread-safety notes on each provider.

Sharing one API-mode instance also means sharing its single websocket, which the
SDK guards with nothing at all. Serialising that is
:data:`~atriumdb_dashboard.pipeline.data_sdk_lock`'s job, and it lives in
:mod:`atriumdb_dashboard.pipeline` beside its only user — the dependency runs one
way, ``api`` importing ``pipeline`` and never the reverse.

Tests bypass all of this through ``app.dependency_overrides``, which is keyed by
the function object and so skips both the cache and the environment.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from fastapi import HTTPException

from atriumdb import AtriumSDK
from atriumdb_dashboard.auth0 import Auth0TokenProvider, AuthError, apply_token
from atriumdb_dashboard.config import (
    DATASET_LOCATION_ENV_VAR,
    ConfigError,
    DashboardConfig,
    load_config,
)

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "DATASET_LOCATION_ENV_VAR",
    "get_config",
    "get_data_sdk",
    "get_meta_sdk",
    "get_sdk_instance",
]


@lru_cache(maxsize=1)
def get_config() -> DashboardConfig:
    """Return the process-wide configuration, reading the environment once.

    Separate from the SDK providers so that startup can validate configuration
    before anything tries to open a connection — see
    :func:`~atriumdb_dashboard.deploy.server.configure_logging`'s caller.
    """
    config = load_config()
    return config


@lru_cache(maxsize=1)
def get_meta_sdk() -> AtriumSDK:
    """Return the process-wide direct-DB SDK.

    SQLite or MariaDB depending on ``ATRIUMDB_METADATA_CONNECTION_TYPE``. The
    SQLite path preserves the pre-UAT behaviour exactly, so an existing local
    deployment needs no new configuration.

    Two things about the MariaDB path are worth knowing at the call site.

    **``no_pool=True`` is not optional.** With pooling left on, ``MariaDBHandler``
    uses ``SingleConnectionManager``, which despite the name holds *one*
    connection behind a borrow flag and *raises* — rather than waiting — if a
    second caller asks while it is borrowed. The endpoints run in FastAPI's
    threadpool, so two concurrent requests would mean one getting
    ``ValueError: The connection is already borrowed`` and a 500, on a perfectly
    valid request. ``no_pool=True`` opens and closes a connection per query,
    which is thread-safe by construction and the right trade against
    intermittent failures.

    **``tsc_file_location`` is passed but never read.** The mariadb branch of
    ``AtriumSDK.__init__`` requires one of ``dataset_location`` or
    ``tsc_file_location`` before it looks at anything else, because it builds an
    ``AtriumFileHandler`` unconditionally. This instance answers metadata
    questions only and never opens a ``.tsc`` file.

    **Why there is no token machinery here.** The MariaDB password *is* the
    credential: it goes straight to the driver and does not expire, so there is
    nothing to refresh. The API side needs :mod:`atriumdb_dashboard.auth0`
    because its secret is not what talks to the API — it is exchanged for a
    token that does expire, and something has to track and replace it.

    Both connections can nevertheless go stale, and the two halves solve that
    oppositely: the data SDK holds a token and refreshes it, while this one holds
    no connection at all. ``no_pool=True`` means every query opens a fresh
    connection, which is always fresh by construction — the SDK's own answer,
    ``SingleConnectionManager`` pinging an idle connection and reconnecting on
    failure, is machinery we skip rather than rely on. The data SDK cannot use
    the same trick: a database connection is cheap and unmetered, whereas Auth0
    rate-limits token requests and rebuilding the SDK re-loads the C library.

    ``auto_upgrade`` is left at its ``False`` default deliberately: with it on,
    the constructor runs ``update_measure_schema()`` and ``upgrade_mrn_schema()``
    — DDL against the UAT database. The read-only account makes that safe by
    construction, which is the point of it.

    :return: A direct-DB ``AtriumSDK``.
    :raises ConfigError: If the metadata configuration is missing or malformed.
    """
    config = get_config().meta

    if config.is_mariadb:
        _LOGGER.info(
            "Opening metadata SDK: mariadb %s@%s:%s/%s",
            (config.connection_params or {}).get("user"),
            (config.connection_params or {}).get("host"),
            (config.connection_params or {}).get("port"),
            (config.connection_params or {}).get("database"),
        )
        return AtriumSDK(
            metadata_connection_type="mariadb",
            connection_params=config.connection_params,
            tsc_file_location=config.dataset_location,
            no_pool=True,
        )

    _LOGGER.info("Opening metadata SDK: sqlite %s", config.dataset_location)
    return AtriumSDK(dataset_location=config.dataset_location)


@lru_cache(maxsize=1)
def _build_data_sdk() -> AtriumSDK:
    """Construct the API-mode SDK once, with a freshly minted token.

    Split from :func:`get_data_sdk` so that the per-request token refresh is not
    itself cached — the instance is built once, but its token is checked every
    time it is handed to an endpoint.

    ``validate_token=False`` because Auth0 client-credentials issues no refresh
    token: ``AtriumSDK._refresh_token`` posts ``grant_type=refresh_token``, which
    Auth0 rejects for an M2M client. With validation off, ``_request`` never
    tries to refresh and simply uses ``sdk.token``, which
    :mod:`atriumdb_dashboard.auth0` keeps current.

    ``token`` is passed explicitly for a second reason: with ``token=None`` the
    SDK loads a dotenv itself, from ``./.env`` relative to the working directory
    and with ``override=True`` — a path that is not stable and an override that
    would clobber variables the deployment set deliberately. Passing the token
    means that branch never runs.
    """
    config = get_config().data
    if config is None:  # pragma: no cover - guarded by get_data_sdk
        raise ConfigError("The API-mode SDK is not configured.")

    token = _get_token_provider().get_token()
    _LOGGER.info("Opening data SDK: api %s", config.api_url)
    return AtriumSDK(
        metadata_connection_type="api",
        api_url=config.api_url,
        token=token,
        validate_token=False,
    )


@lru_cache(maxsize=1)
def _get_token_provider() -> Auth0TokenProvider:
    """Return the process-wide token provider."""
    config = get_config().data
    if config is None:  # pragma: no cover - guarded by get_data_sdk
        raise ConfigError("The API-mode SDK is not configured.")
    return Auth0TokenProvider(config)


def get_data_sdk() -> AtriumSDK:
    """Return the process-wide API-mode SDK, with a current token.

    Not itself ``lru_cache``d: the *instance* is cached by
    :func:`_build_data_sdk`, but the token is re-checked on every request so a
    long-lived process does not drift past expiry. Re-minting happens only near
    expiry — Auth0 meters M2M token requests, so this is one token per process,
    never one per call, and it is never written to disk.

    :return: An api-mode ``AtriumSDK`` whose ``token`` is valid.
    :raises HTTPException: 503 if the API side is not configured at all, or if a
        token cannot be obtained. A 503 rather than a 500 because both are
        deployment states rather than request errors, and both are retryable
        once the deployment is fixed.
    """
    if get_config().data is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "This endpoint requires the AtriumDB API, which is not configured. "
                "Set ATRIUMDB_API_URL and the AUTH0_* variables."
            ),
        )

    try:
        sdk = _build_data_sdk()
        apply_token(sdk, _get_token_provider().get_token())
    except AuthError as exc:
        _LOGGER.error("Could not obtain an API token: %s", exc)
        raise HTTPException(
            status_code=503, detail=f"Could not authenticate to the AtriumDB API: {exc}"
        ) from exc

    return sdk


#: Backwards-compatible alias.
#:
#: Before the UAT split there was one provider named ``get_sdk_instance``, and
#: ``deploy/server.py`` overrides the upstream test app's provider with it. The
#: metadata SDK is what those upstream routes want, so the old name now points
#: there. Kept so that an existing local deployment and any caller outside this
#: package keep working unchanged.
get_sdk_instance = get_meta_sdk
