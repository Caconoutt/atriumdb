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

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class AdmissionDateRange(BaseModel):
    """Inclusive admission window, both bounds in Unix epoch nanoseconds (UTC).

    Used to scope which encounters are considered when resolving a cohort. An
    encounter qualifies when ``encounter.start_time`` falls within
    ``[start, end]`` (inclusive on both sides).
    """

    start: int
    end: int


class AgeBand(BaseModel):
    """A single age band expressed in nanoseconds.

    Both bounds are pre-converted by the dashboard server using the convention::

        total_ns = (year * 365 + month * 30) * 86_400_000_000_000

    AtriumDB receives nanosecond values directly and does not perform any
    further unit conversion.
    """

    startNs: int
    endNs: int


class MrnCohort(BaseModel):
    """A cohort defined by an explicit list of MRNs (Priority 1A).

    Each MRN in ``mrnList`` is validated against AtriumDB: it must both exist
    in the ``patient`` table and have at least one ``encounter`` record within
    the request's ``admissionDateRange``. MRNs that fail either check are
    silently excluded and logged server-side.
    """

    id: str
    mrnList: list[str]


class DemographicCohort(BaseModel):
    """A cohort defined by demographic filters (Priority 1B).

    All supplied filters are ANDed together; within each filter the individual
    values are ORed (e.g. ``sex=["F","U"]`` matches female *or* unknown).

    :param id: Caller-assigned cohort identifier returned unchanged in the response.
    :param age: Optional list of age bands in nanoseconds. Age is evaluated at
        the patient's reference admission time (earliest in-range admit), not
        the current date. Patients with an unknown ``dob`` are excluded when
        this filter is present.
    :param sex: Optional list of sex codes. ``"M"`` and ``"F"`` match their
        stored ``patient.gender`` values directly. ``"U"`` matches NULL, empty,
        or the literal ``'U'`` stored in ``patient.gender``.
    :param location: Optional list of API location codes (e.g. ``["ICU"]``).
        Resolved server-side via ``LOCATION_LOOKUP`` against ``unit.name``.
        ``None`` means no location filter — all admitted patients qualify.
    :param valueRange: Reserved for future vital-sign range filtering; unused
        in Priority 1B.
    """

    id: str
    age: list[AgeBand] | None = None
    sex: list[str] | None = None
    location: list[str] | None = None
    valueRange: dict | None = None


class CohortDefinitionRequest(BaseModel):
    """Top-level request body for ``POST /cohorts``.

    The ``type`` field routes the entire request to either the MRN-validation
    path (1A) or the demographic-filter path (1B). All cohorts in a single
    request must be of the same type.

    :param type: ``"mrn"`` → 1A; ``"demographic"`` → 1B.
    :param admissionDateRange: Applies to both routes. In 1A it defines the
        window an MRN must have an admission in. In 1B it both scopes the
        candidate encounter pool and provides the per-patient ``admit_time_ns``
        anchor needed to compute age-at-admission correctly.
    :param cohorts: One or more cohort definitions, all of the type indicated
        by ``type``.
    """

    type: Literal["mrn", "demographic"]
    admissionDateRange: AdmissionDateRange
    cohorts: list[MrnCohort] | list[DemographicCohort]


class ResolvedCohort(BaseModel):
    """A single resolved cohort in the response.

    :param id: The cohort identifier echoed from the request.
    :param mrnList: Validated MRNs that passed all filters. Every MRN here is
        confirmed to exist in AtriumDB and have an admission in the requested
        date range.
    """

    id: str
    mrnList: list[str]


class MrnCohortResponse(BaseModel):
    """Response body for ``POST /cohorts``.

    :param requestId: Echo of the ``X-Request-ID`` request header, or an empty
        string if the header was omitted.
    :param cohorts: One ``ResolvedCohort`` per input cohort, in the same order
        as the request.
    """

    requestId: str
    cohorts: list[ResolvedCohort]
