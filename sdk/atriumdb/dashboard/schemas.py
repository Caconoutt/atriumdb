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

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class _Base(BaseModel):
    """Shared config: snake_case in Python, camelCase in JSON."""
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class AdmissionDateRange(_Base):
    """Inclusive admission window, both bounds in Unix epoch nanoseconds (UTC).

    Used to scope which encounters are considered when resolving a cohort. An
    encounter qualifies when ``encounter.start_time`` falls within
    ``[start, end]`` (inclusive on both sides).
    """

    start: int
    end: int


class PatientAdmission(_Base):
    """A single patient in a resolved cohort with all their qualifying admissions.

    :param mrn: The patient's medical record number.
    :param admissions: All qualifying encounter start times in Unix epoch
        nanoseconds, sorted ascending. Each entry corresponds to a distinct
        visit within the request's ``admission_date_range``. A patient with
        multiple in-range visits will have multiple entries here.
    """

    mrn: str
    admissions: list[int]


class AgeBand(_Base):
    """A single age band expressed in nanoseconds.

    Both bounds are pre-converted by the dashboard server using the convention::

        total_ns = (year * 365 + month * 30) * 86_400_000_000_000

    AtriumDB receives nanosecond values directly and does not perform any
    further unit conversion.
    """

    start_ns: int
    end_ns: int


class MrnCohort(_Base):
    """A cohort defined by an explicit list of MRNs (Priority 1A).

    Each MRN in ``mrn_list`` is validated against AtriumDB: it must both exist
    in the ``patient`` table and have at least one ``encounter`` record within
    the request's ``admission_date_range``. MRNs that fail either check are
    silently excluded and logged server-side.
    """

    id: str
    mrn_list: list[str]


class DemographicCohort(_Base):
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
    :param value_range: Reserved for future vital-sign range filtering; unused
        in Priority 1B.
    """

    id: str
    age: list[AgeBand] | None = None
    sex: list[str] | None = None
    location: list[str] | None = None
    value_range: dict | None = None


class CohortDefinitionRequest(_Base):
    """Top-level request body for ``POST /cohorts``.

    The ``type`` field routes the entire request to either the MRN-validation
    path (1A) or the demographic-filter path (1B). All cohorts in a single
    request must be of the same type.

    :param type: ``"mrn"`` → 1A; ``"demographic"`` → 1B.
    :param admission_date_range: Applies to both routes. In 1A it defines the
        window an MRN must have an admission in. In 1B it both scopes the
        candidate encounter pool and provides the per-patient ``admit_time_ns``
        anchor needed to compute age-at-admission correctly.
    :param cohorts: One or more cohort definitions, all of the type indicated
        by ``type``.
    """

    type: Literal["mrn", "demographic"]
    admission_date_range: AdmissionDateRange
    cohorts: list[MrnCohort] | list[DemographicCohort]


class ResolvedCohort(_Base):
    """A single resolved cohort in the response.

    :param id: The cohort identifier echoed from the request.
    :param patients: Validated patients that passed all filters. Every entry is
        confirmed to exist in AtriumDB, have an admission within the requested
        date range, and carries the earliest qualifying ``admission_ns`` as the
        anchor for downstream observation window calculations.
    """

    id: str
    patients: list[PatientAdmission]


class MrnCohortResponse(_Base):
    """Response body for ``POST /cohorts``.

    :param request_id: Echo of the ``X-Request-ID`` request header, or an empty
        string if the header was omitted.
    :param cohorts: One ``ResolvedCohort`` per input cohort, in the same order
        as the request.
    """

    request_id: str
    cohorts: list[ResolvedCohort]
