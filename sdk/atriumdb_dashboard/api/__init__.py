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

Both routers define a provider named ``get_sdk_instance``, so they are
re-exported aliased. Importing them unaliased would bind one name to the other
module's provider, and ``dependency_overrides`` — which is keyed by the function
object — would then silently override the wrong router.
"""

from atriumdb_dashboard.api.app import (
    DASHBOARD_PREFIX,
    create_dashboard_app,
    mount_dashboard,
)
from atriumdb_dashboard.api.cohort_endpoints import (
    get_sdk_instance as get_cohort_sdk,
    router as cohort_router,
)
from atriumdb_dashboard.api.statistics_endpoints import (
    get_sdk_instance as get_statistics_sdk,
    router as statistics_router,
)

__all__ = [
    "DASHBOARD_PREFIX",
    "cohort_router",
    "create_dashboard_app",
    "get_cohort_sdk",
    "get_statistics_sdk",
    "mount_dashboard",
    "statistics_router",
]
