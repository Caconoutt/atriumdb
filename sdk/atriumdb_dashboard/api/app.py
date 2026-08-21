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

"""Wiring that mounts the dashboard router onto a FastAPI application.

Kept separate from the router itself so that the dashboard can be attached to
an application owned by someone else — the AtriumDB test app, or a real
deployment — without either side importing the other.
"""

from __future__ import annotations

from atriumdb_dashboard.api.cohort_endpoints import router

DASHBOARD_PREFIX = "/cohorts"


def mount_dashboard(app, prefix: str = DASHBOARD_PREFIX):
    """Attach the dashboard cohorts router to an existing FastAPI app.

    Additive by design: this is what replaces editing the host application's
    ``app.py`` to add an ``include_router`` call, keeping the upstream
    AtriumDB test app byte-identical to ``main``.

    :param app: The FastAPI application to mount onto.
    :param prefix: URL prefix for the dashboard routes. Defaults to
        ``"/cohorts"``.
    :return: The same ``app``, to allow chaining.
    """
    app.include_router(router, prefix=prefix)
    return app


def create_dashboard_app(prefix: str = DASHBOARD_PREFIX):
    """Build a standalone FastAPI app serving only the dashboard routes.

    :param prefix: URL prefix for the dashboard routes.
    :return: A new ``FastAPI`` instance with the dashboard mounted.
    """
    from fastapi import FastAPI

    return mount_dashboard(FastAPI(), prefix=prefix)
