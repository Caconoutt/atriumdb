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

"""Per-cohort aggregate statistics computation.

Entry point: :func:`compute_aggregate_statistics`.

Processing pipeline per cohort:
  3a. MRN → patient_id resolution via ``sdk.get_patient_id``
  3b. Observation window = [admission_ns, admission_ns + observation_window], or
      [admission_ns, discharge_ns] when observation_window is "all_time"
  3c. Availability check via ``sdk.get_interval_array``
  3d. Value extraction via ``sdk.get_data`` + NaN removal + per-patient mean

Stages 3a, 3b, the value-range resolution and the demographics lookup are shared
with the time-series endpoint and live in :mod:`atriumdb_dashboard.pipeline`.
Stage 3c/3d stay here: the whole-window availability gate is exactly what S3
must not inherit, so it is deliberately not shared.

Every patient excluded at any stage is written to the exclusions logger so
results can be audited without re-running. All log lines are prefixed with
``[request_id]`` for correlation across a single request.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

from atriumdb_dashboard.pipeline import (
    compute_observation_window,
    fetch_demographics,
    resolve_measure_id,
    resolve_patient_ids,
    resolve_value_range,
    usable_mask,
)
from atriumdb_dashboard.schemas import (
    AggregateStatisticsRequest,
    AggregateStatisticsResponse,
    CohortInput,
    CohortStatistics,
    ExclusionReason,
    ExclusionRecord,
    PatientResult,
    ValueRange,
)

if TYPE_CHECKING:
    from atriumdb import AtriumSDK

_LOGGER = logging.getLogger(__name__)

# Structured exclusion records go to a child logger so callers can route them
# to a dedicated file handler without mixing with general debug output.
_EXCLUSION_LOGGER = logging.getLogger(__name__ + ".exclusions")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_aggregate_statistics(
    sdk: "AtriumSDK",
    request: AggregateStatisticsRequest,
    request_id: str,
) -> AggregateStatisticsResponse:
    """Compute per-cohort per-patient mean signal values.

    The single entry point for S2. Resolves the requested measure once, then
    iterates through each cohort, applying patient-ID resolution, availability
    filtering, and per-patient mean computation in sequence.

    Exclusions are written to the
    ``atriumdb_dashboard.statistics_resolver.exclusions`` logger. To capture
    them in a file, attach a ``FileHandler`` to that logger before calling this
    function.

    :param sdk: AtriumSDK instance in direct-DB mode.
    :param request: Parsed :class:`~atriumdb_dashboard.schemas.AggregateStatisticsRequest`.
    :param request_id: Correlation ID prepended to every log and exclusion record
        for this request.
    :return: :class:`~atriumdb_dashboard.schemas.AggregateStatisticsResponse`
        with one :class:`~atriumdb_dashboard.schemas.CohortStatistics` per input cohort.
    :raises ValueError: If ``request_id`` is missing or empty, or if the requested
        measure does not exist in the dataset.
    """
    if not request_id:
        _LOGGER.error(
            "compute_aggregate_statistics called with missing or empty "
            "request_id — every request must supply a non-empty request_id."
        )
        raise ValueError("request_id must be a non-empty string.")

    measure_id = resolve_measure_id(sdk, request.measure, request_id)

    cohort_results: list[CohortStatistics] = [
        _process_cohort(sdk, cohort, measure_id, request, request_id)
        for cohort in request.cohorts
    ]

    _LOGGER.debug(
        "[%s] compute_aggregate_statistics complete: %d cohorts processed.",
        request_id, len(cohort_results),
    )
    return AggregateStatisticsResponse(cohorts=cohort_results)


# ---------------------------------------------------------------------------
# Step 3 — Per-cohort processing
# ---------------------------------------------------------------------------

def _process_cohort(
    sdk: "AtriumSDK",
    cohort: CohortInput,
    measure_id: int,
    request: AggregateStatisticsRequest,
    request_id: str,
) -> CohortStatistics:
    """Process one cohort through the full pipeline and return its statistics."""
    cohort_id = cohort.id
    patient_results: list[PatientResult] = []
    exclusions: list[ExclusionRecord] = []

    value_range = resolve_value_range(
        cohort, request.measure.measure_tag, request.value_range, request_id
    )

    # Step 3a — resolve MRN → patient_id
    mrn_to_pid = _resolve_patient_ids(sdk, cohort, request_id, exclusions)
    n_patients = len(mrn_to_pid)

    # Each (patient, admission) pair is one entry in the pipeline.
    n_visits = sum(
        len(p.admissions) for p in cohort.patients if p.mrn in mrn_to_pid
    )

    _LOGGER.debug(
        "[%s] Cohort %s: %d/%d MRNs resolved → %d admission entries to process.",
        request_id, cohort_id, n_patients, len(cohort.patients), n_visits,
    )

    for patient in cohort.patients:
        mrn = patient.mrn
        patient_id = mrn_to_pid.get(mrn)
        if patient_id is None:
            continue

        if len(patient.admissions) > 1:
            _LOGGER.debug(
                "[%s] Cohort %s: mrn=%s has %d admissions — each processed as a distinct entry.",
                request_id, cohort_id, mrn, len(patient.admissions),
            )

        for admission in patient.admissions:
            admission_ns = admission.admission_ns

            # Step 3b — observation window for this admission
            window = compute_observation_window(admission, request.observation_window)
            if window is None:
                exclusions.append(_make_exclusion(
                    request_id=request_id,
                    cohort_id=cohort_id,
                    mrn=mrn,
                    reason=ExclusionReason.MISSING_DISCHARGE_TIME,
                    admission_ns=admission_ns,
                ))
                continue
            window_start_ns, window_end_ns = window

            # Step 3c — availability check
            interval_arr = sdk.get_interval_array(
                measure_id=measure_id,
                patient_id=patient_id,
                start=window_start_ns,
                end=window_end_ns,
            )

            if interval_arr is None or len(interval_arr) == 0:
                covered_ns = 0
            else:
                covered_ns = int(np.sum(interval_arr[:, 1] - interval_arr[:, 0]))

            observation_window_ns = window_end_ns - window_start_ns
            availability = covered_ns / observation_window_ns

            if availability < request.availability_threshold:
                # print(f"[DEBUG] mrn={mrn}  admission_ns={admission_ns}  availability={availability:.3f}  -> EXCLUDED: below_availability_threshold")
                exclusions.append(_make_exclusion(
                    request_id=request_id,
                    cohort_id=cohort_id,
                    mrn=mrn,
                    reason=ExclusionReason.BELOW_AVAILABILITY_THRESHOLD,
                    admission_ns=admission_ns,
                    window=(window_start_ns, window_end_ns),
                    availability=availability,
                ))
                continue

            # Step 3d — value extraction, value-range filtering, and per-entry mean
            mean, reason, post_filter_availability = _extract_patient_mean(
                sdk=sdk,
                measure_id=measure_id,
                patient_id=patient_id,
                window_start_ns=window_start_ns,
                window_end_ns=window_end_ns,
                value_range=value_range,
                availability_threshold=request.availability_threshold,
            )
            if reason is not None:
                exclusions.append(_make_exclusion(
                    request_id=request_id,
                    cohort_id=cohort_id,
                    mrn=mrn,
                    reason=reason,
                    admission_ns=admission_ns,
                    window=(window_start_ns, window_end_ns),
                    availability=post_filter_availability,
                ))
                continue

            # Demographics are only worth fetching for entries that survived the
            # filters, since they exist purely to populate the results table.
            sex, age_months = fetch_demographics(
                sdk=sdk,
                patient_id=patient_id,
                mrn=mrn,
                admission_ns=admission_ns,
                request_id=request_id,
            )

            patient_results.append(
                PatientResult(
                    mrn=mrn,
                    admission_ns=admission_ns,
                    mean=mean,
                    sex=sex,
                    age_months=age_months,
                    # Carried through from the resolver rather than re-queried:
                    # the encounter → unit join already happened upstream.
                    location=admission.location,
                )
            )

    n_excluded = len(exclusions)
    _LOGGER.debug(
        "[%s] Cohort %s: %d included, %d excluded of %d entries (%d distinct patients).",
        request_id, cohort_id, len(patient_results), n_excluded, n_visits, n_patients,
    )

    return CohortStatistics(
        cohort_id=cohort_id,
        n_patients=n_patients,
        n_visits=n_visits,
        n_included=len(patient_results),
        n_excluded=n_excluded,
        patient_results=patient_results,
        exclusions=exclusions,
    )


# ---------------------------------------------------------------------------
# Step 3a helper — patient ID resolution
# ---------------------------------------------------------------------------

def _resolve_patient_ids(
    sdk: "AtriumSDK",
    cohort: CohortInput,
    request_id: str,
    exclusions: list[ExclusionRecord],
) -> dict[str, int]:
    """Return {mrn: patient_id} for MRNs that resolve; append exclusions for the rest.

    The SDK loop itself is shared with the time-series endpoint
    (:func:`~atriumdb_dashboard.pipeline.resolve_patient_ids`); only the
    exclusion record it produces is S2-specific, which is why that half stays
    here. One record is appended per unresolved *patient entry*, in request
    order, so a duplicated bad MRN is reported once per occurrence.
    """
    resolved = resolve_patient_ids(sdk, cohort)
    for patient in cohort.patients:
        if patient.mrn not in resolved:
            exclusions.append(_make_exclusion(
                request_id=request_id,
                cohort_id=cohort.id,
                mrn=patient.mrn,
                reason=ExclusionReason.MRN_NOT_FOUND,
            ))
    return resolved


# ---------------------------------------------------------------------------
# Step 3d helper — value extraction
# ---------------------------------------------------------------------------

def _extract_patient_mean(
    sdk: "AtriumSDK",
    measure_id: int,
    patient_id: int,
    window_start_ns: int,
    window_end_ns: int,
    value_range: ValueRange | None,
    availability_threshold: float,
) -> tuple[float | None, ExclusionReason | None, float | None]:
    """Fetch window values, drop out-of-range samples, and return the per-entry mean.

    :return: ``(mean, None, None)`` when the entry is included, otherwise
        ``(None, reason, availability)``. ``availability`` is the recomputed
        post-filter coverage, and is set only when that recount is what dropped
        the entry.
    """
    if value_range is None:
        _, _, values = sdk.get_data(
            measure_id=measure_id,
            patient_id=patient_id,
            start_time_n=window_start_ns,
            end_time_n=window_end_ns,
        )
        if values is None:
            values = np.array([])
        values = values[usable_mask(values, None)]
        if len(values) == 0:
            return None, ExclusionReason.NO_USABLE_VALUES, None
        return float(np.mean(values)), None, None

    # Bounds are in force. Fetch the window NaN-filled so that every sample slot
    # the measure's frequency implies is represented: gaps arrive as NaN, and
    # out-of-range samples are marked absent alongside them. The usable fraction
    # of that array is the post-filter availability, which is why the check has
    # to be redone here — get_interval_array is value-blind and can only gate on
    # how much data exists, not on how much of it falls inside the bounds.
    _, values = sdk.get_data(
        measure_id=measure_id,
        patient_id=patient_id,
        start_time_n=window_start_ns,
        end_time_n=window_end_ns,
        return_nan_filled=True,
    )
    if values is None or len(values) == 0:
        return None, ExclusionReason.NO_USABLE_VALUES, None

    usable = usable_mask(values, value_range)

    availability = float(np.count_nonzero(usable)) / len(values)
    if availability < availability_threshold:
        return None, ExclusionReason.BELOW_AVAILABILITY_THRESHOLD, availability

    in_range = values[usable]
    if in_range.size == 0:
        return None, ExclusionReason.NO_USABLE_VALUES, None

    return float(np.mean(in_range)), None, None


# ---------------------------------------------------------------------------
# Exclusion record construction + logging
# ---------------------------------------------------------------------------

def _make_exclusion(
    request_id: str,
    cohort_id: int,
    mrn: str,
    reason: ExclusionReason,
    admission_ns: int | None = None,
    window: tuple[int, int] | None = None,
    availability: float | None = None,
) -> ExclusionRecord:
    """Build an ExclusionRecord, write it to the exclusions logger, and return it."""
    record = ExclusionRecord(
        mrn=mrn,
        admission_ns=admission_ns,
        reason=reason,
        window_start_ns=window[0] if window else None,
        window_end_ns=window[1] if window else None,
        availability=availability,
    )
    parts = [
        f"[{request_id}]",
        f"[EXCLUDED] cohort_id={cohort_id}",
        f"mrn={mrn}",
        f"reason={reason.value}",
    ]
    if admission_ns is not None:
        parts.append(f"admission_ns={admission_ns}")
    if window is not None:
        parts.append(f"window=[{window[0]}, {window[1]}]")
    if availability is not None:
        parts.append(f"availability={availability:.4f}")
    _EXCLUSION_LOGGER.warning("  ".join(parts))
    return record
