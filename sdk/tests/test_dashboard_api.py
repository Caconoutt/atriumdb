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
import shutil
import time
import threading
from pathlib import Path

import pytest
import requests
import uvicorn
from pydantic import ValidationError

from atriumdb.atrium_sdk import AtriumSDK
from atriumdb.dashboard.schemas import (
    AdmissionDateRange, AgeBand, CohortDefinitionRequest,
    DemographicCohort, MrnCohort,
)
from tests.mock_api.app import app
from tests.mock_api.sdk_dependency import get_sdk_instance

DB_NAME = 'dashboard_api_test'
SQLITE_DATASET_PATH = Path(__file__).parent / "test_datasets" / f"sqlite_{DB_NAME}"


def test_api_cohorts():
    def start_server():
        uvicorn.run(app, port=8123)

    api_thread = threading.Thread(target=start_server, daemon=True)
    api_thread.start()

    shutil.rmtree(SQLITE_DATASET_PATH, ignore_errors=True)
    _test_api_cohorts('sqlite', SQLITE_DATASET_PATH, None)


def _test_api_cohorts(db_type, dataset_location, connection_params):
    sdk = AtriumSDK.create_dataset(
        dataset_location=dataset_location, database_type=db_type, connection_params=connection_params)

    app.dependency_overrides[get_sdk_instance] = lambda: sdk

    api_sdk = AtriumSDK(metadata_connection_type="api", api_url="http://127.0.0.1:8123", validate_token=False)
    api_sdk.token_expiry = time.time() + 1_000_000

    # --- set up location infrastructure (institution → unit → bed) ---
    institution_id = sdk.sql_handler.insert_institution("Test Hospital")
    unit_id = sdk.sql_handler.insert_unit(institution_id, "ICU", "icu")
    bed_id = sdk.sql_handler.insert_bed(unit_id, "Bed 1")

    ONE_YEAR_NS = 365 * 24 * 3600 * 1_000_000_000
    admit_start_ns = 1_600_000_000_000_000_000
    admit_end_ns   = 1_700_000_000_000_000_000
    inside_ns      = 1_650_000_000_000_000_000  # within the admission window
    outside_ns     = 1_500_000_000_000_000_000  # before the admission window

    # patient A: male, 25 years old at admission, has in-window encounter in ICU
    pid_a = sdk.insert_patient(mrn="MRN001", gender="M", dob=inside_ns - 25 * ONE_YEAR_NS)
    # patient B: female, 35 years old at admission, has in-window encounter in ICU
    pid_b = sdk.insert_patient(mrn="MRN002", gender="F", dob=inside_ns - 35 * ONE_YEAR_NS)
    # patient C: male, but encounter is outside the admission window
    pid_c = sdk.insert_patient(mrn="MRN003", gender="M", dob=inside_ns - 25 * ONE_YEAR_NS)

    sdk.insert_encounter(patient_id=pid_a, bed_id=bed_id, start_time=inside_ns,
                         end_time=inside_ns + ONE_YEAR_NS, visit_number="V001")
    sdk.insert_encounter(patient_id=pid_b, bed_id=bed_id,
                         start_time=inside_ns + ONE_YEAR_NS // 2,
                         end_time=inside_ns + ONE_YEAR_NS, visit_number="V002")
    sdk.insert_encounter(patient_id=pid_c, bed_id=bed_id, start_time=outside_ns,
                         end_time=outside_ns + ONE_YEAR_NS, visit_number="V003")

    date_range = AdmissionDateRange(start=admit_start_ns, end=admit_end_ns)

    print("Testing 1A: MRN cohort endpoint...")

    request_1a = CohortDefinitionRequest(
        type="mrn",
        admission_date_range=date_range,
        cohorts=[MrnCohort(id="cohort_1a", mrn_list=["MRN001", "MRN002", "MRN003", "MRN999"])],
    )

    # MRN001 and MRN002 have in-window encounters; MRN003 is outside the window; MRN999 does not exist
    local_result = sdk.dashboard_resolve_cohort(request_1a, request_id="test-1a-local")
    assert local_result.request_id == "test-1a-local"
    assert len(local_result.cohorts) == 1
    assert local_result.cohorts[0].id == "cohort_1a"
    assert {p.mrn for p in local_result.cohorts[0].patients} == {"MRN001", "MRN002"}

    api_result = api_sdk.dashboard_resolve_cohort(request_1a, request_id="test-1a-api")
    assert {p.mrn for p in api_result.cohorts[0].patients} == {"MRN001", "MRN002"}

    print("Testing 1B: demographic cohort — location filter...")

    request_1b_loc = CohortDefinitionRequest(
        type="demographic",
        admission_date_range=date_range,
        cohorts=[DemographicCohort(id="cohort_1b_loc", location=["ICU"])],
    )

    # both in-window patients are in ICU; MRN003 is outside the window
    local_result = sdk.dashboard_resolve_cohort(request_1b_loc, request_id="test-1b-loc-local")
    assert {p.mrn for p in local_result.cohorts[0].patients} == {"MRN001", "MRN002"}

    api_result = api_sdk.dashboard_resolve_cohort(request_1b_loc, request_id="test-1b-loc-api")
    assert {p.mrn for p in api_result.cohorts[0].patients} == {"MRN001", "MRN002"}

    print("Testing 1B: demographic cohort — sex filter...")

    request_1b_sex = CohortDefinitionRequest(
        type="demographic",
        admission_date_range=date_range,
        cohorts=[DemographicCohort(id="cohort_1b_sex", location=["ICU"], sex=["M"])],
    )

    local_result = sdk.dashboard_resolve_cohort(request_1b_sex, request_id="test-1b-sex-local")
    assert {p.mrn for p in local_result.cohorts[0].patients} == {"MRN001"}

    api_result = api_sdk.dashboard_resolve_cohort(request_1b_sex, request_id="test-1b-sex-api")
    assert {p.mrn for p in api_result.cohorts[0].patients} == {"MRN001"}

    print("Testing 1B: demographic cohort — age filter...")

    # patient A is 25 years old at admission → falls in [20, 30]; patient B is 35 → does not
    request_1b_age = CohortDefinitionRequest(
        type="demographic",
        admission_date_range=date_range,
        cohorts=[DemographicCohort(
            id="cohort_1b_age",
            location=["ICU"],
            age=[AgeBand(start_ns=20 * ONE_YEAR_NS, end_ns=30 * ONE_YEAR_NS)],
        )],
    )

    local_result = sdk.dashboard_resolve_cohort(request_1b_age, request_id="test-1b-age-local")
    assert {p.mrn for p in local_result.cohorts[0].patients} == {"MRN001"}

    api_result = api_sdk.dashboard_resolve_cohort(request_1b_age, request_id="test-1b-age-api")
    assert {p.mrn for p in api_result.cohorts[0].patients} == {"MRN001"}

    print("Testing 1B: demographic cohort — multiple cohorts in one request...")

    request_multi = CohortDefinitionRequest(
        type="demographic",
        admission_date_range=date_range,
        cohorts=[
            DemographicCohort(id="male_icu",  location=["ICU"], sex=["M"]),
            DemographicCohort(id="female_icu", location=["ICU"], sex=["F"]),
        ],
    )

    local_result = sdk.dashboard_resolve_cohort(request_multi, request_id="test-1b-multi-local")
    assert len(local_result.cohorts) == 2
    cohorts_by_id = {c.id: {p.mrn for p in c.patients} for c in local_result.cohorts}
    assert cohorts_by_id["male_icu"] == {"MRN001"}
    assert cohorts_by_id["female_icu"] == {"MRN002"}

    api_result = api_sdk.dashboard_resolve_cohort(request_multi, request_id="test-1b-multi-api")
    cohorts_by_id = {c.id: {p.mrn for p in c.patients} for c in api_result.cohorts}
    assert cohorts_by_id["male_icu"] == {"MRN001"}
    assert cohorts_by_id["female_icu"] == {"MRN002"}

    _test_invalid_input_is_rejected(date_range)

    api_sdk.close()


def test_cohort_request_validation():
    """Invalid cohort input is rejected when the request model is built.

    This is the check that a direct-DB caller and the API-mode SDK client both
    hit, before any database or HTTP work happens. It needs no dataset, so it
    runs independently of :func:`test_api_cohorts`. The matching HTTP status
    codes are asserted in :func:`_test_invalid_input_is_rejected`.
    """
    with pytest.raises(ValidationError, match="Unknown location code"):
        DemographicCohort(id="bad_loc", location=["ICU", "ER"])

    with pytest.raises(ValidationError, match="Unknown sex code"):
        DemographicCohort(id="bad_sex", location=["ICU"], sex=["X"])

    # only M and F are requestable — there is no code for unknown sex
    with pytest.raises(ValidationError, match="Unknown sex code"):
        DemographicCohort(id="unknown_sex", sex=["U"])

    with pytest.raises(ValidationError, match="must not be after"):
        AdmissionDateRange(start=200, end=100)

    # sex codes are case-insensitive and normalised, not rejected
    assert DemographicCohort(id="lower_sex", sex=["m", " f "]).sex == ["M", "F"]

    # valid input still passes, including a single-instant date range
    assert DemographicCohort(id="ok", location=["ICU", "OR"]).location == ["ICU", "OR"]
    assert AdmissionDateRange(start=100, end=100).end == 100

    # unfiltered cohorts remain valid — None means "no filter", not "invalid"
    unfiltered = DemographicCohort(id="ok")
    assert unfiltered.location is None and unfiltered.sex is None

    # the same rejection applies when parsed from camelCase JSON, which is the
    # form FastAPI hands the model
    with pytest.raises(ValidationError, match="Unknown location code"):
        CohortDefinitionRequest.model_validate({
            "type": "demographic",
            "admissionDateRange": {"start": 1, "end": 2},
            "cohorts": [{"id": "c", "location": ["ER"]}],
        })


def _test_invalid_input_is_rejected(date_range):
    """Bad user input must surface as a 422 over HTTP, never as a 500.

    :func:`test_cohort_request_validation` covers the model-construction side;
    this covers the status code the dashboard server actually receives, which
    only a live request can show.
    """
    print("Testing input validation over HTTP...")

    base_url = "http://127.0.0.1:8123"
    valid_body = CohortDefinitionRequest(
        type="demographic",
        admission_date_range=date_range,
        cohorts=[DemographicCohort(id="cohort_1b_loc", location=["ICU"])],
    ).model_dump(by_alias=True)

    # sanity check: the unmodified body and header pair is accepted
    response = requests.post(
        f"{base_url}/cohorts", json=valid_body, headers={"X-Request-ID": "test-valid"}
    )
    assert response.status_code == 200, response.text

    bad_location_body = {
        **valid_body,
        "cohorts": [{"id": "bad_loc", "location": ["ER"]}],
    }
    response = requests.post(
        f"{base_url}/cohorts",
        json=bad_location_body,
        headers={"X-Request-ID": "test-bad-location"},
    )
    assert response.status_code == 422, response.text
    assert "Unknown location code" in response.text

    bad_sex_body = {
        **valid_body,
        "cohorts": [{"id": "bad_sex", "location": ["ICU"], "sex": ["X"]}],
    }
    response = requests.post(
        f"{base_url}/cohorts",
        json=bad_sex_body,
        headers={"X-Request-ID": "test-bad-sex"},
    )
    assert response.status_code == 422, response.text
    assert "Unknown sex code" in response.text

    inverted_range_body = {
        **valid_body,
        "admissionDateRange": {"start": date_range.end, "end": date_range.start},
    }
    response = requests.post(
        f"{base_url}/cohorts",
        json=inverted_range_body,
        headers={"X-Request-ID": "test-inverted-range"},
    )
    assert response.status_code == 422, response.text
    assert "must not be after" in response.text

    print("Testing input validation: blank X-Request-ID header...")

    # min_length=1 alone lets an all-whitespace header through, which used to
    # reach the SDK's own request_id check and surface as a 500
    for bad_request_id in ("", "   "):
        response = requests.post(
            f"{base_url}/cohorts",
            json=valid_body,
            headers={"X-Request-ID": bad_request_id},
        )
        assert response.status_code == 422, (
            f"X-Request-ID={bad_request_id!r} returned "
            f"{response.status_code}: {response.text}"
        )
