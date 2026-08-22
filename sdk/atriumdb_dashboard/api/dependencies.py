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

from atriumdb import AtriumSDK


def get_sdk_instance() -> AtriumSDK:
    """Provide the direct-DB SDK instance the dashboard endpoints run against.

    The default constructs an SDK from the ambient environment. Deployments and
    tests are expected to replace it with
    ``app.dependency_overrides[get_sdk_instance] = lambda: sdk``, which then
    applies to every dashboard router at once.
    """
    return AtriumSDK()
