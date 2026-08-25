"""End-to-end run of the time-series resolver against a real AtriumDB dataset.

Skipped automatically when ``ATRIUMDB_DATASET_LOCATION`` is unset, so it is safe
to leave in the default test run. Fill in the constants below for the dataset in
hand — cohort membership, admission timestamps, the measure identifier, the
window, the interval and the threshold are all module-level.

The pass/fail assertion is deliberately loose. The value of this test is the two
artefacts it produces under ``-v -s``:

1. ``cohort_timeseries_real_data.log`` — the full JSON response, for eyeballing
   the shape the dashboard will actually receive.
2. The availability cross-check in
   :func:`test_timeseries_availability_matches_interval_array`, which is the
   only place the resolver's single-SDK-call design is validated against real
   waveform data. See the docstring there.
"""

import json
import os

import numpy as np
import pytest

from atriumdb import AtriumSDK
from atriumdb_dashboard.schemas import TimeSeriesRequest
from atriumdb_dashboard.timeseries_resolver import compute_cohort_timeseries

DATASET_LOCATION = os.environ.get("ATRIUMDB_DATASET_LOCATION")

pytestmark = pytest.mark.skipif(
    not DATASET_LOCATION,
    reason="ATRIUMDB_DATASET_LOCATION is not set; no dataset to run against.",
)

# ---------------------------------------------------------------------------
# Fill these in for the dataset under test
# ---------------------------------------------------------------------------

ONE_MIN_NS = 60 * 1_000_000_000

#: MRNs to pull into the cohort, with the admission timestamp to anchor each on.
#: One entry per (mrn, admission_ns) pair you want in the series.
COHORT_PATIENTS: list[tuple[str, int]] = [
    # ("1234567", 1_700_000_000_000_000_000),
]

MEASURE = {
    "measureTag": "SpO2",
    "freq": 1.0,
    "units": "%",
    "freqUnits": "Hz",
}

OBSERVATION_WINDOW_NS = 24 * 60 * ONE_MIN_NS   # 24 h
INTERVAL_NS = 5 * ONE_MIN_NS                   # 5 min buckets -> 288 intervals
AVAILABILITY_THRESHOLD = 0.80

REQUEST_ID = "real-data-timeseries-001"
OUTPUT_LOG = "cohort_timeseries_real_data.log"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sdk() -> AtriumSDK:
    return AtriumSDK(dataset_location=DATASET_LOCATION)


@pytest.fixture(scope="module")
def request_body() -> dict:
    if not COHORT_PATIENTS:
        pytest.skip("COHORT_PATIENTS is empty; fill it in for this dataset.")
    return {
        "cohorts": [{
            "id": 1,
            "patients": [
                {"mrn": mrn, "admissions": [{"admissionNs": admission_ns}]}
                for mrn, admission_ns in COHORT_PATIENTS
            ],
        }],
        "measure": MEASURE,
        "observationWindow": OBSERVATION_WINDOW_NS,
        "intervalNs": INTERVAL_NS,
        "availabilityThreshold": AVAILABILITY_THRESHOLD,
    }


# ---------------------------------------------------------------------------
# End-to-end run
# ---------------------------------------------------------------------------

def test_timeseries_real_dataset(sdk, request_body, capsys):
    """Run the resolver end to end and dump the response for inspection."""
    request = TimeSeriesRequest.model_validate(request_body)
    response = compute_cohort_timeseries(sdk, request, REQUEST_ID)

    payload = response.model_dump(by_alias=True)
    with open(OUTPUT_LOG, "w") as handle:
        json.dump(payload, handle, indent=2)

    n_expected = OBSERVATION_WINDOW_NS // INTERVAL_NS
    with capsys.disabled():
        print(f"\nWrote full response to {OUTPUT_LOG}")
        for cohort in response.cohorts:
            print(
                f"\ncohort {cohort.cohort_id}: {cohort.n_patients} patients, "
                f"{cohort.n_visits} visits bucketed, "
                f"{len(cohort.visits)} visit rows, "
                f"{len(cohort.patient_exclusions)} dropped before bucketing"
            )
            included = [iv.n_included for iv in cohort.intervals]
            print(f"  intervals: {len(cohort.intervals)} (expected {n_expected})")
            print(f"  n_included first/last: {included[:5]} ... {included[-5:]}")
            print(f"  n_included max/min: {max(included)}/{min(included)}")

            # The invariant worth seeing on real data: the denominator is the
            # same in every bucket, even as the included population shrinks.
            totals = {iv.n_included + iv.n_excluded for iv in cohort.intervals}
            print(f"  n_included + n_excluded across intervals: {totals}")

    for cohort in response.cohorts:
        assert len(cohort.intervals) == n_expected
        # Indices are dense and complete.
        assert [iv.interval_index for iv in cohort.intervals] == list(range(n_expected))
        # Every visit reference resolves.
        referenced = [e.visit for e in cohort.patient_exclusions]
        for interval in cohort.intervals:
            referenced += [r.visit for r in interval.patient_results]
            referenced += [e.visit for e in interval.exclusions]
        assert all(0 <= v < len(cohort.visits) for v in referenced)
        # Every bucketed entry is accounted for in every interval.
        assert {iv.n_included + iv.n_excluded for iv in cohort.intervals} == {
            cohort.n_visits
        }
        # Entry-level drops can only be mrn_not_found under a fixed window.
        assert {e.reason.value for e in cohort.patient_exclusions} <= {"mrn_not_found"}


# ---------------------------------------------------------------------------
# The single-call design check
# ---------------------------------------------------------------------------

def test_timeseries_availability_matches_interval_array(sdk, request_body, capsys):
    """Cross-check per-interval availability the two possible ways.

    The resolver derives availability from the NaN-filled sample grid alone —
    one ``get_data`` call per entry, no ``get_interval_array``. That rests on the
    two measures agreeing: the grid counts non-NaN samples against the measure's
    *nominal* period, whereas ``get_interval_array`` measures the wall-clock span
    of stored blocks. For a well-behaved periodic signal they agree; for one with
    duplicate or irregular timestamps they can drift.

    This test does not fail on a divergence — the right threshold for "too much"
    is a judgement call about this dataset, not something to hard-code. It
    reports the divergence so that judgement can be made. If it turns out to be
    material, the fix is documented in the design note: fall back to fetching
    ``get_interval_array`` per entry and intersecting it per bucket, using the
    values array for the means only.
    """
    request = TimeSeriesRequest.model_validate(request_body)
    measure_id = sdk.get_measure_id(
        MEASURE["measureTag"], freq=MEASURE["freq"],
        units=MEASURE["units"], freq_units=MEASURE["freqUnits"],
    )
    assert measure_id is not None, f"measure not in dataset: {MEASURE}"

    n_intervals = OBSERVATION_WINDOW_NS // INTERVAL_NS
    worst = 0.0
    compared = 0

    for mrn, admission_ns in COHORT_PATIENTS:
        patient_id = sdk.get_patient_id(mrn=mrn)
        if patient_id is None:
            continue

        window_start = admission_ns
        window_end = admission_ns + OBSERVATION_WINDOW_NS

        # (a) the resolver's way: the NaN-filled grid.
        _, values = sdk.get_data(
            measure_id=measure_id, patient_id=patient_id,
            start_time_n=window_start, end_time_n=window_end,
            return_nan_filled=True,
        )
        values = np.asarray(values if values is not None else [], dtype=np.float64)
        n_samples = values.size
        if n_samples == 0:
            continue
        index = np.arange(n_intervals + 1, dtype=np.int64)
        bounds = (index * n_samples + n_intervals // 2) // n_intervals
        sizes = np.diff(bounds)
        usable = ~np.isnan(values)
        bucket = np.repeat(np.arange(n_intervals, dtype=np.int64), sizes)
        counts = np.bincount(bucket[usable], minlength=n_intervals)
        from_grid = np.divide(
            counts, sizes, out=np.zeros(n_intervals), where=sizes > 0
        )

        # (b) the two-call way: intersect the stored blocks with each bucket.
        interval_arr = sdk.get_interval_array(
            measure_id=measure_id, patient_id=patient_id,
            start=window_start, end=window_end,
        )
        starts = window_start + np.arange(n_intervals, dtype=np.int64) * INTERVAL_NS
        ends = starts + INTERVAL_NS
        if interval_arr is None or len(interval_arr) == 0:
            from_blocks = np.zeros(n_intervals)
        else:
            blocks = np.asarray(interval_arr, dtype=np.int64)
            overlap = (
                np.minimum(ends[:, None], blocks[None, :, 1])
                - np.maximum(starts[:, None], blocks[None, :, 0])
            )
            covered = np.clip(overlap, 0, None).sum(axis=1)
            from_blocks = covered / INTERVAL_NS

        divergence = float(np.max(np.abs(from_grid - from_blocks)))
        worst = max(worst, divergence)
        compared += 1

        with capsys.disabled():
            print(
                f"  mrn={mrn} admission={admission_ns}: "
                f"max |grid - blocks| = {divergence:.4f}"
            )

    if compared == 0:
        pytest.skip("No cohort entry resolved to usable data on this dataset.")

    with capsys.disabled():
        verdict = "single-call path stands" if worst <= 0.05 else "REVIEW: see docstring"
        print(
            f"\nAvailability cross-check over {compared} entries: "
            f"worst divergence {worst:.4f} — {verdict}"
        )
