"""Tests for the POST /cohorts/timeseries endpoint.

Same shape as test_dashboard_statistics_api.py: a real uvicorn server runs in a
daemon thread and tests make plain requests.post() calls, with the SDK a
MagicMock injected via app.dependency_overrides. The endpoint calls the *real*
resolver, so bucketing, per-interval availability and the visit-index contract
are all genuinely exercised — the mock only stands in for the data source.

One addition over the statistics mock: ``get_measure_info`` must return a real
dict carrying ``freq_nhz``, because the resolver rejects aperiodic measures
(``freq_nhz == 0``) before bucketing anything.
"""

import logging
import socket
import threading
import time
from unittest.mock import MagicMock

import numpy as np
import pytest
import requests
import uvicorn
from pydantic import ValidationError

from atriumdb_dashboard.api.app import mount_dashboard
from atriumdb_dashboard.api.dependencies import get_sdk_instance
from atriumdb_dashboard.schemas import MAX_INTERVALS, TimeSeriesRequest
from tests.mock_api.app import app

# Mount the dashboard onto the upstream AtriumDB test app at runtime, rather
# than editing tests/mock_api/app.py, so that file stays identical to main.
mount_dashboard(app)

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

PORT = 8125  # distinct from test_api.py (8123) and the statistics tests (8124)
BASE_URL = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="module", autouse=True)
def api_server():
    """Start one uvicorn server for the whole module and wait until it accepts
    connections. Daemon thread, so it dies with the test process."""
    thread = threading.Thread(
        target=lambda: uvicorn.run(app, host="127.0.0.1", port=PORT),
        daemon=True,
    )
    thread.start()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", PORT)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"API server did not start on port {PORT} within 10s")


@pytest.fixture(autouse=True)
def _clear_overrides():
    """Drop the injected SDK after every test so overrides never leak."""
    yield
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ONE_MIN_NS = 60 * 1_000_000_000
INTERVAL_NS = 15 * ONE_MIN_NS          # 15 min buckets
WINDOW_NS = 60 * ONE_MIN_NS            # 1 h window -> exactly 4 buckets
N_INTERVALS = WINDOW_NS // INTERVAL_NS

ADMIT_NS = 1_600_000_000_000_000_000   # 2020-09-13
ONE_YEAR_NS = 365 * 24 * 3_600 * 1_000_000_000

MEASURE_ID = 1
MEASURE_TAG = "SpO2"
FREQ_NHZ = 1_000_000_000               # 1 Hz
PERIOD_NS = 1_000_000_000

# 12 samples over 4 buckets = 3 samples per bucket; bucket means are 1, 2, 3, 4.
FULL_VALUES = np.array([1., 1., 1., 2., 2., 2., 3., 3., 3., 4., 4., 4.])
#: Dense in the first two buckets, absent in the last two.
HALF_VALUES = np.array([1., 1., 1., 2., 2., 2., np.nan, np.nan, np.nan,
                        np.nan, np.nan, np.nan])
#: One usable sample in bucket 0 only — 1/3 coverage there, nothing elsewhere.
#: Whole-window coverage is 1/12, far below any sane threshold.
SPARSE_VALUES = np.array([7.] + [np.nan] * 11)

DOB_NS = ADMIT_NS - 30 * ONE_YEAR_NS
PATIENT_INFO = {"gender": "M", "dob": DOB_NS}
EXPECTED_AGE_MONTHS = 359

HEADERS = {"X-Request-ID": "test-req-ts-001"}

_UNSET = object()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_sdk(values=None, values_by_mrn=None, unresolvable=(),
              patient_info=_UNSET, measure_id=MEASURE_ID, freq_nhz=FREQ_NHZ,
              measure_info=_UNSET) -> MagicMock:
    """An SDK whose per-stage lookups are stubbed but whose time-series entry
    point runs the real resolver.

    ``get_data`` mirrors the real SDK's two return shapes: a 3-tuple normally,
    but a 2-tuple under ``return_nan_filled=True``, which is the only mode this
    endpoint uses. Values are dispatched by patient_id so a cohort of several
    patients can be given different data each.
    """
    sdk = MagicMock()
    sdk.get_measure_id.return_value = measure_id
    sdk.get_measure_info.return_value = (
        {"freq_nhz": freq_nhz, "period_ns": PERIOD_NS}
        if measure_info is _UNSET else measure_info
    )
    sdk.get_patient_info.return_value = (
        PATIENT_INFO if patient_info is _UNSET else patient_info
    )

    # MRNs get a patient_id on first sight, so tests need not pre-register them.
    pids: dict[str, int] = {}

    def _get_patient_id(mrn=None, **_):
        if mrn in unresolvable:
            return None
        return pids.setdefault(mrn, 100 + len(pids))

    sdk.get_patient_id.side_effect = _get_patient_id

    by_mrn = values_by_mrn or {}
    default = FULL_VALUES if values is None else values

    def _get_data(**kwargs):
        patient_id = kwargs.get("patient_id")
        mrn = next((m for m, p in pids.items() if p == patient_id), None)
        resolved = np.asarray(by_mrn.get(mrn, default), dtype=float)
        if kwargs.get("return_nan_filled"):
            return None, resolved
        return None, None, resolved

    sdk.get_data.side_effect = _get_data
    return sdk


def _body(mrns_and_admissions, availability_threshold=0.5,
          observation_window=WINDOW_NS, interval_ns=INTERVAL_NS,
          value_range=None, cohort_value_range=None) -> dict:
    cohort = {
        "id": 1,
        "patients": [
            {"mrn": mrn, "admissions": [{"admissionNs": a} for a in admissions]}
            for mrn, admissions in mrns_and_admissions
        ],
    }
    if cohort_value_range is not None:
        cohort["valueRange"] = cohort_value_range

    body = {
        "cohorts": [cohort],
        "measure": {"measureTag": MEASURE_TAG, "freq": 1.0,
                    "units": "%", "freqUnits": "Hz"},
        "observationWindow": observation_window,
        "intervalNs": interval_ns,
        "availabilityThreshold": availability_threshold,
    }
    if value_range is not None:
        body["valueRange"] = value_range
    return body


def _post_ts(sdk, body: dict, headers: dict = HEADERS) -> requests.Response:
    app.dependency_overrides[get_sdk_instance] = lambda: sdk
    return requests.post(f"{BASE_URL}/cohorts/timeseries", json=body,
                         headers=headers, timeout=10)


def _cohort_of(resp: requests.Response) -> dict:
    assert resp.status_code == 200, resp.text
    return resp.json()["cohorts"][0]


def _means_by_interval(cohort: dict) -> list[list[float]]:
    return [[r["mean"] for r in iv["patientResults"]] for iv in cohort["intervals"]]


def _reasons_by_interval(cohort: dict) -> list[list[str]]:
    return [[e["reason"] for e in iv["exclusions"]] for iv in cohort["intervals"]]


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------

def test_timeseries_single_patient_bucketed():
    """A patient with data across the whole window lands one mean in each interval."""
    resp = _post_ts(_mock_sdk(), _body([("MRN001", [ADMIT_NS])]))

    cohort = _cohort_of(resp)
    assert (cohort["nPatients"], cohort["nVisits"]) == (1, 1)
    assert cohort["patientExclusions"] == []
    assert len(cohort["intervals"]) == N_INTERVALS
    assert _means_by_interval(cohort) == [[1.0], [2.0], [3.0], [4.0]]


def test_timeseries_interval_indices_are_dense_with_correct_offsets():
    """Interval indices run 0..N-1 with no holes, and offsets are from admission."""
    resp = _post_ts(_mock_sdk(), _body([("MRN001", [ADMIT_NS])]))

    intervals = _cohort_of(resp)["intervals"]
    assert [iv["intervalIndex"] for iv in intervals] == list(range(N_INTERVALS))
    assert [iv["startOffsetNs"] for iv in intervals] == \
        [i * INTERVAL_NS for i in range(N_INTERVALS)]
    assert [iv["endOffsetNs"] for iv in intervals] == \
        [(i + 1) * INTERVAL_NS for i in range(N_INTERVALS)]
    # Offsets, not absolute timestamps — the first bucket starts at zero.
    assert intervals[0]["startOffsetNs"] == 0


def test_timeseries_bucket_not_a_whole_number_of_samples():
    """A grid that does not divide evenly into buckets still partitions exactly.

    10 samples over 4 buckets is 2.5 each. Every sample must land in exactly one
    bucket — none dropped off the tail, none double-counted.
    """
    values = np.arange(10, dtype=float)
    resp = _post_ts(_mock_sdk(values=values), _body([("MRN001", [ADMIT_NS])],
                                                    availability_threshold=0.0))

    cohort = _cohort_of(resp)
    assert len(cohort["intervals"]) == N_INTERVALS
    # Boundaries are rounded half-up, so the buckets are 3/2/3/2 samples wide:
    # [0,1,2] [3,4] [5,6,7] [8,9]. Every sample lands in exactly one bucket —
    # the means below sum back to the whole grid, so nothing fell off the tail.
    means = [m[0] for m in _means_by_interval(cohort)]
    assert means == pytest.approx([1.0, 3.5, 6.0, 8.5])
    assert sum(m * n for m, n in zip(means, [3, 2, 3, 2])) == pytest.approx(
        float(values.sum())
    )


# ---------------------------------------------------------------------------
# Per-interval availability
# ---------------------------------------------------------------------------

def test_timeseries_dense_early_sparse_late():
    """A patient dense early and absent late is included early, dropped late."""
    resp = _post_ts(_mock_sdk(values=HALF_VALUES), _body([("MRN001", [ADMIT_NS])]))

    cohort = _cohort_of(resp)
    assert _means_by_interval(cohort) == [[1.0], [2.0], [], []]
    assert _reasons_by_interval(cohort) == [
        [], [], ["below_availability_threshold"], ["below_availability_threshold"],
    ]
    assert cohort["intervals"][2]["exclusions"][0]["availability"] == 0.0


def test_timeseries_no_window_level_availability_gate():
    """The S2-vs-S3 divergence, asserted explicitly.

    SPARSE_VALUES covers 1/12 of the window — far below the 0.5 threshold that
    would drop this entry outright in the statistics endpoint. Here it must
    still reach bucketing and carry a real mean in the one bucket it does cover.
    A refactor that let a window-level gate ride along in a shared helper would
    fail here.
    """
    resp = _post_ts(_mock_sdk(values=SPARSE_VALUES),
                    _body([("MRN001", [ADMIT_NS])], availability_threshold=0.3))

    cohort = _cohort_of(resp)
    # Not dropped at the entry level, despite 1/12 whole-window coverage.
    assert cohort["patientExclusions"] == []
    assert cohort["nVisits"] == 1
    # Present in exactly the bucket it covers (1 of 3 samples clears 0.3).
    assert _means_by_interval(cohort) == [[7.0], [], [], []]


def test_timeseries_included_population_shrinks_across_series():
    """n_included falls as data runs out, but the denominator stays constant."""
    resp = _post_ts(
        _mock_sdk(values_by_mrn={"MRN001": FULL_VALUES, "MRN002": HALF_VALUES}),
        _body([("MRN001", [ADMIT_NS]), ("MRN002", [ADMIT_NS])]),
    )

    cohort = _cohort_of(resp)
    assert [iv["nIncluded"] for iv in cohort["intervals"]] == [2, 2, 1, 1]
    # Every bucketed entry is accounted for in every interval.
    for interval in cohort["intervals"]:
        assert interval["nIncluded"] + interval["nExcluded"] == cohort["nVisits"] == 2


def test_timeseries_empty_interval_still_emitted():
    """An interval no visit covers is emitted with an empty patientResults."""
    resp = _post_ts(_mock_sdk(values=HALF_VALUES), _body([("MRN001", [ADMIT_NS])]))

    last = _cohort_of(resp)["intervals"][-1]
    assert last["patientResults"] == []
    assert last["nIncluded"] == 0
    assert len(last["exclusions"]) == 1


# ---------------------------------------------------------------------------
# Exclusion placement
# ---------------------------------------------------------------------------

def test_timeseries_mrn_not_found_recorded_once_not_per_interval():
    """An entry-level drop is recorded once, never repeated in every bucket."""
    resp = _post_ts(
        _mock_sdk(unresolvable={"MRN404"}),
        _body([("MRN001", [ADMIT_NS]), ("MRN404", [ADMIT_NS])]),
    )

    cohort = _cohort_of(resp)
    assert cohort["nPatients"] == 1
    assert cohort["nVisits"] == 1
    assert len(cohort["patientExclusions"]) == 1
    assert cohort["patientExclusions"][0]["reason"] == "mrn_not_found"
    # And not repeated per interval.
    for interval in cohort["intervals"]:
        assert interval["exclusions"] == []


def test_timeseries_patient_exclusions_are_single_reason():
    """Under a fixed window, mrn_not_found is the only entry-level drop.

    missing_discharge_time needs the all_time branch, which this endpoint cannot
    express, and there is no device stage. This assertion will fail loudly the
    day another entry-level gate is added, which is the point.
    """
    resp = _post_ts(
        _mock_sdk(unresolvable={"MRN404", "MRN405"}),
        _body([("MRN404", [ADMIT_NS]), ("MRN001", [ADMIT_NS]),
               ("MRN405", [ADMIT_NS])]),
    )

    cohort = _cohort_of(resp)
    assert {e["reason"] for e in cohort["patientExclusions"]} == {"mrn_not_found"}


def test_timeseries_interval_exclusion_is_interval_scoped():
    """A per-interval drop appears only in the interval it applies to."""
    resp = _post_ts(_mock_sdk(values=HALF_VALUES), _body([("MRN001", [ADMIT_NS])]))

    cohort = _cohort_of(resp)
    assert cohort["patientExclusions"] == []
    assert [len(iv["exclusions"]) for iv in cohort["intervals"]] == [0, 0, 1, 1]


# ---------------------------------------------------------------------------
# Visit table and index contract
# ---------------------------------------------------------------------------

def test_timeseries_visit_indices_stable_when_first_entry_excluded():
    """Dropping the first entry must not shift any later entry's index."""
    resp = _post_ts(
        _mock_sdk(unresolvable={"MRN404"}),
        _body([("MRN404", [ADMIT_NS]), ("MRN001", [ADMIT_NS])]),
    )

    cohort = _cohort_of(resp)
    # The excluded entry keeps position 0, in visits and in the exclusion record.
    assert [v["mrn"] for v in cohort["visits"]] == ["MRN404", "MRN001"]
    assert cohort["patientExclusions"][0]["visit"] == 0
    # The surviving entry is still at its request position, not shifted to 0.
    assert cohort["intervals"][0]["patientResults"][0]["visit"] == 1


def test_timeseries_visit_indices_are_all_in_range():
    """Every visit reference anywhere resolves inside its cohort's visits table."""
    resp = _post_ts(
        _mock_sdk(unresolvable={"MRN404"},
                  values_by_mrn={"MRN002": HALF_VALUES}),
        _body([("MRN001", [ADMIT_NS]), ("MRN404", [ADMIT_NS]),
               ("MRN002", [ADMIT_NS])]),
    )

    cohort = _cohort_of(resp)
    n_visits_rows = len(cohort["visits"])
    referenced = [e["visit"] for e in cohort["patientExclusions"]]
    for interval in cohort["intervals"]:
        referenced += [r["visit"] for r in interval["patientResults"]]
        referenced += [e["visit"] for e in interval["exclusions"]]

    assert referenced, "expected at least one visit reference"
    assert all(0 <= v < n_visits_rows for v in referenced)
    # visits carries the pre-bucketing drops too, so it is not a patient count.
    assert n_visits_rows >= cohort["nVisits"]
    assert (n_visits_rows, cohort["nVisits"]) == (3, 2)


def test_timeseries_multiple_admissions_are_independent_visits():
    """Two admissions for one MRN produce two visits and two bucket series."""
    second_admit = ADMIT_NS + 10 * WINDOW_NS
    resp = _post_ts(_mock_sdk(), _body([("MRN001", [ADMIT_NS, second_admit])]))

    cohort = _cohort_of(resp)
    assert cohort["nPatients"] == 1      # one distinct MRN
    assert cohort["nVisits"] == 2        # two entries
    assert [v["admissionNs"] for v in cohort["visits"]] == [ADMIT_NS, second_admit]
    # Both series are present in every interval, as distinct visit indices.
    for interval in cohort["intervals"]:
        assert sorted(r["visit"] for r in interval["patientResults"]) == [0, 1]


# ---------------------------------------------------------------------------
# Demographics
# ---------------------------------------------------------------------------

def test_timeseries_demographics_carried_once_per_visit():
    """Demographics live on the visit, and are looked up once per entry."""
    sdk = _mock_sdk()
    resp = _post_ts(sdk, _body([("MRN001", [ADMIT_NS])]))

    visit = _cohort_of(resp)["visits"][0]
    assert visit["mrn"] == "MRN001"
    assert visit["sex"] == "M"
    assert visit["ageMonths"] == EXPECTED_AGE_MONTHS
    # Once per entry, not once per interval — the whole point of normalising.
    assert sdk.get_patient_info.call_count == 1


def test_timeseries_missing_demographics_do_not_exclude():
    """Demographics are best-effort: absent means null, never a dropped visit."""
    resp = _post_ts(_mock_sdk(patient_info=None), _body([("MRN001", [ADMIT_NS])]))

    cohort = _cohort_of(resp)
    assert cohort["visits"][0]["sex"] is None
    assert cohort["visits"][0]["ageMonths"] is None
    assert _means_by_interval(cohort) == [[1.0], [2.0], [3.0], [4.0]]


# ---------------------------------------------------------------------------
# Value range
# ---------------------------------------------------------------------------

def test_timeseries_value_range_reduces_one_interval_availability():
    """Out-of-range samples count as absent in the bucket they fall in."""
    # Bucket 2's samples are all 3.0, which an upper bound of 2.5 excludes.
    resp = _post_ts(
        _mock_sdk(),
        _body([("MRN001", [ADMIT_NS])],
              value_range={MEASURE_TAG: {"lower": 0.0, "upper": 2.5}}),
    )

    cohort = _cohort_of(resp)
    assert _means_by_interval(cohort) == [[1.0], [2.0], [], []]
    dropped = cohort["intervals"][2]["exclusions"][0]
    assert dropped["reason"] == "below_availability_threshold"
    assert dropped["availability"] == 0.0


def test_timeseries_cohort_value_range_intersects_global():
    """A cohort range narrows the global one; it can never widen it."""
    resp = _post_ts(
        _mock_sdk(),
        _body([("MRN001", [ADMIT_NS])],
              value_range={MEASURE_TAG: {"lower": 0.0, "upper": 3.5}},
              cohort_value_range={MEASURE_TAG: {"lower": 0.0, "upper": 100.0}}),
    )

    # The tighter ceiling (3.5) wins, so bucket 3 (all 4.0) is still excluded.
    assert _means_by_interval(_cohort_of(resp)) == [[1.0], [2.0], [3.0], []]


def test_timeseries_value_range_for_another_tag_is_ignored():
    """Bounds keyed by a measure this request is not analysing do not apply."""
    resp = _post_ts(
        _mock_sdk(),
        _body([("MRN001", [ADMIT_NS])],
              value_range={"SomeOtherTag": {"lower": 0.0, "upper": 1.5}}),
    )

    assert _means_by_interval(_cohort_of(resp)) == [[1.0], [2.0], [3.0], [4.0]]


# ---------------------------------------------------------------------------
# Contract and validation
# ---------------------------------------------------------------------------

def test_timeseries_non_divisible_window_rejected():
    """observation_window must be an exact multiple of interval_ns."""
    resp = _post_ts(_mock_sdk(),
                    _body([("MRN001", [ADMIT_NS])],
                          observation_window=WINDOW_NS + 1))
    assert resp.status_code == 422, resp.text


def test_timeseries_too_many_intervals_rejected():
    """A bucket count above MAX_INTERVALS is a validation error, not an OOM."""
    interval_ns = ONE_MIN_NS
    resp = _post_ts(_mock_sdk(),
                    _body([("MRN001", [ADMIT_NS])],
                          observation_window=(MAX_INTERVALS + 1) * interval_ns,
                          interval_ns=interval_ns))
    assert resp.status_code == 422, resp.text


def test_timeseries_all_time_window_rejected():
    """'all_time' is unrepresentable here — it fails schema parsing."""
    resp = _post_ts(_mock_sdk(),
                    _body([("MRN001", [ADMIT_NS])], observation_window="all_time"))
    assert resp.status_code == 422, resp.text


def test_timeseries_aperiodic_measure_rejected():
    """A measure with no sampling period has no grid to bucket against."""
    sdk = _mock_sdk(measure_info={"freq_nhz": 0, "period_ns": None})
    resp = _post_ts(sdk, _body([("MRN001", [ADMIT_NS])]))
    assert resp.status_code == 422, resp.text
    assert "aperiodic" in resp.json()["detail"]


def test_timeseries_missing_availability_threshold_rejected():
    """The threshold is required: omitting it is a 422, never a silent default.

    A default here would filter the caller's data on their behalf, and they
    could not tell that apart from a genuinely sparse signal.
    """
    body = _body([("MRN001", [ADMIT_NS])])
    del body["availabilityThreshold"]
    resp = _post_ts(_mock_sdk(), body)
    assert resp.status_code == 422, resp.text


def test_timeseries_zero_threshold_applies_no_gate():
    """Passing 0.0 explicitly reports any bucket holding a usable sample."""
    resp = _post_ts(_mock_sdk(values=SPARSE_VALUES),
                    _body([("MRN001", [ADMIT_NS])], availability_threshold=0.0))

    cohort = _cohort_of(resp)
    # Only bucket 0 has a usable sample; the rest are empty, so they fall to
    # no_usable_values rather than the threshold.
    assert _means_by_interval(cohort) == [[7.0], [], [], []]
    assert _reasons_by_interval(cohort)[1:] == [["no_usable_values"]] * 3


def test_timeseries_unknown_measure_rejected():
    resp = _post_ts(_mock_sdk(measure_id=None), _body([("MRN001", [ADMIT_NS])]))
    assert resp.status_code == 422, resp.text


def test_timeseries_missing_request_id_rejected():
    resp = _post_ts(_mock_sdk(), _body([("MRN001", [ADMIT_NS])]), headers={})
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# Rejection logging
#
# FastAPI's default RequestValidationError handler logs nothing and uvicorn's
# access log records only the status code, so without the wrap validator on
# TimeSeriesRequest a rejected request leaves no server-side trace at all.
# ---------------------------------------------------------------------------

SCHEMA_LOGGER = "atriumdb_dashboard.schemas"


def _valid_payload() -> dict:
    return _body([("MRN-PRIVATE-999", [ADMIT_NS])])


def _assert_rejection_logged(caplog, payload: dict, expected_fragment: str):
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=SCHEMA_LOGGER):
        with pytest.raises(ValidationError):
            TimeSeriesRequest.model_validate(payload)

    warnings = [r for r in caplog.records
                if r.name == SCHEMA_LOGGER and r.levelno == logging.WARNING]
    assert warnings, f"rejection was not logged for: {expected_fragment}"
    message = warnings[0].getMessage()
    assert expected_fragment in message, message
    # The offending input is the request body, which carries MRNs. Only error
    # locations and messages may be logged.
    assert "MRN-PRIVATE-999" not in message, "patient MRN leaked into the log"


def test_after_validator_rejection_is_logged(caplog):
    """Guards the declaration-order hazard on the wrap validator.

    A wrap validator only wraps what is declared below it, so if another
    ``model_validator(mode="after")`` is ever added *underneath*
    ``_log_rejected_request``, this check's failures stop being logged —
    silently, and only for that one check. This test is what catches that.
    """
    payload = _valid_payload()
    payload["intervalNs"] = payload["observationWindow"] - 1  # non-divisible
    _assert_rejection_logged(caplog, payload, "must be an exact multiple")


def test_max_intervals_rejection_is_logged(caplog):
    payload = _valid_payload()
    payload["intervalNs"] = 1
    payload["observationWindow"] = MAX_INTERVALS + 1
    _assert_rejection_logged(caplog, payload, "intervals, above the")


def test_field_level_rejection_is_logged(caplog):
    """Field-level failures are logged too — the reason for wrapping rather
    than catching inside the divisibility validator."""
    payload = _valid_payload()
    del payload["availabilityThreshold"]
    _assert_rejection_logged(caplog, payload, "availabilityThreshold")


def test_valid_request_logs_nothing(caplog):
    """The wrapper must be silent on the happy path."""
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=SCHEMA_LOGGER):
        TimeSeriesRequest.model_validate(_valid_payload())

    assert [r for r in caplog.records if r.name == SCHEMA_LOGGER] == []
