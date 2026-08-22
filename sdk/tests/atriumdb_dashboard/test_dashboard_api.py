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
from atriumdb_dashboard.api.app import mount_dashboard
# One provider shared by every dashboard router, so a single override covers
# all of them.
from atriumdb_dashboard.api.dependencies import get_sdk_instance
from atriumdb_dashboard.cohort_resolver import resolve_cohort
from atriumdb_dashboard.locations import UnknownLocationError
from atriumdb_dashboard.queries import (
    group_encounters_by_admission,
    query_measure_total_hours,
    query_patient_encounters,
    select_patient_encounters,
)
from atriumdb_dashboard.schemas import (
    AdmissionDateRange, AgeBand, CohortDefinitionRequest,
    DemographicCohort, MrnCohort,
)
from tests.mock_api.app import app

# Mount the dashboard onto the upstream AtriumDB test app at runtime, rather
# than editing the upstream endpoint modules, so they stay identical to main.
mount_dashboard(app)

DB_NAME = 'dashboard_api_test'
SQLITE_DATASET_PATH = Path(__file__).parent.parent / "test_datasets" / f"sqlite_{DB_NAME}"

DB_NAME_HOURS = 'dashboard_api_hours_test'
SQLITE_DATASET_PATH_HOURS = Path(__file__).parent.parent / "test_datasets" / f"sqlite_{DB_NAME_HOURS}"

# One server on one port for the whole module. Both routers are mounted on the
# same `app`, which is how they are deployed, so serving them from two ports
# would have tested the identical object twice while proving nothing about
# either. A single server is also the only arrangement that can catch the
# routers interfering with one another.
API_PORT = 8123
BASE_URL = f"http://127.0.0.1:{API_PORT}"


@pytest.fixture(scope="module", autouse=True)
def api_server():
    """Start the test API server once, and wait until it answers.

    Session-wide rather than per-test: ``uvicorn.run`` binds the port, so a
    second test starting its own server on the same port would fail with
    "address already in use".

    Readiness is polled rather than slept on. A fixed sleep is both slower than
    it needs to be and unreliable under load, and the previous arrangement was
    worse than it looked — only one of the two tests waited at all, the other
    happening to work because building its dataset took longer than uvicorn
    took to bind.
    """
    threading.Thread(
        target=lambda: uvicorn.run(app, port=API_PORT, log_level="warning"),
        daemon=True,
    ).start()

    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            requests.get(f"{BASE_URL}/openapi.json", timeout=0.2)
            return
        except requests.exceptions.RequestException:
            time.sleep(0.05)

    raise RuntimeError(f"test API server did not come up on port {API_PORT}")


@pytest.fixture(autouse=True)
def clear_dependency_overrides():
    """Drop dependency overrides after each test.

    ``app`` is a module-level singleton shared by every test, so an override
    left behind outlives the SDK it closes over — and the dataset directory
    that SDK points at is deleted by the next run. Clearing keeps the tests
    order-independent.
    """
    yield
    app.dependency_overrides.clear()


def test_api_cohorts():
    shutil.rmtree(SQLITE_DATASET_PATH, ignore_errors=True)
    _test_api_cohorts('sqlite', SQLITE_DATASET_PATH, None)


def _test_api_cohorts(db_type, dataset_location, connection_params):
    sdk = AtriumSDK.create_dataset(
        dataset_location=dataset_location, database_type=db_type, connection_params=connection_params)

    app.dependency_overrides[get_sdk_instance] = lambda: sdk

    api_sdk = AtriumSDK(metadata_connection_type="api", api_url=BASE_URL, validate_token=False)
    api_sdk.token_expiry = time.time() + 1_000_000

    # --- set up location infrastructure (institution → unit → bed) ---
    institution_id = sdk.sql_handler.insert_institution("Test Hospital")
    unit_id = sdk.sql_handler.insert_unit(institution_id, "ICU", "icu")
    bed_id = sdk.sql_handler.insert_bed(unit_id, "Bed 1")
    # A unit that the removed hardcoded LOCATION_LOOKUP never listed. Requests
    # naming it must work purely because the row exists in the unit table.
    nicu_unit_id = sdk.sql_handler.insert_unit(institution_id, "NICU", "icu")
    nicu_bed_id = sdk.sql_handler.insert_bed(nicu_unit_id, "NICU Bed 1")
    # Units used only by the grouping test, kept separate from ICU and NICU so
    # that its extra patients cannot perturb the cohort assertions above.
    step_unit_id = sdk.sql_handler.insert_unit(institution_id, "STEPDOWN", "ward")
    step_bed_1 = sdk.sql_handler.insert_bed(step_unit_id, "Step Bed 1")
    step_bed_2 = sdk.sql_handler.insert_bed(step_unit_id, "Step Bed 2")
    recov_unit_id = sdk.sql_handler.insert_unit(institution_id, "RECOVERY", "ward")
    recov_bed_1 = sdk.sql_handler.insert_bed(recov_unit_id, "Recovery Bed 1")

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

    # patient D: in the NICU, in-window — only reachable via a DB-backed lookup
    pid_d = sdk.insert_patient(mrn="MRN004", gender="F", dob=inside_ns - 2 * ONE_YEAR_NS)
    sdk.insert_encounter(patient_id=pid_d, bed_id=nicu_bed_id, start_time=inside_ns,
                         end_time=inside_ns + ONE_YEAR_NS, visit_number="V004")

    date_range = AdmissionDateRange(start=admit_start_ns, end=admit_end_ns)

    print("Testing 1A: MRN cohort endpoint...")

    request_1a = CohortDefinitionRequest(
        type="mrn",
        admission_date_range=date_range,
        cohorts=[MrnCohort(id="cohort_1a", mrn_list=["MRN001", "MRN002", "MRN003", "MRN999"])],
    )

    # MRN001 and MRN002 have in-window encounters; MRN003 is outside the window; MRN999 does not exist
    local_result = resolve_cohort(sdk, request_1a, request_id="test-1a-local")
    assert local_result.request_id == "test-1a-local"
    assert len(local_result.cohorts) == 1
    assert local_result.cohorts[0].id == "cohort_1a"
    assert {p.mrn for p in local_result.cohorts[0].patients} == {"MRN001", "MRN002"}

    api_result = resolve_cohort(api_sdk, request_1a, request_id="test-1a-api")
    assert {p.mrn for p in api_result.cohorts[0].patients} == {"MRN001", "MRN002"}

    print("Testing 1B: demographic cohort — location filter...")

    request_1b_loc = CohortDefinitionRequest(
        type="demographic",
        admission_date_range=date_range,
        cohorts=[DemographicCohort(id="cohort_1b_loc", location=["ICU"])],
    )

    # both in-window patients are in ICU; MRN003 is outside the window
    local_result = resolve_cohort(sdk, request_1b_loc, request_id="test-1b-loc-local")
    assert {p.mrn for p in local_result.cohorts[0].patients} == {"MRN001", "MRN002"}

    api_result = resolve_cohort(api_sdk, request_1b_loc, request_id="test-1b-loc-api")
    assert {p.mrn for p in api_result.cohorts[0].patients} == {"MRN001", "MRN002"}

    print("Testing 1B: demographic cohort — sex filter...")

    request_1b_sex = CohortDefinitionRequest(
        type="demographic",
        admission_date_range=date_range,
        cohorts=[DemographicCohort(id="cohort_1b_sex", location=["ICU"], sex=["M"])],
    )

    local_result = resolve_cohort(sdk, request_1b_sex, request_id="test-1b-sex-local")
    assert {p.mrn for p in local_result.cohorts[0].patients} == {"MRN001"}

    api_result = resolve_cohort(api_sdk, request_1b_sex, request_id="test-1b-sex-api")
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

    local_result = resolve_cohort(sdk, request_1b_age, request_id="test-1b-age-local")
    assert {p.mrn for p in local_result.cohorts[0].patients} == {"MRN001"}

    api_result = resolve_cohort(api_sdk, request_1b_age, request_id="test-1b-age-api")
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

    local_result = resolve_cohort(sdk, request_multi, request_id="test-1b-multi-local")
    assert len(local_result.cohorts) == 2
    cohorts_by_id = {c.id: {p.mrn for p in c.patients} for c in local_result.cohorts}
    assert cohorts_by_id["male_icu"] == {"MRN001"}
    assert cohorts_by_id["female_icu"] == {"MRN002"}

    api_result = resolve_cohort(api_sdk, request_multi, request_id="test-1b-multi-api")
    cohorts_by_id = {c.id: {p.mrn for p in c.patients} for c in api_result.cohorts}
    assert cohorts_by_id["male_icu"] == {"MRN001"}
    assert cohorts_by_id["female_icu"] == {"MRN002"}

    print("Testing: admissions group by (patient, visit_number, unit)...")

    # Patient F, one visit, three encounter rows: two STEPDOWN beds then a
    # transfer to RECOVERY. The bed move within STEPDOWN must collapse into a
    # single admission spanning both; the unit transfer must stay separate, so
    # that each admission carries one unambiguous location.
    pid_f = sdk.insert_patient(mrn="MRN006", gender="M", dob=inside_ns - 10 * ONE_YEAR_NS)
    t0 = inside_ns
    t1 = inside_ns + ONE_YEAR_NS // 4
    t2 = inside_ns + ONE_YEAR_NS // 2
    t3 = inside_ns + ONE_YEAR_NS
    sdk.insert_encounter(patient_id=pid_f, bed_id=step_bed_1, start_time=t0, end_time=t1, visit_number="V006")
    sdk.insert_encounter(patient_id=pid_f, bed_id=step_bed_2, start_time=t1, end_time=t2, visit_number="V006")
    sdk.insert_encounter(patient_id=pid_f, bed_id=recov_bed_1, start_time=t2, end_time=t3, visit_number="V006")

    grouped = group_encounters_by_admission(
        query_patient_encounters(sdk, patient_id_list=[pid_f])
    )
    assert sorted(grouped) == [(pid_f, "V006", "RECOVERY"), (pid_f, "V006", "STEPDOWN")], grouped

    # STEPDOWN admission spans both beds: MIN(start) .. MAX(end)
    step = grouped[(pid_f, "V006", "STEPDOWN")]
    assert (step["admit_time_ns"], step["discharge_time_ns"]) == (t0, t2)

    # the transfer is its own admission, not merged into the STEPDOWN one
    recov = grouped[(pid_f, "V006", "RECOVERY")]
    assert (recov["admit_time_ns"], recov["discharge_time_ns"]) == (t2, t3)

    # an open row anywhere in a group leaves the whole admission open
    pid_g = sdk.insert_patient(mrn="MRN007", gender="F", dob=inside_ns - 10 * ONE_YEAR_NS)
    sdk.insert_encounter(patient_id=pid_g, bed_id=step_bed_1, start_time=t0, end_time=t1, visit_number="V007")
    sdk.insert_encounter(patient_id=pid_g, bed_id=step_bed_2, start_time=t1, end_time=None, visit_number="V007")
    open_stay = group_encounters_by_admission(
        query_patient_encounters(sdk, patient_id_list=[pid_g])
    )[(pid_g, "V007", "STEPDOWN")]
    assert open_stay["admit_time_ns"] == t0
    assert open_stay["discharge_time_ns"] is None

    print("Testing: encounters with no visit_number are excluded...")

    # patient E is admitted to the ICU in-window but the row carries no visit
    # number, so it cannot be attributed to a stay and must not resolve.
    pid_e = sdk.insert_patient(mrn="MRN005", gender="F", dob=inside_ns - 40 * ONE_YEAR_NS)
    sdk.insert_encounter(patient_id=pid_e, bed_id=bed_id, start_time=inside_ns,
                         end_time=inside_ns + ONE_YEAR_NS, visit_number=None)

    assert pid_e not in {r[1] for r in select_patient_encounters(sdk)}

    # the ICU cohort is unchanged by patient E's presence in the table
    local_result = resolve_cohort(sdk, request_1b_loc, request_id="test-null-visit")
    assert {p.mrn for p in local_result.cohorts[0].patients} == {"MRN001", "MRN002"}

    # and an MRN cohort naming them reports no qualifying admission
    request_null_visit = CohortDefinitionRequest(
        type="mrn",
        admission_date_range=date_range,
        cohorts=[MrnCohort(id="null_visit", mrn_list=["MRN005"])],
    )
    result = resolve_cohort(sdk, request_null_visit, request_id="test-null-visit-mrn")
    assert result.cohorts[0].patients == []

    print("Testing 1B: location validated against the unit table, not a constant...")

    # "NICU" was never in the old hardcoded vocabulary. It resolves now purely
    # because a unit row with that name exists — which is the point of the
    # DB-backed lookup.
    request_nicu = CohortDefinitionRequest(
        type="demographic",
        admission_date_range=date_range,
        cohorts=[DemographicCohort(id="nicu", location=["NICU"])],
    )
    local_result = resolve_cohort(sdk, request_nicu, request_id="test-nicu-local")
    assert {p.mrn for p in local_result.cohorts[0].patients} == {"MRN004"}
    assert local_result.cohorts[0].patients[0].admissions[0].location == "NICU"

    api_result = resolve_cohort(api_sdk, request_nicu, request_id="test-nicu-api")
    assert {p.mrn for p in api_result.cohorts[0].patients} == {"MRN004"}

    # A name with no unit row is still rejected rather than silently widening
    # the cohort to every admitted patient.
    request_absent = CohortDefinitionRequest(
        type="demographic",
        admission_date_range=date_range,
        cohorts=[DemographicCohort(id="absent", location=["WARD_9"])],
    )
    with pytest.raises(UnknownLocationError, match="WARD_9"):
        resolve_cohort(sdk, request_absent, request_id="test-absent-local")

    _test_invalid_input_is_rejected(sdk, date_range)

    api_sdk.close()


def test_cohort_request_validation():
    """Invalid cohort input is rejected when the request model is built.

    This is the check that a direct-DB caller and the API-mode SDK client both
    hit, before any database or HTTP work happens. It needs no dataset, so it
    runs independently of :func:`test_api_cohorts`. The matching HTTP status
    codes are asserted in :func:`_test_invalid_input_is_rejected`.
    """
    # Locations are NOT validated here: the valid set is the unit table, which
    # a Pydantic validator cannot reach. Any string is accepted at construction
    # and checked at resolve time instead — see the 422 assertion in
    # _test_invalid_input_is_rejected, which covers the same input over HTTP.
    assert DemographicCohort(id="unchecked_loc", location=["ER"]).location == ["ER"]

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

    # camelCase JSON — the form FastAPI hands the model — parses the same way,
    # with the location carried through unvalidated for the resolver to check
    parsed = CohortDefinitionRequest.model_validate({
        "type": "demographic",
        "admissionDateRange": {"start": 1, "end": 2},
        "cohorts": [{"id": "c", "location": ["ER"]}],
    })
    assert parsed.cohorts[0].location == ["ER"]


def _test_invalid_input_is_rejected(sdk, date_range):
    """Bad user input must surface as a 422 over HTTP, never as a 500.

    :func:`test_cohort_request_validation` covers the model-construction side;
    this covers the status code the dashboard server actually receives, which
    only a live request can show.

    :param sdk: Direct-DB SDK instance, used for the checks that cannot be
        driven through an HTTP client.
    :param date_range: Admission window shared by the request bodies built here.
    """
    print("Testing input validation over HTTP...")

    base_url = BASE_URL
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

    # An empty header value is legal HTTP, so it reaches the server and the
    # endpoint's min_length=1 rejects it.
    response = requests.post(
        f"{base_url}/cohorts",
        json=valid_body,
        headers={"X-Request-ID": ""},
    )
    assert response.status_code == 422, (
        f"X-Request-ID='' returned {response.status_code}: {response.text}"
    )

    # An all-whitespace value never reaches the server at all: requests
    # validates header values before sending and refuses this one. Asserting
    # the client-side error is the honest test — there is no response to check
    # a status code on, and pretending otherwise would just re-break whenever
    # requests tightens validation again.
    with pytest.raises(requests.exceptions.InvalidHeader):
        requests.post(
            f"{base_url}/cohorts",
            json=valid_body,
            headers={"X-Request-ID": "   "},
        )

    # The server-side guard against a blank request_id is still real — it is
    # simply not reachable through requests. Assert it where it actually lives,
    # which also covers direct-DB callers who never touch HTTP. The check runs
    # before any query, so no dataset state is involved.
    for blank in ("", "   ", "\t"):
        with pytest.raises(ValueError, match="non-empty string"):
            resolve_cohort(
                sdk,
                CohortDefinitionRequest.model_validate(valid_body),
                request_id=blank,
            )


def test_api_measure_total_hours():
    shutil.rmtree(SQLITE_DATASET_PATH_HOURS, ignore_errors=True)
    _test_api_measure_total_hours('sqlite', SQLITE_DATASET_PATH_HOURS, None)


def _test_api_measure_total_hours(db_type, dataset_location, connection_params):
    sdk = AtriumSDK.create_dataset(
        dataset_location=dataset_location, database_type=db_type, connection_params=connection_params)

    app.dependency_overrides[get_sdk_instance] = lambda: sdk

    # --- create measures and devices ---
    hr_id = sdk.insert_measure(measure_tag="HR", freq=1, freq_units="Hz", units="BPM")
    spo2_id = sdk.insert_measure(measure_tag="SpO2", freq=1, freq_units="Hz", units="%")
    dev1_id = sdk.insert_device(device_tag="monitor_1")
    dev2_id = sdk.insert_device(device_tag="monitor_2")

    # Seed block_index directly — no C library (libTSC.so) needed.
    # The query under test reads block_index.num_values and converts to hours
    # using freq_nhz.  HR and SpO2 both have freq=1 Hz → freq_nhz=1_000_000_000.
    #   HR   / device 1 → 7200 values  (= 2 h at 1 Hz)
    #   HR   / device 2 → 3600 values  (= 1 h at 1 Hz)
    #   SpO2 / device 1 → 3600 values  (= 1 h at 1 Hz)
    _NS_PER_HOUR = 3_600_000_000_000
    base_ns = 1_700_000_000 * 1_000_000_000
    with sdk.sql_handler.connection(begin=True) as (conn, cursor):
        cursor.executemany(
            "INSERT INTO block_index "
            "(measure_id, device_id, file_id, start_byte, num_bytes, start_time_n, end_time_n, num_values) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (hr_id,   dev1_id, 0, 0, 0, base_ns, base_ns + 2 * _NS_PER_HOUR, 7200),
                (hr_id,   dev2_id, 0, 0, 0, base_ns, base_ns + 1 * _NS_PER_HOUR, 3600),
                (spo2_id, dev1_id, 0, 0, 0, base_ns, base_ns + 1 * _NS_PER_HOUR, 3600),
            ],
        )

    print("Testing measure_total_hours: local helper...")

    local_result = query_measure_total_hours(sdk)
    assert len(local_result) == 2

    by_tag = {r["measure_tag"]: r for r in local_result}

    assert set(by_tag["HR"].keys()) == {"measure_id", "measure_tag", "freq_nhz", "units",
                                        "total_num_values", "total_ns", "total_hours"}
    assert abs(by_tag["HR"]["total_hours"] - 3.0) < 1e-6
    assert by_tag["HR"]["total_num_values"] == 10800
    assert abs(by_tag["SpO2"]["total_hours"] - 1.0) < 1e-6
    assert by_tag["SpO2"]["total_num_values"] == 3600

    print("Testing measure_total_hours: API endpoint GET /measures/hours...")

    resp = requests.get(f"{BASE_URL}/measures/hours", timeout=10)
    assert resp.status_code == 200

    api_result = resp.json()
    assert len(api_result) == 2

    by_tag_api = {r["measure_tag"]: r for r in api_result}

    assert abs(by_tag_api["HR"]["total_hours"] - 3.0) < 1e-6
    assert by_tag_api["HR"]["total_num_values"] == 10800
    assert abs(by_tag_api["SpO2"]["total_hours"] - 1.0) < 1e-6
    assert by_tag_api["SpO2"]["total_num_values"] == 3600

    print("All measure_total_hours tests passed.")
