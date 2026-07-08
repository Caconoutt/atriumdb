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

"""Per-cohort aggregate statistics computation (Priority S2).

Entry point: :func:`compute_aggregate_statistics`.

Processing pipeline per cohort:
  3a. MRN → patient_id resolution via ``sdk.get_patient_id``
  3b. Observation window = [admission_ns, admission_ns + observation_window]
  3c. patient_id + window → device_id via ``sdk.convert_patient_to_device_id``
  3d. Availability check via ``sdk.get_interval_array``
   4. Value extraction via ``sdk.get_data`` + NaN removal + per-patient mean

Every patient excluded at any stage is written to the exclusions logger so
results can be audited without re-running. All log lines are prefixed with
``[request_id]`` for correlation across a single request.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

from atriumdb.dashboard.schemas import (
    AggregateStatisticsRequest,
    AggregateStatisticsResponse,
    CohortInput,
    CohortStatistics,
    PatientResult,
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

    Resolves the requested measure once, then iterates through each cohort,
    applying patient-ID resolution, device resolution, availability filtering,
    and per-patient mean computation in sequence.

    :param sdk: AtriumSDK instance in direct-DB mode.
    :param request: Parsed :class:`~atriumdb.dashboard.schemas.AggregateStatisticsRequest`.
    :param request_id: Correlation ID prepended to every log and exclusion record
        for this request.
    :return: :class:`~atriumdb.dashboard.schemas.AggregateStatisticsResponse`
        with one :class:`~atriumdb.dashboard.schemas.CohortStatistics` per input cohort.
    :raises ValueError: If the requested measure does not exist in the dataset.
    """
    measure_id = _resolve_measure_id(sdk, request, request_id)

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
# Step 2 — Measure resolution
# ---------------------------------------------------------------------------

def _resolve_measure_id(
    sdk: "AtriumSDK",
    request: AggregateStatisticsRequest,
    request_id: str,
) -> int:
    """Resolve the measure to an internal measure_id; raise if not found."""
    m = request.measure
    measure_id = sdk.get_measure_id(
        m.measure_tag,
        freq=m.freq,
        units=m.units,
        freq_units=m.freq_units,
    )
    if measure_id is None:
        raise ValueError(
            f"[{request_id}] Measure not found in dataset: tag='{m.measure_tag}', "
            f"freq={m.freq} {m.freq_units}, units='{m.units}'"
        )
    _LOGGER.debug(
        "[%s] Resolved measure '%s' (freq=%s %s, units=%s) -> measure_id=%d",
        request_id, m.measure_tag, m.freq, m.freq_units, m.units, measure_id,
    )
    return measure_id


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
    n_excluded = 0
    patient_results: list[PatientResult] = []

    # Step 3a — resolve MRN → patient_id
    mrn_to_pid = _resolve_patient_ids(sdk, cohort, request_id)
    n_candidates = len(mrn_to_pid)

    _LOGGER.debug(
        "[%s] Cohort %s: %d/%d MRNs resolved to patient IDs.",
        request_id, cohort_id, n_candidates, len(cohort.patients),
    )

    for mrn, patient_id in mrn_to_pid.items():
        admission_ns = next(
            p.admission_ns for p in cohort.patients if p.mrn == mrn
        )

        # Step 3b — observation window
        window_start_ns = admission_ns
        window_end_ns = admission_ns + request.observation_window

        # Step 3c — device resolution
        device_id = sdk.convert_patient_to_device_id(
            start_time=window_start_ns,
            end_time=window_end_ns,
            patient_id=patient_id,
        )
        if device_id is None:
            _log_exclusion(
                request_id=request_id,
                cohort_id=cohort_id,
                mrn=mrn,
                patient_id=patient_id,
                reason="no_device_found",
                window=(window_start_ns, window_end_ns),
            )
            n_excluded += 1
            continue

        # Step 3d — availability check
        interval_arr = sdk.get_interval_array(
            measure_id=measure_id,
            device_id=device_id,
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
            _log_exclusion(
                request_id=request_id,
                cohort_id=cohort_id,
                mrn=mrn,
                patient_id=patient_id,
                reason="below_availability_threshold",
                window=(window_start_ns, window_end_ns),
                availability=availability,
                threshold=request.availability_threshold,
            )
            n_excluded += 1
            continue

        # Step 4 — value extraction and per-patient mean
        result = _extract_patient_mean(
            sdk=sdk,
            measure_id=measure_id,
            device_id=device_id,
            patient_id=patient_id,
            mrn=mrn,
            cohort_id=cohort_id,
            request_id=request_id,
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
        )
        if result is None:
            n_excluded += 1
            continue

        patient_results.append(PatientResult(mrn=mrn, mean=result))

    _LOGGER.debug(
        "[%s] Cohort %s: %d included, %d excluded of %d candidates.",
        request_id, cohort_id, len(patient_results), n_excluded, n_candidates,
    )

    return CohortStatistics(
        cohort_id=cohort_id,
        n_candidates=n_candidates,
        n_included=len(patient_results),
        n_excluded=n_excluded,
        patient_results=patient_results,
    )


# ---------------------------------------------------------------------------
# Step 3a helper — patient ID resolution
# ---------------------------------------------------------------------------

def _resolve_patient_ids(
    sdk: "AtriumSDK",
    cohort: CohortInput,
    request_id: str,
) -> dict[str, int]:
    """Return {mrn: patient_id} for MRNs that resolve; log and skip the rest."""
    result: dict[str, int] = {}
    for patient in cohort.patients:
        mrn = patient.mrn
        patient_id = sdk.get_patient_id(mrn=mrn)
        if patient_id is None:
            _log_exclusion(
                request_id=request_id,
                cohort_id=cohort.id,
                mrn=mrn,
                patient_id=None,
                reason="mrn_not_found",
            )
            continue
        result[mrn] = patient_id
    return result


# ---------------------------------------------------------------------------
# Step 4 helper — value extraction
# ---------------------------------------------------------------------------

def _extract_patient_mean(
    sdk: "AtriumSDK",
    measure_id: int,
    device_id: int,
    patient_id: int,
    mrn: str,
    cohort_id: int,
    request_id: str,
    window_start_ns: int,
    window_end_ns: int,
) -> float | None:
    """Fetch signal values and return the per-patient mean, or None if excluded."""
    _, _, values = sdk.get_data(
        measure_id=measure_id,
        device_id=device_id,
        patient_id=patient_id,
        start_time_n=window_start_ns,
        end_time_n=window_end_ns,
    )

    if values is None:
        values = np.array([])

    values = values[~np.isnan(values)]

    if len(values) == 0:
        _log_exclusion(
            request_id=request_id,
            cohort_id=cohort_id,
            mrn=mrn,
            patient_id=patient_id,
            reason="empty_values_after_nan_drop",
            window=(window_start_ns, window_end_ns),
        )
        return None

    return float(np.mean(values))


# ---------------------------------------------------------------------------
# Exclusion logging
# ---------------------------------------------------------------------------

def _log_exclusion(
    request_id: str,
    cohort_id: int,
    mrn: str,
    patient_id: int | None,
    reason: str,
    window: tuple[int, int] | None = None,
    availability: float | None = None,
    threshold: float | None = None,
) -> None:
    """Write a structured exclusion record to the exclusions logger."""
    parts = [
        f"[{request_id}]",
        f"[EXCLUDED] cohort_id={cohort_id}",
        f"mrn={mrn}",
        f"patient_id={patient_id}",
        f"reason={reason}",
    ]
    if window is not None:
        parts.append(f"window=[{window[0]}, {window[1]}]")
    if availability is not None:
        parts.append(f"availability={availability:.4f}")
    if threshold is not None:
        parts.append(f"threshold={threshold}")
    _EXCLUSION_LOGGER.warning("  ".join(parts))
