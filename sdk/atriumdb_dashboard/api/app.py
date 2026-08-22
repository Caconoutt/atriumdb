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

"""Wiring that mounts the dashboard routers onto a FastAPI application.

Kept separate from the routers themselves so that the dashboard can be attached
to an application owned by someone else — the AtriumDB test app, or a real
deployment — without either side importing the other.
"""

from __future__ import annotations

from atriumdb_dashboard.api.cohort_endpoints import router as cohort_router
from atriumdb_dashboard.api.statistics_endpoints import router as statistics_router

DASHBOARD_PREFIX = "/cohorts"


def _mount_router(app, router, prefix: str):
    """Include ``router`` at ``prefix`` and move its routes to the front.

    Starlette matches routes in registration order and takes the first full
    match, with no preference for a more specific path, while
    ``include_router`` can only append. Registering the dashboard first means a
    host route that happens to match a dashboard path — a wildcard such as
    ``/{cohort_id}``, say — cannot capture it.

    Nothing upstream currently serves ``/cohorts``, so this is defensive rather
    than load-bearing here. Every dashboard path is a literal, so only the
    routes added by this call move ahead and nothing already registered can be
    shadowed.

    :param app: The FastAPI application to mount onto.
    :param router: The ``APIRouter`` to include.
    :param prefix: URL prefix for that router.
    :return: The same ``app``, to allow chaining.
    """
    first_new = len(app.router.routes)
    app.include_router(router, prefix=prefix)

    added = app.router.routes[first_new:]
    del app.router.routes[first_new:]
    app.router.routes[0:0] = added

    return app


def mount_dashboard(app, prefix: str = DASHBOARD_PREFIX):
    """Attach every dashboard router to an existing FastAPI app.

    Additive by design: this is what replaces editing the host application's
    ``app.py`` to add an ``include_router`` call, keeping the upstream AtriumDB
    test app byte-identical to ``main``.

    Both routers share the ``/cohorts`` prefix and serve distinct literal paths
    — ``POST /cohorts`` resolves a cohort definition, ``POST /cohorts/statistics``
    computes statistics over already-resolved cohorts — so their order relative
    to each other does not matter.

    Each router keeps its own SDK dependency, so a caller overriding one does
    not affect the other. Both providers are named ``get_sdk_instance``; import
    them aliased apart, since ``dependency_overrides`` is keyed by the function
    object and the unaliased names collide.

    :param app: The FastAPI application to mount onto.
    :param prefix: URL prefix shared by the dashboard routers.
    :return: The same ``app``, to allow chaining.
    """
    _mount_router(app, cohort_router, prefix)
    _mount_router(app, statistics_router, prefix)
    return app


def create_dashboard_app(prefix: str = DASHBOARD_PREFIX):
    """Build a standalone FastAPI app serving only the dashboard routes.

    :param prefix: URL prefix shared by the dashboard routers.
    :return: A new ``FastAPI`` instance with the dashboard mounted.
    """
    from fastapi import FastAPI

    return mount_dashboard(FastAPI(), prefix=prefix)
