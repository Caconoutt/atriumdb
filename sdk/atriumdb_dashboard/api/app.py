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
from atriumdb_dashboard.api.measures_endpoints import router as measures_router

COHORT_PREFIX = "/cohorts"
MEASURES_PREFIX = "/measures"


def _mount_router(app, router, prefix: str):
    """Include ``router`` at ``prefix`` and move its routes to the front.

    Starlette matches routes in registration order and takes the first full
    match, with no preference for a more specific path. ``include_router`` can
    only append, so a dashboard route added to an app that already has a
    matching wildcard would never be reached.

    That is not hypothetical: the host registers ``GET /measures/{measure_id}``,
    whose pattern also matches ``/measures/hours``. Appended normally, a request
    for the dashboard endpoint resolves to ``get_measure_info(measure_id="hours")``
    instead. Registering first is what the original in-place edit achieved by
    declaring ``/hours`` above ``/{measure_id}`` in the same module.

    The cohort routes have no such conflict — nothing upstream serves
    ``/cohorts`` — but they are mounted the same way so that one rule covers
    every router and a future host route cannot quietly capture them.

    Only the routes added by this call move ahead, and every dashboard path is a
    literal, so nothing already registered can be shadowed.

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


def mount_dashboard(
    app,
    cohort_prefix: str = COHORT_PREFIX,
    measures_prefix: str = MEASURES_PREFIX,
):
    """Attach every dashboard router to an existing FastAPI app.

    Additive by design: this is what replaces editing the host application's
    endpoint modules, keeping the upstream AtriumDB test app byte-identical to
    ``main``.

    Each router keeps its own SDK dependency, so a caller overriding one does
    not affect the other. Both providers are named ``get_sdk_instance``; import
    them aliased apart, since ``dependency_overrides`` is keyed by the function
    object and the unaliased names collide.

    :param app: The FastAPI application to mount onto.
    :param cohort_prefix: URL prefix for the cohorts router, serving
        ``POST /cohorts``.
    :param measures_prefix: URL prefix for the measures router, serving
        ``GET /measures/hours``.
    :return: The same ``app``, to allow chaining.
    """
    _mount_router(app, cohort_router, cohort_prefix)
    _mount_router(app, measures_router, measures_prefix)
    return app


def create_dashboard_app(
    cohort_prefix: str = COHORT_PREFIX,
    measures_prefix: str = MEASURES_PREFIX,
):
    """Build a standalone FastAPI app serving only the dashboard routes.

    :param cohort_prefix: URL prefix for the cohorts router.
    :param measures_prefix: URL prefix for the measures router.
    :return: A new ``FastAPI`` instance with the dashboard mounted.
    """
    from fastapi import FastAPI

    return mount_dashboard(
        FastAPI(),
        cohort_prefix=cohort_prefix,
        measures_prefix=measures_prefix,
    )
