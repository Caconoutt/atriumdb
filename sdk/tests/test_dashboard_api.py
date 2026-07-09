"""Tests for the POST /cohorts/statistics endpoint.

Follows the same pattern as test_api.py: a real uvicorn server runs in a
daemon thread and tests make plain requests.post() calls. The SDK is a
MagicMock injected via app.dependency_overrides so no real dataset is needed.
dashboard_compute_statistics on the mock is wired to the real resolver so
all per-stage SDK method stubs are actually exercised.
"""

import threading
import time

import numpy as np
import pytest
import requests
import uvicorn
from unittest.mock import MagicMock

from tests.mock_api.app import app
from tests.mock_api.sdk_dependency import get_sdk_instance
from atriumdb.dashboard.statistics_resolver import compute_aggregate_statistics

# ---------------------------------------------------------------------------
# Server setup — one server shared across all tests in this module
# ---------------------------------------------------------------------------

PORT = 8124  # distinct from test_api.py (8123)
BASE_URL = f"http://127.0.0.1:{PORT}"


def pytest_configure(config):
    thread = threading.Thread(
        target=lambda: uvicorn.run(app, host="127.0.0.1", port=PORT),
        daemon=True,
    )
    thread.start()
    # wait until the server is accepting connections
    for _ in range(20):
        try:
            requests.get(BASE_URL, timeout=0.5)
            break
        except requests.exceptions.ConnectionError:
            time.sleep(0.2)


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

ONE_HOUR_NS = 3_600 * 1_000_000_000
WINDOW_NS   = 24 * ONE_HOUR_NS
ADMIT_NS    = 1_600_000_000_000_000_000  # 2020-09-13

MEASURE_ID  = 1
PATIENT_ID  = 42
DEVICE_ID   = 7

FULL_COVERAGE = np.array([[ADMIT_NS, ADMIT_NS + WINDOW_NS]])
LOW_COVERAGE  = np.array([[ADMIT_NS, ADMIT_NS + 10 * ONE_HOUR_NS]])  # 10/24 ≈ 41 %

GOOD_VALUES   = np.array([95.0, 96.0, float("nan"), 97.0])  # non-NaN mean = 96.0
ALL_NAN       = np.array([float("nan"), float("nan")])

HEADERS = {"X-Request-ID": "test-req-001"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post(body: dict, headers: dict = HEADERS) -> requests.Response:
    return requests.post(f"{BASE_URL}/cohorts/statistics", json=body, headers=headers)


def _body(mrns_and_admissions: list[tuple[str, list[int]]], availability_threshold: float = 0.5) -> dict:
    return {
        "cohorts": [{
            "id": 1,
            "patients": [
                {"mrn": mrn, "admissions": admissions}
                for mrn, admissions in mrns_and_admissions
            ],
        }],
        "measure": {
            "measureTag": "SpO2",
            "freq": 1.0,
            "units": "%",
            "freqUnits": "Hz",
        },
        "observationWindow": WINDOW_NS,
        "availabilityThreshold": availability_threshold,
    }


def _mock_sdk(
    measure_id=MEASURE_ID,
    patient_id=PATIENT_ID,
    device_id=DEVICE_ID,
    interval_arr=None,
    values=None,
) -> MagicMock:
    sdk = MagicMock()
    sdk.get_measure_id.return_value = measure_id
    sdk.get_patient_id.return_value = patient_id
    sdk.convert_patient_to_device_id.return_value = device_id
    sdk.get_interval_array.return_value = FULL_COVERAGE if interval_arr is None else interval_arr
    sdk.get_data.return_value = (None, None, GOOD_VALUES if values is None else values)
    # Route dashboard_compute_statistics through the real resolver so the
    # individual SDK method stubs above are exercised.
    sdk.dashboard_compute_statistics.side_effect = (
        lambda req, req_id: compute_aggregate_statistics(sdk, req, req_id)
    )
    return sdk


def _cohort(resp: requests.Response) -> dict:
    return resp.json()["cohorts"][0]


def _use(sdk: MagicMock):
    app.dependency_overrides[get_sdk_instance] = lambda: sdk


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_valid_path_single_patient():
    """One patient, one admission — full coverage, good values → included."""
    _use(_mock_sdk())
    resp = _post(_body([("MRN001", [ADMIT_NS])]))

    assert resp.status_code == 200
    cohort = _cohort(resp)
    assert cohort["nPatients"] == 1
    assert cohort["nVisits"] == 1
    assert cohort["nIncluded"] == 1
    assert cohort["nExcluded"] == 0
    assert cohort["exclusions"] == []

    result = cohort["patientResults"][0]
    assert result["mrn"] == "MRN001"
    assert result["admissionNs"] == ADMIT_NS
    assert result["mean"] == pytest.approx(96.0)


def test_valid_path_two_patients():
    """Two patients — both included, means differ by mock order."""
    sdk = _mock_sdk()
    sdk.get_patient_id.side_effect = [10, 20]
    sdk.get_data.side_effect = [
        (None, None, np.array([90.0, 92.0])),
        (None, None, np.array([80.0, 82.0])),
    ]
    _use(sdk)
    resp = _post(_body([("MRN001", [ADMIT_NS]), ("MRN002", [ADMIT_NS])]))

    assert resp.status_code == 200
    cohort = _cohort(resp)
    assert cohort["nPatients"] == 2
    assert cohort["nIncluded"] == 2
    means = {r["mrn"]: r["mean"] for r in cohort["patientResults"]}
    assert means["MRN001"] == pytest.approx(91.0)
    assert means["MRN002"] == pytest.approx(81.0)


def test_mrn_not_found():
    """MRN does not exist in AtriumDB → mrn_not_found exclusion."""
    _use(_mock_sdk(patient_id=None))
    resp = _post(_body([("MRNBAD", [ADMIT_NS])]))

    assert resp.status_code == 200
    cohort = _cohort(resp)
    assert cohort["nIncluded"] == 0
    assert cohort["nExcluded"] == 1
    assert cohort["patientResults"] == []

    exc = cohort["exclusions"][0]
    assert exc["mrn"] == "MRNBAD"
    assert exc["reason"] == "mrn_not_found"
    assert exc["admissionNs"] is None


def test_no_device_found():
    """Patient resolves but no device covers the window → no_device_found exclusion."""
    _use(_mock_sdk(device_id=None))
    resp = _post(_body([("MRN001", [ADMIT_NS])]))

    assert resp.status_code == 200
    cohort = _cohort(resp)
    assert cohort["nIncluded"] == 0

    exc = cohort["exclusions"][0]
    assert exc["reason"] == "no_device_found"
    assert exc["admissionNs"] == ADMIT_NS
    assert exc["windowStartNs"] == ADMIT_NS
    assert exc["windowEndNs"] == ADMIT_NS + WINDOW_NS


def test_below_availability_threshold():
    """Coverage below 50% threshold → below_availability_threshold exclusion."""
    _use(_mock_sdk(interval_arr=LOW_COVERAGE))
    resp = _post(_body([("MRN001", [ADMIT_NS])]))

    assert resp.status_code == 200
    exc = _cohort(resp)["exclusions"][0]
    assert exc["reason"] == "below_availability_threshold"
    assert exc["availability"] == pytest.approx(10 / 24, rel=1e-3)


def test_no_usable_values():
    """All values are NaN → no_usable_values exclusion."""
    _use(_mock_sdk(values=ALL_NAN))
    resp = _post(_body([("MRN001", [ADMIT_NS])]))

    assert resp.status_code == 200
    assert _cohort(resp)["exclusions"][0]["reason"] == "no_usable_values"


def test_multiple_admissions_one_patient():
    """Patient with 2 admissions: first excluded (no device), second included."""
    admit_2 = ADMIT_NS + 30 * 24 * ONE_HOUR_NS

    sdk = _mock_sdk()
    sdk.convert_patient_to_device_id.side_effect = [None, DEVICE_ID]
    _use(sdk)
    resp = _post(_body([("MRN001", [ADMIT_NS, admit_2])]))

    assert resp.status_code == 200
    cohort = _cohort(resp)
    assert cohort["nPatients"] == 1
    assert cohort["nVisits"] == 2
    assert cohort["nIncluded"] == 1
    assert cohort["nExcluded"] == 1

    assert cohort["patientResults"][0]["admissionNs"] == admit_2

    exc = cohort["exclusions"][0]
    assert exc["mrn"] == "MRN001"
    assert exc["admissionNs"] == ADMIT_NS
    assert exc["reason"] == "no_device_found"


def test_missing_request_id_header():
    """No X-Request-ID header → 400."""
    _use(_mock_sdk())
    resp = _post(_body([("MRN001", [ADMIT_NS])]), headers={})

    assert resp.status_code == 400
    assert "X-Request-ID" in resp.json()["detail"]


def test_measure_not_found():
    """Measure absent from dataset → 422."""
    _use(_mock_sdk(measure_id=None))
    resp = _post(_body([("MRN001", [ADMIT_NS])]))

    assert resp.status_code == 422
