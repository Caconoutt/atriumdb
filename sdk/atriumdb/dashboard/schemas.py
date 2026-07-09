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

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ExclusionReason(str, Enum):
    MRN_NOT_FOUND = "mrn_not_found"
    NO_DEVICE_FOUND = "no_device_found"
    BELOW_AVAILABILITY_THRESHOLD = "below_availability_threshold"
    NO_USABLE_VALUES = "no_usable_values"


class _Base(BaseModel):
    """Shared config: snake_case in Python, camelCase in JSON."""
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class PatientAdmission(_Base):
    """A single patient with all their qualifying admission timestamps.

    :param mrn: Medical record number.
    :param admissions: All qualifying encounter start times in Unix epoch
        nanoseconds, sorted ascending. A patient with multiple in-range visits
        will have multiple entries here; each is processed as a distinct entry
        in the statistics pipeline.
    """

    mrn: str
    admissions: list[int]


class MeasureIdentifier(_Base):
    """Identifies a physiological signal measure in AtriumDB.

    All four fields are passed to ``sdk.get_measure_id()`` to resolve the
    internal integer measure ID. Returns ``None`` if no matching measure exists.
    """

    measure_tag: str
    freq: float
    units: str
    freq_units: str


class CohortInput(_Base):
    """A pre-resolved cohort passed to the statistics endpoint.

    Typically built from the output of the cohort resolver (S1). Each patient
    already has a verified MRN and admission anchor.

    :param id: Integer cohort identifier.
    :param patients: Pre-resolved patient list from the cohort resolver.
    """

    id: int
    patients: list[PatientAdmission]


class AggregateStatisticsRequest(_Base):
    """Request body for ``POST /cohort/statistics``.

    :param cohorts: One entry per cohort, each containing the pre-resolved
        patient list from the cohort resolver.
    :param measure: Identifies the signal to analyse.
    :param observation_window: Length of the observation window in epoch
        nanoseconds, anchored at each patient's ``admission_ns``
        (e.g. 24 h = ``86_400_000_000_000``).
    :param availability_threshold: Minimum fraction ``[0, 1]`` of the window
        that must be covered by valid data for a patient to be included.
        Defaults to ``0.80``.
    """

    cohorts: list[CohortInput]
    measure: MeasureIdentifier
    observation_window: int
    availability_threshold: float = 0.80


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
        ``mrn_not_found``.
    :param window_end_ns: Observation window end; ``None`` for
        ``mrn_not_found``.
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

    :param mrn: Patient identifier, carried through for export.
    :param admission_ns: The specific admission this result is anchored to.
        Distinguishes multiple entries for the same patient.
    :param mean: Mean of signal values over the observation window after
        NaN removal.
    """

    mrn: str
    admission_ns: int
    mean: float


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
