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

import logging
from enum import Enum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    PositiveInt,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

_LOGGER = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# S3 - Cohort time-series
#
# Same cohort/measure inputs as S2, but the observation window is chopped into
# fixed-width intervals and a per-patient mean is reported for each. The models
# below reuse S2's field types throughout; only the containers are new, because
# the per-interval response normalises patient demographics into a per-cohort
# ``visits`` table rather than repeating them on every bucket.
# ---------------------------------------------------------------------------

#: Upper bound on ``observation_window // interval_ns``. The response is
#: O(intervals x visits), so an unbounded bucket count is a memory hazard well
#: before it is a useful query: a 24 h window at 1 s buckets is 86,400 buckets,
#: which across a few hundred visits is tens of millions of result objects. The
#: intended range sits far below this (24 h at 1 min = 1,440), so the cap only
#: ever catches a miscalibrated request — most likely an ``interval_ns`` that
#: was never converted from minutes.
MAX_INTERVALS = 2_000


class TimeSeriesRequest(_Base):
    """Request body for ``POST /cohorts/timeseries``.

    Deliberately not a subclass of :class:`AggregateStatisticsRequest`: this
    endpoint's ``observation_window`` cannot be ``"all_time"``, and inheriting a
    field only to narrow it is more confusing than declaring the shared four
    again. The field-level *types* are reused; the container is its own.

    :param cohorts: One entry per cohort, each carrying the pre-resolved patient
        list from the cohort resolver and, optionally, its own ``value_range``.
    :param measure: Identifies the signal to analyse.
    :param observation_window: Fixed window length in epoch nanoseconds,
        anchored at each patient's ``admission_ns``. Unlike S2 there is no
        ``"all_time"`` option: under ``all_time`` each stay would yield a
        different number of buckets, so late intervals would silently contain
        only the longer-staying patients and no shared x-axis would exist.
    :param interval_ns: Bucket width in epoch nanoseconds — e.g. 5 min is
        ``300_000_000_000``. The dashboard server converts the user's minute
        selection before calling; AtriumDB does no unit inference, so a value of
        ``5`` means 5 nanoseconds.
    :param availability_threshold: Minimum fraction ``[0, 1]`` of a *bucket*
        that must be covered by valid data for that bucket's mean to be
        reported. Applied per interval and only per interval — this endpoint has
        no window-level availability gate, so an entry is never dropped for
        being sparse across the window as a whole.

        **Required, with no default.** How much coverage makes a bucket mean
        trustworthy is the caller's judgement, not this layer's: a default here
        would silently discard data on behalf of a caller who never asked for
        any filtering, and the caller could not tell that apart from a genuinely
        sparse signal. Pass ``0.0`` to apply no threshold at all, in which case
        a bucket is reported whenever it holds at least one usable sample.
    :param value_range: Global signal bounds, keyed by measure tag, with the
        same "out-of-range means absent" semantics as S2. A cohort's own
        ``value_range`` is intersected with this one for that cohort. Applied
        per interval.
    """

    cohorts: list[CohortInput]
    measure: MeasureIdentifier
    # Positive, and no "all_time": a time-series needs a bucket grid shared by
    # every patient, which only a fixed window provides.
    observation_window: PositiveInt
    interval_ns: PositiveInt
    # Required, deliberately: no default. See the field docs above.
    availability_threshold: float
    value_range: ValueRangeMap | None = None

    @model_validator(mode="after")
    def _window_divides_into_intervals(self) -> "TimeSeriesRequest":
        """Reject a window the interval does not evenly divide, or too many buckets.

        Even division is what makes every bucket exactly ``interval_ns`` wide,
        so availability fractions are comparable across buckets and the bucket
        count is unambiguous. Allowing a short trailing bucket is defensible but
        pushes an edge case onto every downstream consumer.
        """
        if self.observation_window % self.interval_ns != 0:
            raise ValueError(
                f"observationWindow ({self.observation_window}) must be an exact "
                f"multiple of intervalNs ({self.interval_ns}); it currently leaves "
                f"a remainder of {self.observation_window % self.interval_ns} ns, "
                f"which would make the final interval narrower than the rest."
            )

        n_intervals = self.observation_window // self.interval_ns
        if n_intervals > MAX_INTERVALS:
            raise ValueError(
                f"observationWindow ({self.observation_window}) / intervalNs "
                f"({self.interval_ns}) yields {n_intervals} intervals, above the "
                f"limit of {MAX_INTERVALS}. Widen intervalNs — note it is "
                f"denominated in nanoseconds, so a value meant as minutes will "
                f"land here."
            )
        return self

    # NOTE: this must stay the LAST model_validator declared on this class.
    # A wrap validator only wraps the validation applied *below* it, so an
    # ``after`` validator declared underneath this one would have its errors
    # escape the logging entirely — silently, and only for that one check.
    @model_validator(mode="wrap")
    @classmethod
    def _log_rejected_request(cls, data, handler):
        """Log every rejection of this model, then re-raise unchanged.

        Without this a rejected request leaves no server-side trace at all.
        FastAPI's default ``RequestValidationError`` handler only builds a 422
        response — it logs nothing — and uvicorn's access log records the status
        code without the reason or any header, so a malformed request is
        otherwise observable only as an anonymous ``422`` line. That matters
        most for the divisibility and ``MAX_INTERVALS`` checks, which the
        dashboard frontend is supposed to make unreachable: a 422 there signals
        a frontend bug, and is exactly the thing worth having a record of.

        Wrapping is what makes the coverage complete. Catching inside
        :meth:`_window_divides_into_intervals` would log only that check, while
        this catches field-level failures too — a missing
        ``availabilityThreshold``, an ``"all_time"`` window, a malformed cohort.

        Logged at WARNING deliberately: the deployment configures no logging, so
        these loggers fall through to ``logging.lastResort``, which drops
        anything below WARNING. A rejected request is not a server fault, but it
        is not routine either.

        Only the error locations and messages are logged, never
        ``ValidationError.errors()[*]["input"]`` — the offending input is the
        request body, which carries patient MRNs.

        No request ID is available: a Pydantic model cannot see request headers,
        and this runs before the endpoint function reads ``X-Request-ID``.
        Correlating these lines to a request needs a
        ``RequestValidationError`` handler, which does receive the ``Request``.
        """
        try:
            return handler(data)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or '<request>'}: "
                f"{error['msg']}"
                for error in exc.errors()
            )
            _LOGGER.warning(
                "Rejected %s: %d validation error(s): %s",
                cls.__name__, exc.error_count(), details,
            )
            raise


class VisitInfo(_Base):
    """One (patient, admission) entry, with its demographics carried once.

    Demographics are demographics-*at-admission*: for a given
    ``(mrn, admission_ns)`` they are constant for the whole stay, so repeating
    them on every interval is lossless to remove and substantial to carry — a
    24 h / 5 min request holds 288 result rows per visit.

    Every per-interval row in the response references one of these by its
    position in :attr:`CohortTimeSeries.visits`, so the client has exactly one
    resolution rule to learn: ``visits[row.visit]``.

    :param mrn: Patient identifier.
    :param admission_ns: The specific admission this entry is anchored to.
        Distinguishes multiple entries for the same patient — a readmitted
        patient has a different ``age_months`` and often a different
        ``location`` per admission, which an MRN-keyed table would collapse.
    :param sex: ``"M"`` or ``"F"``; ``None`` when unknown or not returned.
    :param age_months: Age at admission in whole months (a 3y 4m old is 40).
        ``None`` when unknown or not returned.
    :param location: Encounter location for this admission. ``None`` when the
        encounter has no unit recorded.
    """

    mrn: str
    admission_ns: int
    sex: str | None = None
    age_months: int | None = None
    location: str | None = None


class VisitMean(_Base):
    """One visit's mean signal value inside one interval.

    The normalised counterpart of :class:`PatientResult`: same ``mean``
    semantics, but the patient is named by index rather than by repeating the
    MRN, admission timestamp and demographics on every bucket.

    :param visit: 0-based index into the enclosing
        :attr:`CohortTimeSeries.visits`.
    :param mean: Mean of the usable samples in this interval.
    """

    visit: int
    mean: float


class VisitExclusion(_Base):
    """One visit dropped, either before bucketing or from a single interval.

    The normalised counterpart of :class:`ExclusionRecord`. Window bounds are
    not carried: for an interval-level exclusion they are exactly the enclosing
    :attr:`IntervalResult.start_offset_ns` / ``end_offset_ns``, so repeating
    them per record would be pure duplication.

    :param visit: 0-based index into the enclosing
        :attr:`CohortTimeSeries.visits`.
    :param reason: Why this visit was dropped. In
        :attr:`CohortTimeSeries.patient_exclusions` this is always
        ``mrn_not_found`` — the only entry-level drop reachable under a fixed
        observation window. In :attr:`IntervalResult.exclusions` it is
        ``below_availability_threshold`` or ``no_usable_values``.
    :param availability: This interval's covered fraction ``[0, 1]``; present
        only for ``below_availability_threshold``.
    """

    visit: int
    reason: ExclusionReason
    availability: float | None = None


class IntervalResult(_Base):
    """One bucket of the observation window, across every visit in the cohort.

    Interval ``i`` covers ``[admission_ns + i*interval_ns,
    admission_ns + (i+1)*interval_ns)``. Offsets are relative to each patient's
    own admission, so the same index means the same elapsed time-since-admission
    for every patient — which is what makes the series plottable on one shared
    x-axis.

    Intervals are always dense and complete, ``0 … n-1``. An interval where
    every visit was excluded is still emitted, with an empty
    ``patient_results``, so the client renders a visible gap rather than
    interpolating across a missing index.

    :param interval_index: 0-based position in the series.
    :param start_offset_ns: ``interval_index * interval_ns``, offset from
        admission — not an absolute timestamp.
    :param end_offset_ns: ``(interval_index + 1) * interval_ns``.
    :param n_included: Visits with a usable mean in this interval. Because there
        is no window-level gate, this population varies per interval and
        generally shrinks across the series as patients are discharged or come
        off monitoring — plot it alongside each point, or a changing denominator
        will read as a trend.
    :param n_excluded: Visits dropped in this interval. ``n_included +
        n_excluded`` equals :attr:`CohortTimeSeries.n_visits` in every interval.
    :param patient_results: One entry per visit with a usable mean here.
    :param exclusions: One record per visit dropped *in this interval*.
    """

    interval_index: int
    start_offset_ns: int
    end_offset_ns: int
    n_included: int
    n_excluded: int
    patient_results: list[VisitMean]
    exclusions: list[VisitExclusion]


class CohortTimeSeries(_Base):
    """Time-series result for one cohort.

    :param cohort_id: Echoed from the corresponding ``CohortInput.id``.
    :param n_patients: Distinct patients whose MRN resolved to a patient ID.
    :param n_visits: (patient, admission) entries that reached bucketing.
    :param visits: **Every** entry that entered the pipeline, including those
        later excluded, in request order. Indices are assigned in a pass that
        completes before any exclusion runs, so an index is simply the entry's
        position in the request and cannot shift when an earlier entry is
        dropped. Consequently ``len(visits) >= n_visits`` — the difference is
        the entries dropped before bucketing — and ``len(visits)`` is not a
        patient count.
    :param patient_exclusions: Entries dropped before bucketing, removing them
        from every interval. Recorded once here rather than repeated in all
        ``n`` intervals.
    :param intervals: The bucket series, dense and in ascending index order.
    """

    cohort_id: int
    n_patients: int
    n_visits: int
    visits: list[VisitInfo]
    patient_exclusions: list[VisitExclusion]
    intervals: list[IntervalResult]


class TimeSeriesResponse(_Base):
    """Response body for ``POST /cohorts/timeseries``."""

    cohorts: list[CohortTimeSeries]
