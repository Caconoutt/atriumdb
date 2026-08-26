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

"""Per-cohort per-interval time-series computation.

Entry point: :func:`compute_cohort_timeseries`.

Takes the same cohort input as the statistics endpoint, but instead of one
summary per patient over the whole observation window, it chops each patient's
window into fixed-width intervals and reports a per-patient mean for each, so
the client can plot a cohort's signal over time.

Processing pipeline per cohort:
  1. MRN → patient_id resolution (shared, :mod:`atriumdb_dashboard.pipeline`)
  2. Pass 1 — build the ``visits`` table over every entry, before any exclusion
     runs, so a visit index is simply the entry's position in the request
  3. Pass 2 — per entry: one NaN-filled ``sdk.get_data`` call over the whole
     window, then bucket the returned grid in memory

Two differences from the statistics endpoint are load-bearing:

* **No window-level availability gate.** ``availability_threshold`` is applied
  per interval and only per interval. An entry sparse across the window as a
  whole still reaches bucketing and carries a real mean in whichever intervals
  it does cover. This is why S2's ``_extract_patient_mean`` is not reused —
  it contains exactly that gate.
* **Exclusions land in two places.** A drop that removes the whole entry
  (``mrn_not_found``) is recorded once in ``patient_exclusions``; a drop
  specific to one bucket is recorded in that bucket. Recording an entry-level
  drop per interval would repeat it once per bucket.
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
    Admission,
    CohortInput,
    CohortTimeSeries,
    ExclusionReason,
    IntervalResult,
    TimeSeriesRequest,
    TimeSeriesResponse,
    ValueRange,
    VisitExclusion,
    VisitInfo,
    VisitMean,
)

if TYPE_CHECKING:
    from atriumdb import AtriumSDK

_LOGGER = logging.getLogger(__name__)

# Its own child logger, mirroring the statistics resolver, so the two endpoints'
# audit streams can be routed to separate files.
_EXCLUSION_LOGGER = logging.getLogger(__name__ + ".exclusions")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_cohort_timeseries(
    sdk: "AtriumSDK",
    request: TimeSeriesRequest,
    request_id: str,
) -> TimeSeriesResponse:
    """Compute per-cohort per-interval per-patient mean signal values.

    The single entry point for S3. Resolves the requested measure once, then
    processes each cohort independently.

    Exclusions are written to the
    ``atriumdb_dashboard.timeseries_resolver.exclusions`` logger. To capture
    them in a file, attach a ``FileHandler`` to that logger before calling this
    function. Interval-level drops are summarised one line per visit rather than
    one per bucket — a 24 h / 5 min request over a few hundred visits would
    otherwise emit tens of thousands of lines for a single request.

    :param sdk: AtriumSDK instance in direct-DB mode.
    :param request: Parsed :class:`~atriumdb_dashboard.schemas.TimeSeriesRequest`.
        Its validators have already guaranteed that ``interval_ns`` divides
        ``observation_window`` evenly and that the bucket count is within
        ``MAX_INTERVALS``.
    :param request_id: Correlation ID prepended to every log and exclusion record.
    :return: :class:`~atriumdb_dashboard.schemas.TimeSeriesResponse` with one
        :class:`~atriumdb_dashboard.schemas.CohortTimeSeries` per input cohort.
    :raises ValueError: If ``request_id`` is missing or empty, if the requested
        measure does not exist in the dataset, or if it is aperiodic.
    """
    if not request_id:
        _LOGGER.error(
            "compute_cohort_timeseries called with missing or empty request_id "
            "— every request must supply a non-empty request_id."
        )
        raise ValueError("request_id must be a non-empty string.")

    measure_id = resolve_measure_id(sdk, request.measure, request_id)
    _reject_aperiodic_measure(sdk, measure_id, request, request_id)

    n_intervals = request.observation_window // request.interval_ns
    _LOGGER.debug(
        "[%s] Time-series over %d intervals of %d ns (window %d ns), %d cohorts.",
        request_id, n_intervals, request.interval_ns,
        request.observation_window, len(request.cohorts),
    )

    cohort_results = [
        _process_cohort(sdk, cohort, measure_id, n_intervals, request, request_id)
        for cohort in request.cohorts
    ]

    _LOGGER.debug(
        "[%s] compute_cohort_timeseries complete: %d cohorts processed.",
        request_id, len(cohort_results),
    )
    return TimeSeriesResponse(cohorts=cohort_results)


# ---------------------------------------------------------------------------
# Measure suitability
# ---------------------------------------------------------------------------

def _reject_aperiodic_measure(
    sdk: "AtriumSDK",
    measure_id: int,
    request: TimeSeriesRequest,
    request_id: str,
) -> None:
    """Raise if the measure has no sampling period, so no regular grid exists.

    Aperiodic and annotation signals are stored with ``freq_nhz = 0``. They have
    no period, so ``return_nan_filled=True`` has nothing to fill against — the
    SDK derives the grid length as ``(end - start) / period_ns`` and divides by
    zero. Bucketing such a measure is not merely unsupported, it is undefined:
    there is no notion of "the fraction of this bucket that is covered" without
    an expected sample count.

    Caught here rather than in ``pipeline`` because the statistics endpoint is
    unaffected — its unbounded path never NaN-fills.
    """
    try:
        info = sdk.get_measure_info(measure_id)
    except ZeroDivisionError:
        # freq_nhz == 0 with no stored period_ns: get_measure_info computes
        # 10**18 // freq_nhz and fails before returning anything to inspect.
        info = None

    if info is not None and info.get("freq_nhz"):
        return

    raise ValueError(
        f"[{request_id}] Measure '{request.measure.measure_tag}' is aperiodic "
        f"(freq_nhz = 0), so it has no sampling period to bucket against. The "
        f"time-series endpoint supports periodic measures only."
    )


# ---------------------------------------------------------------------------
# Per-cohort processing
# ---------------------------------------------------------------------------

def _cohort_entries(cohort: CohortInput) -> list[tuple[str, Admission]]:
    """Flatten the cohort into ``(mrn, admission)`` entries in request order.

    This ordering *is* the visit index: entry ``v`` becomes ``visits[v]``. It is
    computed once, up front, over the raw request, so no later exclusion can
    shift it.

    A patient carrying no admissions contributes no entries, matching the
    statistics endpoint — such a patient has no window to anchor and no
    exclusion reason in the enum honestly describes it, so it is passed over the
    same way there.
    """
    return [
        (patient.mrn, admission)
        for patient in cohort.patients
        for admission in patient.admissions
    ]


def _process_cohort(
    sdk: "AtriumSDK",
    cohort: CohortInput,
    measure_id: int,
    n_intervals: int,
    request: TimeSeriesRequest,
    request_id: str,
) -> CohortTimeSeries:
    """Process one cohort into its dense interval series."""
    cohort_id = cohort.id
    entries = _cohort_entries(cohort)

    value_range = resolve_value_range(
        cohort, request.measure.measure_tag, request.value_range, request_id
    )
    mrn_to_pid = resolve_patient_ids(sdk, cohort)
    n_patients = len(mrn_to_pid)

    # PASS 1 — the visit table, built over every entry before any exclusion runs.
    # Appending only for entries that survive would make an index depend on how
    # many earlier entries were dropped, silently shifting every later reference.
    visits = [
        _build_visit(sdk, mrn, admission, mrn_to_pid.get(mrn), request_id)
        for mrn, admission in entries
    ]

    patient_exclusions: list[VisitExclusion] = []
    # One accumulator pair per interval; every entry that reaches bucketing
    # contributes exactly one record to each interval, either a mean or a drop.
    interval_means: list[list[VisitMean]] = [[] for _ in range(n_intervals)]
    interval_drops: list[list[VisitExclusion]] = [[] for _ in range(n_intervals)]

    # PASS 2 — process; every record refers back by index.
    n_visits = 0
    for visit, (mrn, admission) in enumerate(entries):
        patient_id = mrn_to_pid.get(mrn)
        if patient_id is None:
            patient_exclusions.append(_make_visit_exclusion(
                request_id=request_id,
                cohort_id=cohort_id,
                mrn=mrn,
                visit=visit,
                reason=ExclusionReason.MRN_NOT_FOUND,
            ))
            continue

        n_visits += 1

        # A fixed observation window always bounds, so this never returns None —
        # the "all_time" branch that can is unreachable from this endpoint,
        # TimeSeriesRequest.observation_window being a PositiveInt.
        window_start_ns, window_end_ns = compute_observation_window(
            admission, request.observation_window
        )

        values = _fetch_window_values(
            sdk=sdk,
            measure_id=measure_id,
            patient_id=patient_id,
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
            request_id=request_id,
        )

        _bucket_visit(
            visit=visit,
            values=values,
            value_range=value_range,
            availability_threshold=request.availability_threshold,
            n_intervals=n_intervals,
            interval_means=interval_means,
            interval_drops=interval_drops,
            cohort_id=cohort_id,
            mrn=mrn,
            admission_ns=admission.admission_ns,
            request_id=request_id,
        )

    intervals = [
        IntervalResult(
            interval_index=i,
            start_offset_ns=i * request.interval_ns,
            end_offset_ns=(i + 1) * request.interval_ns,
            n_included=len(interval_means[i]),
            n_excluded=len(interval_drops[i]),
            patient_results=interval_means[i],
            exclusions=interval_drops[i],
        )
        for i in range(n_intervals)
    ]

    _LOGGER.debug(
        "[%s] Cohort %s: %d distinct patients, %d/%d entries bucketed into %d "
        "intervals (%d dropped before bucketing).",
        request_id, cohort_id, n_patients, n_visits, len(entries),
        n_intervals, len(patient_exclusions),
    )

    return CohortTimeSeries(
        cohort_id=cohort_id,
        n_patients=n_patients,
        n_visits=n_visits,
        visits=visits,
        patient_exclusions=patient_exclusions,
        intervals=intervals,
    )


def _build_visit(
    sdk: "AtriumSDK",
    mrn: str,
    admission: Admission,
    patient_id: int | None,
    request_id: str,
) -> VisitInfo:
    """Build one ``visits`` row, looking demographics up once for the whole stay.

    Demographics are at-admission and constant for the stay, so this runs once
    per entry rather than once per interval — normalising the response removes
    the redundant work from assembly, not just from the wire.

    An entry whose MRN did not resolve has no patient to look up, so its
    demographics stay null. It still gets a row: ``patient_exclusions``
    references it by index, so every entry must be addressable.
    """
    if patient_id is None:
        sex, age_months = None, None
    else:
        sex, age_months = fetch_demographics(
            sdk=sdk,
            patient_id=patient_id,
            mrn=mrn,
            admission_ns=admission.admission_ns,
            request_id=request_id,
        )

    return VisitInfo(
        mrn=mrn,
        admission_ns=admission.admission_ns,
        sex=sex,
        age_months=age_months,
        # Carried through from the cohort resolver rather than re-queried: the
        # encounter -> unit join already happened upstream.
        location=admission.location,
    )


# ---------------------------------------------------------------------------
# Data fetch — one call per entry, covering every interval
# ---------------------------------------------------------------------------

def _fetch_window_values(
    sdk: "AtriumSDK",
    measure_id: int,
    patient_id: int,
    window_start_ns: int,
    window_end_ns: int,
    request_id: str,
) -> np.ndarray:
    """Fetch the whole window as one regular, NaN-filled sample grid.

    ``return_nan_filled=True`` returns a **2-tuple** ``(headers, values)`` rather
    than the usual 3-tuple, where ``values`` spans the full window at the
    measure's nominal period with gaps filled as NaN. Two things follow, and
    together they are why this endpoint makes one SDK data call per entry rather
    than one per bucket:

    * The grid is regular, so a sample's interval is pure integer arithmetic on
      its index — no per-sample timestamp lookup, and no timestamps are returned
      in this mode anyway.
    * Coverage *is* the non-NaN fraction of the grid, so per-interval
      availability falls out of the same array as the means. There is no need to
      ask ``get_interval_array`` how much data exists and then re-derive
      availability from the values; the second computation subsumes the first.

    :return: 1D float64 array, empty when the SDK returned nothing.
    """
    _, values = sdk.get_data(
        measure_id=measure_id,
        patient_id=patient_id,
        start_time_n=window_start_ns,
        end_time_n=window_end_ns,
        return_nan_filled=True,
    )
    # Guarded rather than relying on lazy %s formatting: lazy formatting defers
    # the % operation, not the evaluation of the arguments, so array2string would
    # build its ~1 MB string on every entry even with DEBUG off.
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "[%s] get_data measure_id=%s patient_id=%s window=[%s, %s] "
            "n_values=%s values=%s",
            request_id, measure_id, patient_id, window_start_ns, window_end_ns,
            "None" if values is None else len(values),
            # threshold=size defeats numpy's default summarisation, which would
            # otherwise render a 24 h grid as "[0. 1. 2. ... 86398. 86399.]".
            # max_line_width keeps it on one line so the record stays one line.
            "None" if values is None else np.array2string(
                values, threshold=values.size, max_line_width=10 ** 9,
            ),
        )
    if values is None:
        return np.array([], dtype=np.float64)
    # float64 explicitly: usable_mask calls np.isnan, which rejects integer
    # arrays, and a dataset storing an integer-typed measure would otherwise
    # fail deep inside the mask rather than here.
    return np.asarray(values, dtype=np.float64)


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------

def _interval_bounds(n_samples: int, n_intervals: int) -> np.ndarray:
    """Return the ``n_intervals + 1`` sample-index boundaries of the buckets.

    Boundaries are placed proportionally over the *actual* returned grid rather
    than derived from the measure's period. Every interval is exactly
    ``interval_ns`` wide (the request validator guarantees the window divides
    evenly) and the grid is uniform over the window, so boundary ``i`` is
    exactly ``i * n_samples / n_intervals``. Taking it from ``len(values)``
    means the partition cannot disagree with the array it indexes, whatever
    rounding the SDK used to size that array.

    Rounded half-up in pure integer arithmetic — a float intermediate would
    lose exactness once ``i * n_samples`` passes 2**53. The result is
    non-decreasing, starts at 0 and ends at ``n_samples``, so the buckets
    partition the grid exactly: contiguous, non-overlapping, no dropped tail.
    A bucket narrower than one sample simply comes out empty, which the caller
    reports as ``no_usable_values`` rather than dividing by zero.
    """
    index = np.arange(n_intervals + 1, dtype=np.int64)
    return (index * n_samples + n_intervals // 2) // n_intervals


def _bucket_visit(
    visit: int,
    values: np.ndarray,
    value_range: ValueRange | None,
    availability_threshold: float,
    n_intervals: int,
    interval_means: list[list[VisitMean]],
    interval_drops: list[list[VisitExclusion]],
    cohort_id: int,
    mrn: str,
    admission_ns: int,
    request_id: str,
) -> None:
    """Score one visit in every interval, appending into the interval accumulators.

    Exactly one record per interval is produced — a mean or a drop — so
    ``n_included + n_excluded`` is the same in every interval and equals the
    cohort's ``n_visits``.
    """
    n_samples = int(values.size)
    bounds = _interval_bounds(n_samples, n_intervals)
    sizes = np.diff(bounds)

    if n_samples == 0:
        # The SDK returned nothing for the whole window. Every bucket is empty;
        # record the drop rather than omitting the visit, so the per-interval
        # denominators stay constant.
        counts = np.zeros(n_intervals, dtype=np.int64)
        sums = np.zeros(n_intervals, dtype=np.float64)
    else:
        usable = usable_mask(values, value_range)
        # Each sample's bucket, by construction of the boundaries above.
        bucket_of_sample = np.repeat(np.arange(n_intervals, dtype=np.int64), sizes)
        selected = bucket_of_sample[usable]
        # bincount, not add.reduceat: reduceat returns the element at the index
        # rather than zero when two boundaries coincide, which is exactly what an
        # empty bucket looks like, and it fails silently.
        counts = np.bincount(selected, minlength=n_intervals)
        sums = np.bincount(
            selected, weights=values[usable], minlength=n_intervals
        )

    n_dropped = 0
    worst_availability: float | None = None

    for i in range(n_intervals):
        size = int(sizes[i])
        count = int(counts[i])

        if size == 0:
            # A bucket narrower than one sample of this measure. Nothing to
            # average and no denominator to measure coverage against.
            interval_drops[i].append(VisitExclusion(
                visit=visit, reason=ExclusionReason.NO_USABLE_VALUES,
            ))
            n_dropped += 1
            continue

        availability = count / size
        # Threshold before all-absent, matching the statistics endpoint: with any
        # positive threshold an all-absent bucket fails the threshold first, so
        # no_usable_values stays reachable only at threshold 0 in both endpoints.
        if availability < availability_threshold:
            interval_drops[i].append(VisitExclusion(
                visit=visit,
                reason=ExclusionReason.BELOW_AVAILABILITY_THRESHOLD,
                availability=availability,
            ))
            n_dropped += 1
            worst_availability = (
                availability if worst_availability is None
                else min(worst_availability, availability)
            )
            continue

        if count == 0:
            interval_drops[i].append(VisitExclusion(
                visit=visit, reason=ExclusionReason.NO_USABLE_VALUES,
            ))
            n_dropped += 1
            continue

        interval_means[i].append(VisitMean(
            visit=visit, mean=float(sums[i]) / count,
        ))

    if n_dropped:
        _log_interval_drops(
            request_id=request_id,
            cohort_id=cohort_id,
            mrn=mrn,
            visit=visit,
            admission_ns=admission_ns,
            n_dropped=n_dropped,
            n_intervals=n_intervals,
            worst_availability=worst_availability,
        )


# ---------------------------------------------------------------------------
# Exclusion record construction + logging
# ---------------------------------------------------------------------------

def _make_visit_exclusion(
    request_id: str,
    cohort_id: int,
    mrn: str,
    visit: int,
    reason: ExclusionReason,
    availability: float | None = None,
) -> VisitExclusion:
    """Build an entry-level VisitExclusion, log it, and return it.

    Entry-level only: these are few (one per unresolved MRN), so each gets its
    own line. Interval-level drops are summarised by
    :func:`_log_interval_drops` instead.

    The record itself carries only the visit index — the MRN is resolved by the
    client through ``visits[visit]`` — but the log line carries the MRN, since a
    bare index is not auditable without the response beside it.
    """
    record = VisitExclusion(visit=visit, reason=reason, availability=availability)
    parts = [
        f"[{request_id}]",
        f"[EXCLUDED] cohort_id={cohort_id}",
        f"mrn={mrn}",
        f"visit={visit}",
        f"reason={reason.value}",
    ]
    if availability is not None:
        parts.append(f"availability={availability:.4f}")
    _EXCLUSION_LOGGER.warning("  ".join(parts))
    return record


def _log_interval_drops(
    request_id: str,
    cohort_id: int,
    mrn: str,
    visit: int,
    admission_ns: int,
    n_dropped: int,
    n_intervals: int,
    worst_availability: float | None,
) -> None:
    """Log one summary line for a visit's interval-level drops.

    Deliberately one line per *visit*, not per bucket. A 24 h / 5 min request is
    288 intervals; a few hundred visits mostly failing their threshold would
    otherwise emit tens of thousands of warning lines for a single request and
    bury the entry-level exclusions that actually need attention. The
    per-interval detail is already in the response, which is what an audit
    reads.
    """
    parts = [
        f"[{request_id}]",
        f"[EXCLUDED-INTERVALS] cohort_id={cohort_id}",
        f"mrn={mrn}",
        f"visit={visit}",
        f"admission_ns={admission_ns}",
        f"dropped={n_dropped}/{n_intervals} intervals",
    ]
    if worst_availability is not None:
        parts.append(f"min_availability={worst_availability:.4f}")
    _EXCLUSION_LOGGER.warning("  ".join(parts))
