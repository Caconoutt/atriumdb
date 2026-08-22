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

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, PositiveInt, field_validator, model_validator
from pydantic.alias_generators import to_camel


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
    :param location: Optional list of location names (e.g. ``["ICU"]``),
        matched against ``unit.name``. Not validated here: the valid set lives
        in the database, which a Pydantic validator cannot reach, so
        :func:`~atriumdb_dashboard.locations.validate_location_codes` checks it
        at resolve time and the endpoint maps a failure to a 422. ``None``
        means no location filter — all admitted patients qualify.
    :param value_range: Reserved for future vital-sign range filtering; unused
        in Priority 1B.
    """

    id: str
    age: list[AgeBand] | None = None
    sex: list[str] | None = None
    location: list[str] | None = None
    value_range: dict | None = None

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


# ---------------------------------------------------------------------------
# S2 - Cohort statistics
#
# These models share ``_Base``, ``Admission``, and ``PatientAdmission`` with the
# cohort-definition models above rather than redeclaring them: a cohort resolved
# by S1 is the input to the S2 pipeline, so the two must agree by construction.
# ---------------------------------------------------------------------------

ALL_TIME = "all_time"

class ExclusionReason(str, Enum):
    MRN_NOT_FOUND = "mrn_not_found"
    NO_DEVICE_FOUND = "no_device_found"
    BELOW_AVAILABILITY_THRESHOLD = "below_availability_threshold"
    NO_USABLE_VALUES = "no_usable_values"
    MISSING_DISCHARGE_TIME = "missing_discharge_time"

class ValueRange(_Base):
    """Signal value bounds applied during retrieval.

    Either bound may be ``None`` to leave that end open::

        {"lower": 40.0, "upper": 200.0}  # drop values outside [40, 200]
        {"lower": null, "upper": 180.0}  # drop only values above 180
        {"lower": 30.0, "upper": null}   # drop only values below 30
        {"lower": null, "upper": null}   # no filtering

    Values outside the bounds are treated as absent: they reduce a patient's
    data availability rather than being averaged in.

    :param lower: Inclusive lower bound; ``None`` leaves the low end open.
    :param upper: Inclusive upper bound; ``None`` leaves the high end open.
    """

    lower: float | None = None
    upper: float | None = None

ValueRangeMap = dict[str, ValueRange]

class MeasureIdentifier(_Base):
    """Identifies a physiological signal measure in AtriumDB.

    All four fields are passed to ``sdk.get_measure_id()`` to resolve the
    internal integer measure ID. Returns ``None`` if no matching measure exists.
    """

    measure_tag: str
    freq: float
    units: str | None = None
    freq_units: str | None = None

class CohortInput(_Base):
    """A pre-resolved cohort passed to the statistics endpoint.

    Typically built from the output of the cohort resolver (S1). Each patient
    already has a verified MRN and admission anchor.

    :param id: Integer cohort identifier.
    :param patients: Pre-resolved patient list from the cohort resolver.
    :param value_range: This cohort's own signal bounds, keyed by measure tag —
        carried through from the dashboard query and intersected with the
        request's global ``value_range`` for this cohort, so these bounds can
        only narrow the global range, never widen it. Omitted entirely when the
        cohort set no bounds of its own, in which case the global range applies
        on its own.
    """

    id: int
    patients: list[PatientAdmission]
    value_range: ValueRangeMap | None = None

class AggregateStatisticsRequest(_Base):
    """Request body for ``POST /cohort/statistics``.

    :param cohorts: One entry per cohort, each containing the pre-resolved
        patient list from the cohort resolver and, optionally, its own
        ``value_range`` override.
    :param measure: Identifies the signal to analyse.
    :param observation_window: Either a fixed window length in epoch nanoseconds,
        anchored at each patient's ``admission_ns`` (e.g. 24 h =
        ``86_400_000_000_000``), or the string ``"all_time"``, in which case each
        entry's window instead spans its own admission — from ``admission_ns`` to
        ``discharge_ns``. Under ``"all_time"`` an admission with no
        ``discharge_ns`` has no bounded window and is excluded with
        :attr:`ExclusionReason.MISSING_DISCHARGE_TIME`.
    :param availability_threshold: Minimum fraction ``[0, 1]`` of the window
        that must be covered by valid data for a patient to be included.
        Defaults to ``0.80``.
    :param value_range: Global signal bounds, keyed by measure tag. Values
        outside the bounds are treated as absent — they reduce a patient's data
        availability rather than being averaged in, so an admission whose signal
        is mostly out of range fails :attr:`availability_threshold` instead of
        producing a plausible-looking mean. A cohort's own ``value_range`` is
        intersected with this one for that cohort. Omitted when the query set no
        global bounds.
    """

    cohorts: list[CohortInput]
    measure: MeasureIdentifier
    # Positive: a zero-length window has nothing to measure availability against.
    observation_window: PositiveInt | Literal["all_time"]
    availability_threshold: float = 0.80
    value_range: ValueRangeMap | None = None

class ExclusionRecord(_Base):
    """One excluded (patient, admission) entry from the statistics pipeline.

    Populated for every entry dropped at any stage so the client can build
    a full audit report without re-running the request.

    :param mrn: Patient identifier.
    :param admission_ns: The specific admission that was excluded; ``None``
        for patient-level exclusions (``mrn_not_found``) where no admission
        was reached.
    :param reason: The pipeline stage that dropped this entry; see
        :class:`ExclusionReason` for the full set of values.
    :param window_start_ns: Observation window start; ``None`` for
        ``mrn_not_found``, and for ``missing_discharge_time`` where no window
        could be bounded.
    :param window_end_ns: Observation window end; ``None`` in the same cases as
        ``window_start_ns``.
    :param availability: Actual data coverage fraction ``[0, 1]``; present
        only for ``below_availability_threshold``.
    """

    mrn: str
    admission_ns: int | None = None
    reason: ExclusionReason
    window_start_ns: int | None = None
    window_end_ns: int | None = None
    availability: float | None = None

class PatientResult(_Base):
    """Per-admission summary statistic for one cohort entry.

    A patient with multiple qualifying admissions produces one ``PatientResult``
    per admission. Use ``(mrn, admission_ns)`` together as the unique key.

    The three demographic fields describe the patient *at this admission* and
    feed the Data Records table. They are optional: an AtriumDB instance that
    does not yet return them leaves them ``None``, and the dashboard renders an
    em-dash rather than failing.

    :param mrn: Patient identifier, carried through for export.
    :param admission_ns: The specific admission this result is anchored to.
        Distinguishes multiple entries for the same patient.
    :param mean: Mean of signal values over the observation window after
        NaN removal.
    :param sex: ``"M"`` or ``"F"``; ``None`` when unknown or not returned.
    :param age_months: Age at admission in whole months (a 3y 4m old is 40).
        ``None`` when unknown or not returned.
    :param location: Encounter location for this admission, e.g. ``"ICU"`` or
        ``"OR"``. ``None`` when unknown or not returned.
    """

    mrn: str
    admission_ns: int
    mean: float
    sex: str | None = None
    age_months: int | None = None
    location: str | None = None

class CohortStatistics(_Base):
    """Statistics result for one cohort.

    Counts distinguish between *patients* (distinct MRNs) and *entries*
    ((patient, admission) pairs). A patient with two qualifying admissions
    counts as one patient but two entries.

    :param cohort_id: Echoed from the corresponding ``CohortInput.id``.
    :param n_patients: Distinct patients whose MRN resolved to a patient ID.
        MRNs that could not be resolved are not counted here.
    :param n_visits: Total (patient, admission) entries going into the
        pipeline — equal to ``sum(len(p.admissions) for resolved patients)``.
    :param n_included: Entries that passed all filters and produced a result.
    :param n_excluded: Entries filtered out at any stage. Per-entry detail is
        written to the exclusions log.
    :param patient_results: One entry per included (patient, admission) pair.
    :param exclusions: One record per dropped entry, with enough context for
        the client to explain each drop without re-running the request.
    """

    cohort_id: int
    n_patients: int
    n_visits: int
    n_included: int
    n_excluded: int
    patient_results: list[PatientResult]
    exclusions: list[ExclusionRecord]

class AggregateStatisticsResponse(_Base):
    """Response body for ``POST /cohort/statistics``."""

    cohorts: list[CohortStatistics]
