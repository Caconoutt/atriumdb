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
which reads ``ATRIUMDB_DATASET_LOCATION``.
"""

from tests.mock_api.app import app as app

from atriumdb_dashboard.api.app import mount_dashboard

mount_dashboard(app)


@app.get("/health")
async def health():
    """Liveness probe for container orchestration.

    Deliberately does not touch the SDK: it answers whether the process is
    serving, which is what a healthcheck should gate ``depends_on`` upon.
    Dataset problems surface as errors on the routes that actually read it.
    """
    return {"status": "ok"}


__all__ = ["app"]
