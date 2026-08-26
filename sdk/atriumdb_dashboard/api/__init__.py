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

"""HTTP surface for the dashboard: the routers and their app wiring.

Every router takes its SDK from the single provider in
:mod:`atriumdb_dashboard.api.dependencies`, so one
``app.dependency_overrides[get_sdk_instance]`` entry covers all of them.
"""

from atriumdb_dashboard.api.app import (
    COHORT_PREFIX,
    MEASURES_PREFIX,
    create_dashboard_app,
    mount_dashboard,
)
from atriumdb_dashboard.api.cohort_endpoints import router as cohort_router
from atriumdb_dashboard.api.dependencies import get_sdk_instance
from atriumdb_dashboard.api.measures_endpoints import router as measures_router
from atriumdb_dashboard.api.statistics_endpoints import router as statistics_router
from atriumdb_dashboard.api.timeseries_endpoints import router as timeseries_router

__all__ = [
    "COHORT_PREFIX",
    "MEASURES_PREFIX",
    "cohort_router",
    "create_dashboard_app",
    "get_sdk_instance",
    "measures_router",
    "mount_dashboard",
    "statistics_router",
    "timeseries_router",
]
