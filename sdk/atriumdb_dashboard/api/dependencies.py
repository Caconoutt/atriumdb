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

"""The SDK dependency shared by every dashboard router.

One provider for all routers, mirroring the upstream ``tests/mock_api``
convention of a single ``sdk_dependency`` module rather than one per endpoint
file.

Sharing it is what keeps ``dependency_overrides`` usable: FastAPI keys that
mapping by the function object, so a per-router provider would mean each router
needing its own override, and importing several same-named providers into one
module would silently rebind the name and override the wrong router.
"""

import os
from functools import lru_cache

from atriumdb import AtriumSDK

DATASET_LOCATION_ENV_VAR = "ATRIUMDB_DATASET_LOCATION"


@lru_cache(maxsize=1)
def get_sdk_instance() -> AtriumSDK:
    """Return the process-wide AtriumSDK, opening the dataset on first use.

    The dataset directory — the one holding ``meta/index.db`` and ``tsc/`` —
    comes from ``ATRIUMDB_DATASET_LOCATION``. The SDK constructor does not read
    that variable itself (only the CLI does), so it is passed explicitly here.

    Cached because building an instance re-loads the C library and re-reads the
    settings table, and this runs as a FastAPI ``Depends`` — once per request
    without the cache. Sharing one instance is safe for the direct-DB path:
    :class:`~atriumdb.sql_handler.sqlite.sqlite_handler.SQLiteHandler` opens a
    fresh connection per query rather than holding one on the instance.

    Tests do not go through this function — they inject their own SDK through
    ``app.dependency_overrides``, which is keyed by this function object and so
    bypasses both the cache and the environment lookup.
    """
    dataset_location = os.environ.get(DATASET_LOCATION_ENV_VAR)
    if not dataset_location:
        raise RuntimeError(
            f"{DATASET_LOCATION_ENV_VAR} is not set. Point it at the dataset "
            f"directory containing meta/index.db and tsc/."
        )
    return AtriumSDK(dataset_location=dataset_location)
