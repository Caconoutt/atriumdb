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
  3c. patient_id + window → device_id via ``sdk.convert_patient_to_device_id``
  3d. Availability check via ``sdk.get_interval_array``
   4. Value extraction via ``sdk.get_data`` + NaN removal + per-patient mean

Every patient excluded at any stage is written to the exclusions logger so
results can be audited without re-running. All log lines are prefixed with
``[request_id]`` for correlation across a single request.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import numpy as np

from atriumdb_dashboard.schemas import (
    ALL_TIME,
    Admission,
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
    iterates through each cohort, applying patient-ID resolution, device
    resolution, availability filtering, and per-patient mean computation in
    sequence.

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
    patient_results: list[PatientResult] = []
    exclusions: list[ExclusionRecord] = []

    value_range = _resolve_value_range(cohort, request, request_id)

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
            window = _observation_window(admission, request.observation_window)
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

            # Step 3c — device resolution
            device_id = sdk.convert_patient_to_device_id(
                start_time=window_start_ns,
                end_time=window_end_ns,
                patient_id=patient_id,
            )
            if device_id is None:
                # print(f"[DEBUG] mrn={mrn}  admission_ns={admission_ns}  -> EXCLUDED: no_device_found")
                exclusions.append(_make_exclusion(
                    request_id=request_id,
                    cohort_id=cohort_id,
                    mrn=mrn,
                    reason=ExclusionReason.NO_DEVICE_FOUND,
                    admission_ns=admission_ns,
                    window=(window_start_ns, window_end_ns),
                ))
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
                # print(f"[DEBUG] mrn={mrn}  admission_ns={admission_ns}  device_id={device_id}  availability={availability:.3f}  -> EXCLUDED: below_availability_threshold")
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

            # Step 4 — value extraction, value-range filtering, and per-entry mean
            mean, reason, post_filter_availability = _extract_patient_mean(
                sdk=sdk,
                measure_id=measure_id,
                device_id=device_id,
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
            sex, age_months = _fetch_demographics(
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
    """Return {mrn: patient_id} for MRNs that resolve; append exclusions for the rest."""
    result: dict[str, int] = {}
    for patient in cohort.patients:
        mrn = patient.mrn
        patient_id = sdk.get_patient_id(mrn=mrn)
        if patient_id is None:
            exclusions.append(_make_exclusion(
                request_id=request_id,
                cohort_id=cohort.id,
                mrn=mrn,
                reason=ExclusionReason.MRN_NOT_FOUND,
            ))
            continue
        result[mrn] = patient_id
    return result


# ---------------------------------------------------------------------------
# Step 3b helper — observation window
# ---------------------------------------------------------------------------

def _observation_window(
    admission: Admission,
    observation_window: int | str,
) -> tuple[int, int] | None:
    """Return ``(start_ns, end_ns)`` for this admission, or None if unbounded.

    A fixed window runs for ``observation_window`` nanoseconds from the
    admission. Under ``"all_time"`` it instead spans the admission itself, so
    the stay's own discharge is what ends it.

    :return: The window, or ``None`` when ``"all_time"`` was requested but this
        admission has no usable discharge — an open stay, or a discharge that
        does not follow the admission. Such an entry has no window to measure
        availability against, and the caller excludes it.
    """
    if observation_window != ALL_TIME:
        return admission.admission_ns, admission.admission_ns + observation_window

    if admission.discharge_ns is None or admission.discharge_ns <= admission.admission_ns:
        return None

    return admission.admission_ns, admission.discharge_ns


# ---------------------------------------------------------------------------
# Value-range resolution
# ---------------------------------------------------------------------------

def _resolve_value_range(
    cohort: CohortInput,
    request: AggregateStatisticsRequest,
    request_id: str,
) -> ValueRange | None:
    """Return the bounds in force for this cohort, or None if the signal is unbounded.

    Both maps are keyed by measure tag, and only the tag named by the request's
    ``measure`` is ever consulted — bounds keyed by any other tag do not apply.

    When a cohort and the global request both bound the tag, the two are
    intersected rather than one replacing the other: the tighter of the two
    bounds wins at each end independently, so a value must satisfy both to
    count. An end left open (``None``) constrains nothing, so the other side's
    bound carries. When only one of the two is present it applies on its own.
    """
    tag = request.measure.measure_tag

    global_range = (request.value_range or {}).get(tag)
    cohort_range = (cohort.value_range or {}).get(tag)

    lowers = [r.lower for r in (global_range, cohort_range) if r is not None and r.lower is not None]
    uppers = [r.upper for r in (global_range, cohort_range) if r is not None and r.upper is not None]

    # Tighter bound wins at each end: the highest floor, the lowest ceiling.
    lower = max(lowers) if lowers else None
    upper = min(uppers) if uppers else None

    if lower is None and upper is None:
        _LOGGER.debug(
            "[%s] Cohort %s: no value range in force for tag '%s' — signal unbounded.",
            request_id, cohort.id, tag,
        )
        return None

    _LOGGER.debug(
        "[%s] Cohort %s: value range for tag '%s' (global=%s, cohort=%s) -> lower=%s, upper=%s",
        request_id, cohort.id, tag, global_range, cohort_range, lower, upper,
    )
    return ValueRange(lower=lower, upper=upper)


# ---------------------------------------------------------------------------
# Demographics — sex and age-at-admission for the Data Records table
# ---------------------------------------------------------------------------

def _age_months(dob_ns: int, admission_ns: int) -> int | None:
    """Whole months elapsed from ``dob_ns`` to ``admission_ns`` (3y 4m -> 40).

    Counted on the calendar rather than by dividing a nanosecond span, so month
    lengths don't accumulate drift. The final month only counts once the day of
    the month is reached.
    """
    dob = datetime.fromtimestamp(dob_ns / 1e9, tz=timezone.utc)
    admitted = datetime.fromtimestamp(admission_ns / 1e9, tz=timezone.utc)

    months = (admitted.year - dob.year) * 12 + (admitted.month - dob.month)
    if admitted.day < dob.day:
        months -= 1

    # A dob after the admission means the record is inconsistent; report unknown
    # rather than a negative age.
    return months if months >= 0 else None


def _fetch_demographics(
    sdk: "AtriumSDK",
    patient_id: int,
    mrn: str,
    admission_ns: int,
    request_id: str,
) -> tuple[str | None, int | None]:
    """Return ``(sex, age_months)`` as of this admission; either may be None.

    Demographics are best-effort: a dataset that does not record gender or dob
    yields ``None``, which the dashboard renders as an em-dash. Missing values
    never exclude an entry.
    """
    info = sdk.get_patient_info(patient_id=patient_id, time=admission_ns)
    if info is None:
        _LOGGER.debug(
            "[%s] mrn=%s: get_patient_info returned no record at admission_ns=%d.",
            request_id, mrn, admission_ns,
        )
        return None, None

    sex = info.get("gender") or None

    dob_ns = info.get("dob")
    age_months = None if dob_ns is None else _age_months(dob_ns, admission_ns)

    return sex, age_months


# ---------------------------------------------------------------------------
# Step 4 helper — value extraction
# ---------------------------------------------------------------------------

def _extract_patient_mean(
    sdk: "AtriumSDK",
    measure_id: int,
    device_id: int,
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
            device_id=device_id,
            patient_id=patient_id,
            start_time_n=window_start_ns,
            end_time_n=window_end_ns,
        )
        if values is None:
            values = np.array([])
        values = values[~np.isnan(values)]
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
        device_id=device_id,
        patient_id=patient_id,
        start_time_n=window_start_ns,
        end_time_n=window_end_ns,
        return_nan_filled=True,
    )
    if values is None or len(values) == 0:
        return None, ExclusionReason.NO_USABLE_VALUES, None

    usable = ~np.isnan(values)
    if value_range.lower is not None:
        usable &= values >= value_range.lower
    if value_range.upper is not None:
        usable &= values <= value_range.upper

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
