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

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic.alias_generators import to_camel

from atriumdb.dashboard.locations import LOCATION_LOOKUP

#: Sex codes accepted in a demographic cohort filter. A patient whose
#: ``patient.gender`` is NULL, empty, or the ``'U'`` unknown marker cannot match
#: either code, so such patients are excluded whenever a sex filter is applied.
VALID_SEX_CODES: tuple[str, ...] = ("M", "F")


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

    @model_validator(mode="after")
    def _start_not_after_end(self) -> "AdmissionDateRange":
        """Reject an inverted window.

        ``start > end`` describes an empty interval, so every cohort in the
        request would resolve to zero patients. That is indistinguishable from
        a genuine no-match result, so it is rejected at the boundary rather
        than returned as an empty response.
        """
        if self.start > self.end:
            raise ValueError(
                f"admissionDateRange.start ({self.start}) must not be after "
                f"admissionDateRange.end ({self.end})."
            )
        return self


class Admission(_Base):
    """One qualifying admission, with the location the patient occupied.

    The ``encounter → bed → unit`` join already runs during cohort resolution,
    so the unit name is carried through to the response rather than discarded —
    downstream consumers (the statistics endpoint, the Data Records table) need
    it and would otherwise have to re-run the same join.

    A stay that transfers between units produces one ``Admission`` per unit, so
    that a cohort spanning several locations (e.g. ICU *and* OR) can still say
    which one each patient was in. Bed-to-bed moves within a single unit are
    collapsed into one admission.

    :param admission_ns: Encounter start time in Unix epoch nanoseconds. This
        anchors the downstream observation window.
    :param discharge_ns: Encounter end time in Unix epoch nanoseconds; ``None``
        when the stay is still open.
    :param location: Unit name for this admission, e.g. ``"ICU"`` or ``"OR"``.
        ``None`` when the encounter has no unit recorded.
    """

    admission_ns: int
    discharge_ns: int | None = None
    location: str | None = None


class PatientAdmission(_Base):
    """A single patient in a resolved cohort with all their qualifying admissions.

    :param mrn: The patient's medical record number.
    :param admissions: All qualifying admissions, sorted ascending by
        ``admission_ns``. Each entry corresponds to a distinct admission within
        the request's ``admission_date_range``. A patient with multiple in-range
        admissions will have multiple entries here.
    """

    mrn: str
    admissions: list[Admission]


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
    :param sex: Optional list of sex codes, one of ``VALID_SEX_CODES``
        (``"M"`` or ``"F"``). Accepted case-insensitively and normalised to
        upper case; each matches its stored ``patient.gender`` value directly.
        There is no code for unknown sex, so a patient whose ``gender`` is
        NULL, empty, or ``'U'`` is excluded whenever this filter is present.
        An unrecognised code is rejected rather than silently matching no one.
    :param location: Optional list of API location codes (e.g. ``["ICU"]``).
        Validated against ``LOCATION_LOOKUP`` here, then resolved to
        ``unit.name`` values at query time. ``None`` means no location filter —
        all admitted patients qualify.
    :param value_range: Reserved for future vital-sign range filtering; unused
        in Priority 1B.
    """

    id: str
    age: list[AgeBand] | None = None
    sex: list[str] | None = None
    location: list[str] | None = None
    value_range: dict | None = None

    @field_validator("location")
    @classmethod
    def _known_location_codes(cls, value: list[str] | None) -> list[str] | None:
        """Reject location codes absent from ``LOCATION_LOOKUP``.

        Checking here rather than at query time is what turns an unknown code
        into a 422 naming the offending element, instead of a ``ValueError``
        surfacing from
        :func:`~atriumdb.dashboard.encounter_queries.query_patient_encounters`
        as a 500.
        """
        if value is None:
            return None
        unknown = [code for code in value if code not in LOCATION_LOOKUP]
        if unknown:
            raise ValueError(
                f"Unknown location code(s) {unknown}. "
                f"Valid codes are: {list(LOCATION_LOOKUP)}"
            )
        return value

    @field_validator("sex")
    @classmethod
    def _known_sex_codes(cls, value: list[str] | None) -> list[str] | None:
        """Normalise sex codes to upper case and reject unrecognised ones.

        Without this an unrecognised code such as ``"X"`` matches no patient
        and the cohort resolves empty, which the caller cannot tell apart from
        a filter that genuinely matched no one.
        """
        if value is None:
            return None
        normalised = [code.strip().upper() for code in value]
        unknown = [code for code in normalised if code not in VALID_SEX_CODES]
        if unknown:
            raise ValueError(
                f"Unknown sex code(s) {unknown}. "
                f"Valid codes are: {list(VALID_SEX_CODES)}"
            )
        return normalised


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

    @model_validator(mode="after")
    def _cohorts_match_type(self) -> "CohortDefinitionRequest":
        """Reject a request whose cohorts do not match its ``type``.

        The union above is resolved by shape, not by ``type``, so a
        ``"demographic"`` request carrying ``mrnList`` entries would otherwise
        parse as ``MrnCohort`` and fail later with an ``AttributeError`` deep
        in the resolver. Checking here turns that into a validation error at
        the request boundary.
        """
        expected = MrnCohort if self.type == "mrn" else DemographicCohort
        for cohort in self.cohorts:
            if not isinstance(cohort, expected):
                raise ValueError(
                    f"type={self.type!r} requires every cohort to be a "
                    f"{expected.__name__}; cohort id={getattr(cohort, 'id', '?')!r} "
                    f"parsed as {type(cohort).__name__}."
                )
        return self


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

    :param request_id: Echo of the ``X-Request-ID`` request header. Always
        non-empty — a missing or blank header is rejected before resolution
        begins, so it can be used as a correlation key against server logs.
    :param cohorts: One ``ResolvedCohort`` per input cohort, in the same order
        as the request.
    """

    request_id: str
    cohorts: list[ResolvedCohort]
