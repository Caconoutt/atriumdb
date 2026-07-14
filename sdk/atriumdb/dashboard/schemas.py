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

"""Pydantic schemas for the S2 aggregate statistics endpoint.

``PatientAdmission`` is defined here as the element type of ``CohortInput.patients``
— it carries the MRN and admission anchor passed in from the cohort resolver output.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, PositiveInt
from pydantic.alias_generators import to_camel

#: Sentinel value for ``AggregateStatisticsRequest.observation_window``: instead of
#: a fixed span, each entry's window runs from its admission to its discharge.
ALL_TIME = "all_time"


class ExclusionReason(str, Enum):
    MRN_NOT_FOUND = "mrn_not_found"
    NO_DEVICE_FOUND = "no_device_found"
    BELOW_AVAILABILITY_THRESHOLD = "below_availability_threshold"
    NO_USABLE_VALUES = "no_usable_values"
    MISSING_DISCHARGE_TIME = "missing_discharge_time"


class _Base(BaseModel):
    """Shared config: snake_case in Python, camelCase in JSON."""
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


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


#: Value bounds keyed by AtriumDB measure tag, e.g.
#: ``{"MDC_ECG_CARD_BEAT_RATE": {"lower": 40, "upper": 200}}``.
#:
#: Resolution order, per tag per cohort:
#:   1. the cohort's own ``value_range`` entry, if present;
#:   2. else the request-level (global) ``value_range`` entry, if present;
#:   3. else no filtering.
ValueRangeMap = dict[str, ValueRange]


class Admission(_Base):
    """One qualifying admission, with the location the patient occupied.

    Emitted by the cohort resolver (S1), which already joins ``encounter → bed
    → unit`` and therefore knows the unit name. Carrying it here means S2 never
    has to re-run that join, and a query spanning several locations (e.g. ICU
    *and* OR) can still report which one each patient was actually in.

    A stay that transfers between units produces one ``Admission`` per unit
    rather than one record with an ambiguous set of locations.

    :param admission_ns: Encounter start time in Unix epoch nanoseconds. This
        is the anchor for the observation window and, with ``mrn``, the unique
        key of a pipeline entry.
    :param discharge_ns: Encounter end time in Unix epoch nanoseconds; ``None``
        when the stay is still open.
    :param location: Unit name for this admission, e.g. ``"ICU"`` or ``"OR"``.
        ``None`` when the resolver could not determine it.
    """

    admission_ns: int
    discharge_ns: int | None = None
    location: str | None = None


class PatientAdmission(_Base):
    """A single patient with all their qualifying admissions.

    :param mrn: Medical record number.
    :param admissions: All qualifying admissions, sorted ascending by
        ``admission_ns``. A patient with multiple in-range visits will have
        multiple entries here; each is processed as a distinct entry in the
        statistics pipeline.
    """

    mrn: str
    admissions: list[Admission]


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
        carried through from the dashboard query and overriding the request's
        global ``value_range`` for this cohort. Omitted entirely when the cohort
        set no bounds of its own, in which case the global range applies.
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
        availability rather than being averaged in. A cohort's own
        ``value_range`` supersedes this for that cohort. Omitted when the query
        set no global bounds.
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
