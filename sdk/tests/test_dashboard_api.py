"""Tests for the POST /cohorts/statistics endpoint.

A real uvicorn server runs in a daemon thread and tests make plain
requests.post() calls. The SDK is a MagicMock injected via
app.dependency_overrides, so no dataset is needed — the resolver's inputs
(interval arrays, value arrays) are far easier to control directly than to
produce by writing waveform data. dashboard_compute_statistics on the mock is
wired to the *real* resolver, so every per-stage SDK call is still exercised.
"""

import socket
import threading
import time
from unittest.mock import MagicMock

import numpy as np
import pytest
import requests
import uvicorn

from atriumdb.dashboard.statistics_resolver import compute_aggregate_statistics
from tests.mock_api.app import app
from tests.mock_api.sdk_dependency import get_sdk_instance

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

PORT = 8124  # distinct from test_api.py (8123)
BASE_URL = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="module", autouse=True)
def api_server():
    """Start one uvicorn server for the whole module and wait until it accepts
    connections. Daemon thread, so it dies with the test process.

    A fixture rather than pytest_configure: pytest only collects that hook from
    conftest.py and plugins, never from a test module, so defining it here would
    silently never run.
    """
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
    """Drop the injected SDK after every test so overrides never leak between
    tests — otherwise a test that forgets to inject silently runs against the
    previous test's SDK."""
    yield
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ONE_HOUR_NS = 3_600 * 1_000_000_000
WINDOW_NS = 24 * ONE_HOUR_NS
ADMIT_NS = 1_600_000_000_000_000_000  # 2020-09-13
ONE_YEAR_NS = 365 * 24 * ONE_HOUR_NS

MEASURE_ID = 1
MEASURE_TAG = "SpO2"
PATIENT_ID = 42
DEVICE_ID = 7

FULL_COVERAGE = np.array([[ADMIT_NS, ADMIT_NS + WINDOW_NS]])
LOW_COVERAGE = np.array([[ADMIT_NS, ADMIT_NS + 10 * ONE_HOUR_NS]])  # 10/24 ≈ 41 %

GOOD_VALUES = np.array([95.0, 96.0, float("nan"), 97.0])  # non-NaN mean = 96.0
ALL_NAN = np.array([float("nan"), float("nan")])

# Demographics as of ADMIT_NS. dob is 30 years earlier, so age_months == 359 —
# 30 * 365 days lands a few days short of the birthday.
DOB_NS = ADMIT_NS - 30 * ONE_YEAR_NS
PATIENT_INFO = {"gender": "M", "dob": DOB_NS}
EXPECTED_AGE_MONTHS = 359

HEADERS = {"X-Request-ID": "test-req-001"}

#: Distinguishes "argument omitted" from an explicit None, which for
#: patient_info is a meaningful value (no demographics record found).
_UNSET = object()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_sdk(measure_id=MEASURE_ID, patient_id=PATIENT_ID, device_id=DEVICE_ID,
              interval_arr=None, values=None, patient_info=_UNSET) -> MagicMock:
    """An SDK whose per-stage lookups are stubbed but whose statistics entry
    point runs the real resolver, so the stubs below are genuinely exercised.

    ``get_patient_info`` must be stubbed explicitly: a bare MagicMock returns a
    MagicMock for ``gender``, which fails PatientResult validation with a 422
    rather than anything that points at the mock.

    ``get_data`` mirrors the real SDK's two return shapes: a 3-tuple normally,
    but a 2-tuple under ``return_nan_filled=True``, which is the mode the
    resolver uses whenever bounds are in force. Returning the wrong arity there
    surfaces as an unpacking error deep in the resolver, so the mock branches on
    the flag rather than pinning one shape.
    """
    sdk = MagicMock()
    sdk.get_measure_id.return_value = measure_id
    sdk.get_patient_id.return_value = patient_id
    sdk.convert_patient_to_device_id.return_value = device_id
    sdk.get_interval_array.return_value = FULL_COVERAGE if interval_arr is None else interval_arr
    sdk.get_patient_info.return_value = PATIENT_INFO if patient_info is _UNSET else patient_info

    resolved_values = GOOD_VALUES if values is None else values

    def _get_data(**kwargs):
        if kwargs.get("return_nan_filled"):
            return None, resolved_values
        return None, None, resolved_values

    sdk.get_data.side_effect = _get_data
    sdk.dashboard_compute_statistics.side_effect = (
        lambda req, req_id: compute_aggregate_statistics(sdk, req, req_id)
    )
    return sdk


def _admission(spec) -> dict:
    """An admission entry from either a bare ``admission_ns`` or an
    ``(admission_ns, discharge_ns)`` pair — discharge only matters under
    ``all_time``, so most cases pass the bare form."""
    if isinstance(spec, tuple):
        admission_ns, discharge_ns = spec
        return {"admissionNs": admission_ns, "dischargeNs": discharge_ns}
    return {"admissionNs": spec}


def _body(mrns_and_admissions: list[tuple[str, list]],
          availability_threshold: float = 0.5,
          observation_window=WINDOW_NS,
          value_range: dict | None = None,
          cohort_value_range: dict | None = None) -> dict:
    cohort = {
        "id": 1,
        "patients": [
            {"mrn": mrn, "admissions": [_admission(a) for a in admissions]}
            for mrn, admissions in mrns_and_admissions
        ],
    }
    if cohort_value_range is not None:
        cohort["valueRange"] = cohort_value_range

    body = {
        "cohorts": [cohort],
        "measure": {"measureTag": MEASURE_TAG, "freq": 1.0, "units": "%", "freqUnits": "Hz"},
        "observationWindow": observation_window,
        "availabilityThreshold": availability_threshold,
    }
    if value_range is not None:
        body["valueRange"] = value_range
    return body


def _post_stats(sdk, body: dict, headers: dict = HEADERS) -> requests.Response:
    app.dependency_overrides[get_sdk_instance] = lambda: sdk
    return requests.post(f"{BASE_URL}/cohorts/statistics", json=body,
                         headers=headers, timeout=10)


def _cohort_of(resp: requests.Response) -> dict:
    assert resp.status_code == 200, resp.text
    return resp.json()["cohorts"][0]


def _one_exclusion(resp: requests.Response) -> dict:
    """Assert the request produced exactly one excluded entry and return it."""
    cohort = _cohort_of(resp)
    assert cohort["nIncluded"] == 0
    assert cohort["patientResults"] == []
    assert len(cohort["exclusions"]) == 1
    return cohort["exclusions"][0]


# ---------------------------------------------------------------------------
# Included paths
# ---------------------------------------------------------------------------

def test_statistics_single_patient_included():
    """One admission with full coverage and usable values produces one result."""
    resp = _post_stats(_mock_sdk(), _body([("MRN001", [ADMIT_NS])]))

    cohort = _cohort_of(resp)
    assert (cohort["nPatients"], cohort["nVisits"]) == (1, 1)
    assert (cohort["nIncluded"], cohort["nExcluded"]) == (1, 0)
    assert cohort["exclusions"] == []

    result = cohort["patientResults"][0]
    assert result["mrn"] == "MRN001"
    assert result["admissionNs"] == ADMIT_NS
    # NaN is dropped before averaging: mean(95, 96, 97) == 96.
    assert result["mean"] == pytest.approx(96.0)
    # Demographics are carried through from get_patient_info as of this admission.
    assert result["sex"] == "M"
    assert result["ageMonths"] == EXPECTED_AGE_MONTHS


def test_statistics_missing_demographics_do_not_exclude():
    """Demographics are best-effort: a dataset with no patient record still
    produces a result, with the optional fields left null."""
    resp = _post_stats(_mock_sdk(patient_info=None), _body([("MRN001", [ADMIT_NS])]))

    cohort = _cohort_of(resp)
    assert cohort["nIncluded"] == 1

    result = cohort["patientResults"][0]
    assert result["mean"] == pytest.approx(96.0)
    assert result["sex"] is None
    assert result["ageMonths"] is None


def test_statistics_two_patients_get_independent_means():
    """Each patient's mean is computed over its own data, not pooled."""
    sdk = _mock_sdk()
    sdk.get_patient_id.side_effect = [10, 20]
    sdk.get_data.side_effect = [
        (None, None, np.array([90.0, 92.0])),
        (None, None, np.array([80.0, 82.0])),
    ]
    resp = _post_stats(sdk, _body([("MRN001", [ADMIT_NS]), ("MRN002", [ADMIT_NS])]))

    cohort = _cohort_of(resp)
    assert (cohort["nPatients"], cohort["nIncluded"]) == (2, 2)
    means = {r["mrn"]: r["mean"] for r in cohort["patientResults"]}
    assert means == {"MRN001": pytest.approx(91.0), "MRN002": pytest.approx(81.0)}


def test_statistics_multiple_admissions_are_scored_separately():
    """Two admissions for one patient are independent entries: one may be
    excluded while the other is included."""
    admit_2 = ADMIT_NS + 30 * 24 * ONE_HOUR_NS

    sdk = _mock_sdk()
    sdk.convert_patient_to_device_id.side_effect = [None, DEVICE_ID]
    resp = _post_stats(sdk, _body([("MRN001", [ADMIT_NS, admit_2])]))

    cohort = _cohort_of(resp)
    # One patient, but two visits — the count distinction that makes
    # per-admission granularity observable.
    assert (cohort["nPatients"], cohort["nVisits"]) == (1, 2)
    assert (cohort["nIncluded"], cohort["nExcluded"]) == (1, 1)

    assert cohort["patientResults"][0]["admissionNs"] == admit_2

    exc = cohort["exclusions"][0]
    assert (exc["mrn"], exc["admissionNs"], exc["reason"]) == ("MRN001", ADMIT_NS, "no_device_found")


# ---------------------------------------------------------------------------
# Exclusion reasons
# ---------------------------------------------------------------------------

def test_statistics_mrn_not_found():
    """An MRN absent from AtriumDB is excluded before any admission is reached,
    so it carries no admission or window."""
    exc = _one_exclusion(_post_stats(_mock_sdk(patient_id=None), _body([("MRNBAD", [ADMIT_NS])])))

    assert exc["mrn"] == "MRNBAD"
    assert exc["reason"] == "mrn_not_found"
    assert exc["admissionNs"] is None


def test_statistics_no_device_found():
    """A patient with no device covering the window is excluded, and the window
    that was searched is reported."""
    exc = _one_exclusion(_post_stats(_mock_sdk(device_id=None), _body([("MRN001", [ADMIT_NS])])))

    assert exc["reason"] == "no_device_found"
    assert exc["admissionNs"] == ADMIT_NS
    assert exc["windowStartNs"] == ADMIT_NS
    assert exc["windowEndNs"] == ADMIT_NS + WINDOW_NS


def test_statistics_below_availability_threshold():
    """Coverage under the threshold excludes the entry and reports the actual
    fraction, so the client can explain the drop."""
    exc = _one_exclusion(_post_stats(_mock_sdk(interval_arr=LOW_COVERAGE),
                                     _body([("MRN001", [ADMIT_NS])])))

    assert exc["reason"] == "below_availability_threshold"
    assert exc["availability"] == pytest.approx(10 / 24, rel=1e-3)


def test_statistics_no_usable_values():
    """An admission whose values are entirely NaN has nothing to average."""
    exc = _one_exclusion(_post_stats(_mock_sdk(values=ALL_NAN), _body([("MRN001", [ADMIT_NS])])))
    assert exc["reason"] == "no_usable_values"


# ---------------------------------------------------------------------------
# value_range bounds
#
# Under bounds, get_data is called NaN-filled: every slot the frequency implies
# is present, gaps arrive as NaN, and out-of-range samples are marked absent
# alongside them. Availability is therefore the *usable* fraction of that array,
# which is why these cases pin availability as well as the mean.
# ---------------------------------------------------------------------------

def test_statistics_value_range_lower_bound_only():
    """An open upper end constrains nothing; only the floor applies."""
    values = np.array([30.0, 95.0, 96.0, 97.0])  # 30 is below the floor
    resp = _post_stats(
        _mock_sdk(values=values),
        _body([("MRN001", [ADMIT_NS])], value_range={MEASURE_TAG: {"lower": 40.0}}),
    )

    cohort = _cohort_of(resp)
    assert cohort["nIncluded"] == 1
    # 3 of 4 usable = 0.75, over the 0.5 threshold; mean of the survivors.
    assert cohort["patientResults"][0]["mean"] == pytest.approx(96.0)


def test_statistics_value_range_upper_bound_only():
    """An open lower end constrains nothing; only the ceiling applies."""
    values = np.array([95.0, 96.0, 97.0, 500.0])  # 500 is above the ceiling
    resp = _post_stats(
        _mock_sdk(values=values),
        _body([("MRN001", [ADMIT_NS])], value_range={MEASURE_TAG: {"upper": 100.0}}),
    )

    assert _cohort_of(resp)["patientResults"][0]["mean"] == pytest.approx(96.0)


def test_statistics_value_range_both_bounds_and_nan():
    """Bounds and NaN both mark a sample absent; survivors set the mean."""
    values = np.array([30.0, 95.0, 96.0, float("nan"), 97.0, 500.0])
    resp = _post_stats(
        _mock_sdk(values=values),
        _body([("MRN001", [ADMIT_NS])],
              availability_threshold=0.5,
              value_range={MEASURE_TAG: {"lower": 40.0, "upper": 100.0}}),
    )

    cohort = _cohort_of(resp)
    # 3 usable of 6 slots = 0.5, exactly at the threshold — inclusive.
    assert cohort["nIncluded"] == 1
    assert cohort["patientResults"][0]["mean"] == pytest.approx(96.0)


def test_statistics_out_of_range_values_reduce_availability():
    """The core claim of bounded retrieval: out-of-range samples are treated as
    *absent*, so a mostly-artifact signal fails the availability gate instead of
    quietly producing a mean over the few good samples."""
    # 8 of 10 samples are far out of range; only 95 and 97 survive.
    values = np.array([500.0] * 8 + [95.0, 97.0])
    resp = _post_stats(
        _mock_sdk(values=values),
        _body([("MRN001", [ADMIT_NS])],
              availability_threshold=0.5,
              value_range={MEASURE_TAG: {"lower": 40.0, "upper": 100.0}}),
    )

    exc = _one_exclusion(resp)
    assert exc["reason"] == "below_availability_threshold"
    # Post-filter availability, not the interval-array coverage (which is full).
    assert exc["availability"] == pytest.approx(0.2)


def test_statistics_value_range_intersects_global_and_cohort():
    """Cohort and global bounds are intersected — the tighter end wins on each
    side independently, and a value must satisfy both."""
    # global [40, 200] ∩ cohort [60, 180] -> [60, 180]
    values = np.array([50.0, 100.0, 150.0, 190.0])
    resp = _post_stats(
        _mock_sdk(values=values),
        _body([("MRN001", [ADMIT_NS])],
              availability_threshold=0.5,
              value_range={MEASURE_TAG: {"lower": 40.0, "upper": 200.0}},
              cohort_value_range={MEASURE_TAG: {"lower": 60.0, "upper": 180.0}}),
    )

    cohort = _cohort_of(resp)
    assert cohort["nIncluded"] == 1
    # 50 fails the cohort floor, 190 fails the cohort ceiling — both would have
    # passed the global range alone.
    assert cohort["patientResults"][0]["mean"] == pytest.approx(125.0)


def test_statistics_cohort_value_range_cannot_widen_global():
    """A cohort range looser than the global one does not relax it.

    This is the case that distinguishes intersection from override semantics —
    if it starts failing, the resolution rule has changed.
    """
    # global [40, 200] ∩ cohort [30, 220] -> [40, 200]
    values = np.array([35.0, 100.0, 210.0])
    resp = _post_stats(
        _mock_sdk(values=values),
        _body([("MRN001", [ADMIT_NS])],
              availability_threshold=0.3,
              value_range={MEASURE_TAG: {"lower": 40.0, "upper": 200.0}},
              cohort_value_range={MEASURE_TAG: {"lower": 30.0, "upper": 220.0}}),
    )

    cohort = _cohort_of(resp)
    assert cohort["nIncluded"] == 1
    # 35 and 210 sit inside the cohort range but outside the global one.
    assert cohort["patientResults"][0]["mean"] == pytest.approx(100.0)


def test_statistics_value_range_for_other_measure_tag_is_ignored():
    """Only bounds keyed by the requested measure tag apply."""
    values = np.array([30.0, 95.0, 500.0])
    resp = _post_stats(
        _mock_sdk(values=values),
        _body([("MRN001", [ADMIT_NS])],
              value_range={"SomeOtherMeasure": {"lower": 40.0, "upper": 100.0}}),
    )

    cohort = _cohort_of(resp)
    assert cohort["nIncluded"] == 1
    # Unbounded: nothing filtered, so every sample is averaged.
    assert cohort["patientResults"][0]["mean"] == pytest.approx(np.mean(values))


# ---------------------------------------------------------------------------
# observation_window: "all_time"
# ---------------------------------------------------------------------------

def test_statistics_all_time_window_spans_admission_to_discharge():
    """Under all_time the window ends at the stay's own discharge rather than a
    fixed offset from admission."""
    discharge_ns = ADMIT_NS + 72 * ONE_HOUR_NS  # deliberately unlike WINDOW_NS

    # A no-device exclusion is the cheapest way to observe the window the
    # resolver computed, since it reports the window it searched.
    resp = _post_stats(
        _mock_sdk(device_id=None),
        _body([("MRN001", [(ADMIT_NS, discharge_ns)])], observation_window="all_time"),
    )

    exc = _one_exclusion(resp)
    assert exc["reason"] == "no_device_found"
    assert exc["windowStartNs"] == ADMIT_NS
    assert exc["windowEndNs"] == discharge_ns


def test_statistics_all_time_included_admission():
    """A closed stay under all_time resolves normally."""
    resp = _post_stats(
        _mock_sdk(interval_arr=np.array([[ADMIT_NS, ADMIT_NS + 72 * ONE_HOUR_NS]])),
        _body([("MRN001", [(ADMIT_NS, ADMIT_NS + 72 * ONE_HOUR_NS)])],
              observation_window="all_time"),
    )

    cohort = _cohort_of(resp)
    assert cohort["nIncluded"] == 1
    assert cohort["patientResults"][0]["mean"] == pytest.approx(96.0)


def test_statistics_all_time_open_stay_is_excluded():
    """An open stay has no bounded window to measure availability against."""
    exc = _one_exclusion(_post_stats(
        _mock_sdk(),
        _body([("MRN001", [ADMIT_NS])], observation_window="all_time"),
    ))

    assert exc["reason"] == "missing_discharge_time"
    assert exc["admissionNs"] == ADMIT_NS
    # No window could be bounded, so neither end is reported.
    assert exc["windowStartNs"] is None
    assert exc["windowEndNs"] is None


@pytest.mark.parametrize(
    "discharge_ns, case",
    [(ADMIT_NS, "same instant"), (ADMIT_NS - ONE_HOUR_NS, "before admission")],
)
def test_statistics_all_time_discharge_not_after_admission_is_excluded(discharge_ns, case):
    """A discharge that does not follow the admission is an inconsistent record,
    not a zero-length window to average over."""
    exc = _one_exclusion(_post_stats(
        _mock_sdk(),
        _body([("MRN001", [(ADMIT_NS, discharge_ns)])], observation_window="all_time"),
    ))
    assert exc["reason"] == "missing_discharge_time", case


def test_statistics_all_time_mixed_admissions():
    """One patient with a closed and an open stay: the closed one resolves, the
    open one is excluded — per-admission handling under all_time."""
    discharge_ns = ADMIT_NS + 72 * ONE_HOUR_NS
    open_admit_ns = ADMIT_NS + 30 * 24 * ONE_HOUR_NS

    resp = _post_stats(
        _mock_sdk(interval_arr=np.array([[ADMIT_NS, discharge_ns]])),
        _body([("MRN001", [(ADMIT_NS, discharge_ns), open_admit_ns])],
              observation_window="all_time"),
    )

    cohort = _cohort_of(resp)
    assert (cohort["nPatients"], cohort["nVisits"]) == (1, 2)
    assert (cohort["nIncluded"], cohort["nExcluded"]) == (1, 1)
    assert cohort["patientResults"][0]["admissionNs"] == ADMIT_NS
    assert cohort["exclusions"][0]["admissionNs"] == open_admit_ns
    assert cohort["exclusions"][0]["reason"] == "missing_discharge_time"


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

def test_statistics_missing_request_id_header_is_rejected():
    """The endpoint requires X-Request-ID for log correlation."""
    resp = _post_stats(_mock_sdk(), _body([("MRN001", [ADMIT_NS])]), headers={})

    assert resp.status_code == 400
    assert "X-Request-ID" in resp.json()["detail"]


def test_statistics_unknown_measure_is_rejected():
    """A measure absent from the dataset is a client error, not a 500."""
    resp = _post_stats(_mock_sdk(measure_id=None), _body([("MRN001", [ADMIT_NS])]))
    assert resp.status_code == 422
