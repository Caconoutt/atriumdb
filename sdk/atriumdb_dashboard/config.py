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

"""The dashboard's entire configuration surface.

The only module that reads ``os.environ``. Everything else takes a resolved
config object, so there is one place to look for "where does this value come
from" and one place that can fail at startup rather than mid-request.

**The rule: the dashboard reads the process environment. How values get there is
the deployment's business.** Locally that is a ``.env`` file; in deployment it is
whatever the orchestrator injects. The Python does not know the difference,
which is what keeps the two from needing different code.

Precedence, highest first:

1. Real environment variables.
2. A ``.env`` file, loaded with ``override=False`` so it fills gaps and never
   clobbers something the deployment set deliberately.
3. Defaults declared here — for non-secret values only. No secret has a default.

Secrets (``ATRIUMDB_MARIA_PASSWORD``, ``AUTH0_CLIENT_SECRET``) are read but never
logged, never echoed in an error, and never written to disk. Validation reports
missing *names*; :func:`describe` renders values redacted.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "DashboardConfig",
    "DataConfig",
    "MetaConfig",
    "ConfigError",
    "describe",
    "load_config",
]

# --------------------------------------------------------------------------
# Environment variable names, declared once
# --------------------------------------------------------------------------

#: Which backend the metadata SDK opens. ``sqlite`` keeps the pre-UAT local
#: behaviour working unchanged, so an existing deployment needs no new variables.
METADATA_CONNECTION_TYPE_ENV_VAR = "ATRIUMDB_METADATA_CONNECTION_TYPE"

#: SQLite dataset directory. Also supplies ``tsc_file_location`` in mariadb mode,
#: where the SDK demands a file location it then never reads — see
#: :func:`~atriumdb_dashboard.api.dependencies.get_meta_sdk`.
DATASET_LOCATION_ENV_VAR = "ATRIUMDB_DATASET_LOCATION"

MARIA_HOST_ENV_VAR = "ATRIUMDB_MARIA_HOST"
MARIA_PORT_ENV_VAR = "ATRIUMDB_MARIA_PORT"
MARIA_USER_ENV_VAR = "ATRIUMDB_MARIA_USER"
MARIA_PASSWORD_ENV_VAR = "ATRIUMDB_MARIA_PASSWORD"
MARIA_DATABASE_ENV_VAR = "ATRIUMDB_MARIA_DATABASE"

#: Base URL of the AtriumDB API, **including any version segment**. The SDK
#: appends bare endpoint names ("measures/", "sdk/blocks") to this, and there is
#: no separate setting for a version prefix, so a versioned API must carry it
#: here or every request 404s.
API_URL_ENV_VAR = "ATRIUMDB_API_URL"

#: Optional pre-minted token. Skips the Auth0 round trip, for iterating.
API_TOKEN_ENV_VAR = "ATRIUMDB_API_TOKEN"

AUTH0_TENANT_ENV_VAR = "AUTH0_TENANT"
AUTH0_CLIENT_ID_ENV_VAR = "AUTH0_CLIENT_ID"
AUTH0_CLIENT_SECRET_ENV_VAR = "AUTH0_CLIENT_SECRET"
AUTH0_AUDIENCE_ENV_VAR = "AUTH0_AUDIENCE"
AUTH0_GRANT_TYPE_ENV_VAR = "AUTH0_GRANT_TYPE"

SQLITE = "sqlite"
MARIADB = "mariadb"

DEFAULT_METADATA_CONNECTION_TYPE = SQLITE
DEFAULT_MARIA_PORT = 3306
DEFAULT_GRANT_TYPE = "client_credentials"

#: Names whose values must never reach a log, an error message, or a response.
_SECRET_ENV_VARS = frozenset({MARIA_PASSWORD_ENV_VAR, AUTH0_CLIENT_SECRET_ENV_VAR,
                              API_TOKEN_ENV_VAR})


class ConfigError(RuntimeError):
    """Raised at startup when required configuration is missing or malformed.

    Carries variable *names* only. Never include a value in the message: this
    propagates to logs and to the console of whoever started the container.
    """


# --------------------------------------------------------------------------
# Resolved configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MetaConfig:
    """How to open the metadata SDK.

    :param connection_type: ``"sqlite"`` or ``"mariadb"``.
    :param dataset_location: SQLite dataset directory; in mariadb mode this is
        passed as ``tsc_file_location`` purely to satisfy the constructor.
    :param connection_params: ``{host, user, password, database, port}`` for
        mariadb mode, else ``None``.
    """

    connection_type: str
    dataset_location: str | None
    connection_params: dict | None = None

    @property
    def is_mariadb(self) -> bool:
        return self.connection_type == MARIADB

    def __repr__(self) -> str:
        """Redact the password.

        A dataclass's generated repr would print ``connection_params`` whole,
        password included — and a config object reaches a repr more easily than
        it looks: a stray ``_LOGGER.debug("%s", config)``, a traceback that
        renders locals, an interactive session. ``DataConfig`` gets the same
        protection from ``field(repr=False)``; this one needs a method because
        the secret is inside a dict rather than its own field.
        """
        params = self.connection_params
        if params is not None:
            params = {**params, "password": "***" if params.get("password") else None}
        return (
            f"MetaConfig(connection_type={self.connection_type!r}, "
            f"dataset_location={self.dataset_location!r}, "
            f"connection_params={params!r})"
        )


@dataclass(frozen=True)
class DataConfig:
    """How to open the API-mode SDK and mint tokens for it.

    :param api_url: Base URL including any version segment.
    :param tenant: Auth0 tenant hostname.
    :param client_id: Auth0 M2M client id.
    :param client_secret: Auth0 M2M client secret. Secret.
    :param audience: Auth0 audience; stamped into the token's ``aud`` claim, and
        **not** the same value as ``api_url``.
    :param grant_type: Always ``client_credentials`` for this flow.
    :param static_token: A pre-minted token from the environment, or ``None`` to
        mint one.
    """

    api_url: str
    tenant: str | None = None
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)
    audience: str | None = None
    grant_type: str = DEFAULT_GRANT_TYPE
    static_token: str | None = field(default=None, repr=False)

    @property
    def can_mint(self) -> bool:
        """Whether the Auth0 values needed to mint a token are all present."""
        return all((self.tenant, self.client_id, self.client_secret, self.audience))


@dataclass(frozen=True)
class DashboardConfig:
    """Both halves, resolved and validated."""

    meta: MetaConfig
    data: DataConfig | None

    @property
    def data_configured(self) -> bool:
        """Whether the API-mode SDK can be built at all.

        ``False`` for a purely local deployment, where the statistics and
        time-series endpoints are unavailable but cohorts and measure-hours still
        work. Kept as a property rather than an assertion so that an incomplete
        API configuration degrades two endpoints instead of refusing to start.
        """
        return self.data is not None


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def _load_dotenv_if_present() -> None:
    """Load a ``.env`` next to the package, and one in the working directory.

    ``override=False`` throughout: a value already in the environment was put
    there deliberately by the deployment and always wins over a file.

    Deliberately silent when ``python-dotenv`` is absent or no file exists — a
    container gets real environment variables and has no ``.env`` at all, which
    is the intended production shape rather than a degraded one.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"):
        if candidate.is_file():
            load_dotenv(dotenv_path=candidate, override=False)
            _LOGGER.info("Loaded configuration defaults from %s", candidate)


def _get(name: str) -> str | None:
    """Return a stripped environment value, treating blank as absent.

    An empty string is almost always an unfilled template line rather than an
    intentional value, and letting one through produces a confusing failure much
    later (an empty password, a zero-length URL).
    """
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _require(names: list[str], context: str) -> dict[str, str]:
    """Read every name, or raise naming all of the missing ones at once.

    Reporting them together matters: filling one variable, restarting, and
    discovering the next is a slow way to configure a container.
    """
    values = {name: _get(name) for name in names}
    missing = sorted(name for name, value in values.items() if value is None)
    if missing:
        raise ConfigError(
            f"{context} is missing required environment variable(s): "
            + ", ".join(missing)
        )
    return values  # type: ignore[return-value]


def _load_meta_config() -> MetaConfig:
    """Resolve the metadata half, or raise :class:`ConfigError`."""
    connection_type = (
        _get(METADATA_CONNECTION_TYPE_ENV_VAR) or DEFAULT_METADATA_CONNECTION_TYPE
    ).lower()

    if connection_type not in (SQLITE, MARIADB):
        raise ConfigError(
            f"{METADATA_CONNECTION_TYPE_ENV_VAR} must be '{SQLITE}' or '{MARIADB}'; "
            f"got {connection_type!r}."
        )

    dataset_location = _get(DATASET_LOCATION_ENV_VAR)

    if connection_type == SQLITE:
        if not dataset_location:
            raise ConfigError(
                f"{DATASET_LOCATION_ENV_VAR} is not set. Point it at the dataset "
                f"directory containing meta/index.db and tsc/."
            )
        return MetaConfig(connection_type=SQLITE, dataset_location=dataset_location)

    values = _require(
        [
            MARIA_HOST_ENV_VAR,
            MARIA_USER_ENV_VAR,
            MARIA_PASSWORD_ENV_VAR,
            MARIA_DATABASE_ENV_VAR,
        ],
        context="MariaDB metadata configuration",
    )

    port_raw = _get(MARIA_PORT_ENV_VAR)
    try:
        port = int(port_raw) if port_raw else DEFAULT_MARIA_PORT
    except ValueError as exc:
        raise ConfigError(
            f"{MARIA_PORT_ENV_VAR} must be an integer; got {port_raw!r}."
        ) from exc

    # The mariadb branch of AtriumSDK.__init__ requires one of dataset_location
    # or tsc_file_location even though the metadata SDK reads no .tsc files —
    # it builds an AtriumFileHandler unconditionally. Any existing directory
    # satisfies it; see get_meta_sdk for how this is passed.
    if not dataset_location:
        raise ConfigError(
            f"{DATASET_LOCATION_ENV_VAR} must be set even in {MARIADB} mode. The SDK "
            f"constructor requires a file location before it looks at the connection "
            f"type; the dashboard never reads waveform files through this instance, so "
            f"any existing directory will do."
        )

    return MetaConfig(
        connection_type=MARIADB,
        dataset_location=dataset_location,
        connection_params={
            "host": values[MARIA_HOST_ENV_VAR],
            "user": values[MARIA_USER_ENV_VAR],
            "password": values[MARIA_PASSWORD_ENV_VAR],
            "database": values[MARIA_DATABASE_ENV_VAR],
            "port": port,
        },
    )


def _load_data_config() -> DataConfig | None:
    """Resolve the API half, or return ``None`` when it is not configured.

    Returning ``None`` rather than raising is deliberate: a local SQLite
    deployment has no API to talk to, and should still serve the two endpoints
    that do not need one. The statistics and time-series endpoints raise a clear
    503 in that case — see
    :func:`~atriumdb_dashboard.api.dependencies.get_data_sdk`.
    """
    api_url = _get(API_URL_ENV_VAR)
    if not api_url:
        return None

    static_token = _get(API_TOKEN_ENV_VAR)

    config = DataConfig(
        api_url=api_url.rstrip("/"),
        tenant=_get(AUTH0_TENANT_ENV_VAR),
        client_id=_get(AUTH0_CLIENT_ID_ENV_VAR),
        client_secret=_get(AUTH0_CLIENT_SECRET_ENV_VAR),
        audience=_get(AUTH0_AUDIENCE_ENV_VAR),
        grant_type=_get(AUTH0_GRANT_TYPE_ENV_VAR) or DEFAULT_GRANT_TYPE,
        static_token=static_token,
    )

    if not config.can_mint and not static_token:
        missing = sorted(
            name
            for name, value in (
                (AUTH0_TENANT_ENV_VAR, config.tenant),
                (AUTH0_CLIENT_ID_ENV_VAR, config.client_id),
                (AUTH0_CLIENT_SECRET_ENV_VAR, config.client_secret),
                (AUTH0_AUDIENCE_ENV_VAR, config.audience),
            )
            if not value
        )
        raise ConfigError(
            f"{API_URL_ENV_VAR} is set, so the API-mode SDK will be built, but it has "
            f"no way to authenticate: supply {API_TOKEN_ENV_VAR}, or all of "
            + ", ".join(missing)
            + "."
        )

    return config


def load_config() -> DashboardConfig:
    """Read and validate the whole configuration surface.

    Called once at startup so that a misconfiguration is a refusal to start with
    a message naming every missing variable, rather than a 500 on the first
    request that happens to need one.

    :return: The resolved :class:`DashboardConfig`.
    :raises ConfigError: If anything required is missing or malformed. The
        message names variables, never values.
    """
    _load_dotenv_if_present()
    return DashboardConfig(meta=_load_meta_config(), data=_load_data_config())


def describe(config: DashboardConfig) -> str:
    """Render the configuration for a startup log line, with secrets redacted.

    Exists so that "what is this container actually pointed at" is answerable
    from the log without anyone being tempted to log the config object itself.
    """
    meta = config.meta
    if meta.is_mariadb:
        params = meta.connection_params or {}
        where = (
            f"mariadb {params.get('user')}@{params.get('host')}:{params.get('port')}"
            f"/{params.get('database')}"
        )
    else:
        where = f"sqlite {meta.dataset_location}"

    if config.data is None:
        api = "not configured"
    else:
        auth = "static token" if config.data.static_token else "auth0 client_credentials"
        api = f"{config.data.api_url} ({auth})"

    return f"metadata: {where} | data api: {api}"
