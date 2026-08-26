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

"""The ASGI application served in deployment.

Composes the upstream AtriumDB application with the dashboard routers at import
time, which is what lets ``tests/mock_api/app.py`` stay byte-identical to
upstream: the deploy branch this was ported from added the routers and the
health route to that file directly, and this module is the additive equivalent.

Served as::

    uvicorn atriumdb_dashboard.deploy.server:app --host 0.0.0.0 --port 8000

The upstream routes ship alongside the dashboard ones deliberately — the
deployment check verifies ``/measures/`` (upstream) as well as
``/measures/hours``, ``/cohorts`` and ``/cohorts/statistics`` (dashboard).

The SDK comes from :func:`~atriumdb_dashboard.api.dependencies.get_sdk_instance`,
which reads ``ATRIUMDB_DATASET_LOCATION``. The upstream routes are pointed at it
too, via ``dependency_overrides`` — see below.

Logging is configured here, at import, by :func:`configure_logging` — see its
docstring for the two environment variables involved.
"""

import logging
import logging.config
import os

from tests.mock_api.app import app as app
from tests.mock_api.sdk_dependency import get_sdk_instance as _upstream_get_sdk

from atriumdb_dashboard.api.app import mount_dashboard
from atriumdb_dashboard.api.dependencies import get_sdk_instance

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

#: Level for every ``atriumdb_dashboard.*`` logger. INFO by default rather than
#: DEBUG: the resolvers emit a full per-entry value dump at DEBUG, which for a
#: 24 h window at 1 Hz is roughly 1 MB per patient per request.
LOG_LEVEL_ENV_VAR = "ATRIUMDB_DASHBOARD_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "INFO"

#: Optional path for the exclusion audit trail. Unset means exclusions go to the
#: console with everything else.
EXCLUSION_LOG_ENV_VAR = "ATRIUMDB_DASHBOARD_EXCLUSION_LOG"

#: The two child loggers carrying per-entry exclusion records. Named explicitly
#: rather than derived, so adding a third resolver is a deliberate edit here
#: rather than something that silently fails to be routed.
EXCLUSION_LOGGERS = (
    "atriumdb_dashboard.statistics_resolver.exclusions",
    "atriumdb_dashboard.timeseries_resolver.exclusions",
)

_LOGGER = logging.getLogger(__name__)


def _resolve_log_level() -> str:
    """Return the configured level name, falling back to the default.

    An unrecognised value falls back rather than raising: a typo in an
    environment variable should not stop the container from serving.
    """
    requested = os.environ.get(LOG_LEVEL_ENV_VAR, DEFAULT_LOG_LEVEL).strip().upper()
    if requested in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
        return requested
    logging.getLogger(__name__).warning(
        "%s=%r is not a recognised level; falling back to %s.",
        LOG_LEVEL_ENV_VAR, requested, DEFAULT_LOG_LEVEL,
    )
    return DEFAULT_LOG_LEVEL


def configure_logging() -> None:
    """Give the ``atriumdb_dashboard`` loggers a handler and a level.

    Without this the package's nine loggers are unconfigured nodes inheriting
    from a root that nothing sets up: uvicorn's own ``dictConfig`` touches only
    ``uvicorn``, ``uvicorn.access`` and ``uvicorn.error``, so records fall
    through to :data:`logging.lastResort` — a bare stderr handler pinned at
    WARNING with no formatter. The practical effect is that every ``debug()``
    call in the package is discarded, and the ``warning()`` calls that do get
    out carry no timestamp, level or logger name.

    Two switches are needed, not one. Setting a level alone still leaves no
    handler to receive the record, so it is emitted and then dropped — paying
    the cost of building the message for nothing.

    Controlled by two environment variables:

    ``ATRIUMDB_DASHBOARD_LOG_LEVEL``
        Level for every ``atriumdb_dashboard.*`` logger; ``INFO`` by default.
        Set ``DEBUG`` to surface the per-entry diagnostics, including the full
        value grid each resolver fetches — useful for one patient, very large
        for a cohort.

    ``ATRIUMDB_DASHBOARD_EXCLUSION_LOG``
        Optional file path for the exclusion audit trail. When set, the two
        ``*.exclusions`` loggers write there and stop propagating, so the audit
        trail is a file of its own rather than interleaved into the console. An
        unwritable path fails at startup, deliberately: an audit trail that
        silently goes nowhere is worse than a container that refuses to start.
        When unset, exclusions go to the console like everything else.

    Neither ``root`` nor uvicorn's loggers are declared here, and
    ``disable_existing_loggers`` is ``False``, so uvicorn's access log is left
    exactly as it was.
    """
    level = _resolve_log_level()
    exclusion_path = os.environ.get(EXCLUSION_LOG_ENV_VAR)

    config: dict = {
        "version": 1,
        # uvicorn has already configured its own loggers by the time this module
        # is imported; disabling existing loggers would silence its access log.
        "disable_existing_loggers": False,
        "formatters": {
            "dashboard": {
                "format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "dashboard",
            },
        },
        "loggers": {
            "atriumdb_dashboard": {
                "handlers": ["console"],
                "level": level,
                # Not propagated to root: root has no handlers here, and leaving
                # it on would double up under any future root configuration.
                "propagate": False,
            },
        },
    }

    if exclusion_path:
        config["handlers"]["exclusions"] = {
            "class": "logging.FileHandler",
            "filename": exclusion_path,
            "formatter": "dashboard",
        }
        for name in EXCLUSION_LOGGERS:
            config["loggers"][name] = {
                "handlers": ["exclusions"],
                # These emit at WARNING only; pinning it here keeps the audit
                # trail complete even when the package level is raised.
                "level": "WARNING",
                "propagate": False,
            }

    logging.config.dictConfig(config)

    _LOGGER.info(
        "Dashboard logging configured: level=%s, exclusion log=%s",
        level, exclusion_path or "<console>",
    )


configure_logging()

mount_dashboard(app)

# The upstream provider is ``return AtriumSDK()`` with no arguments, which raises
# "dataset location must be specified for sqlite mode" on the first request to any
# upstream route. Overriding it here rather than editing ``tests/mock_api`` is what
# keeps that package byte-identical to upstream, and one entry covers every
# upstream route since they all depend on the same function object.
app.dependency_overrides[_upstream_get_sdk] = get_sdk_instance


@app.get("/health")
async def health():
    """Liveness probe for container orchestration.

    Deliberately does not touch the SDK: it answers whether the process is
    serving, which is what a healthcheck should gate ``depends_on`` upon.
    Dataset problems surface as errors on the routes that actually read it.
    """
    return {"status": "ok"}


__all__ = ["app"]
