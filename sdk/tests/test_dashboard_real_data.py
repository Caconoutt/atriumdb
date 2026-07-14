"""Real-dataset tests for dashboard cohort resolution.

These tests require a real AtriumDB dataset to be mounted into the container.
They are skipped automatically when ATRIUMDB_DATASET_LOCATION is not set, so
they never block the normal CI test run.

To run them:

    docker run --rm -it \\
      -v "/host/path/to/dataset":/data/atriumdb \\
      --env-file .env \\
      atriumdb-sdk python -m pytest tests/test_dashboard_real_data.py -v -s

Workflow
--------
1. Run ``test_inspect_real_dataset`` first (-v -s) to see what patients,
   MRNs, encounters, and units are actually in the dataset.
2. Copy a few real MRNs from that output into ``test_mrn_cohort_real_data``
   and adjust the admission date range to match your data.
3. Run ``test_demographic_cohort_real_data`` to verify demographic filtering
   works end-to-end against the real schema.
"""

import os

import pytest

from atriumdb import AtriumSDK
from atriumdb.dashboard.schemas import (
    Admission,
    AdmissionDateRange,
    AgeBand,
    CohortDefinitionRequest,
    DemographicCohort,
    MrnCohort,
    PatientAdmission,
)

DATASET_LOCATION = os.environ.get("ATRIUMDB_DATASET_LOCATION")

pytestmark = pytest.mark.skipif(
    not DATASET_LOCATION,
    reason="ATRIUMDB_DATASET_LOCATION not set — skipping real-data tests. "
           "See dockersetup.md section 4 for how to mount a dataset.",
)


@pytest.fixture(scope="module")
def sdk():
    return AtriumSDK(dataset_location=DATASET_LOCATION, auto_upgrade=True)


# ---------------------------------------------------------------------------
# Step 1 — discovery: run this first to learn what is in the dataset
# ---------------------------------------------------------------------------

def test_inspect_real_dataset(sdk):
    """Print a summary of patients, MRNs, encounters, and units in the dataset.

    Run with -v -s to see the full output.  Use this output to fill in the
    real MRNs and date ranges used by the tests below.
    """
    patients = sdk.sql_handler.select_all_patients_in_list()
    print(f"\n{'='*60}")
    print(f"PATIENTS ({len(patients)} total — showing first 30)")
    print(f"{'='*60}")
    print(f"  {'id':>6}  {'mrn':<20}  {'gender':<8}  {'dob (ns)'}")
    for row in patients[:30]:
        pid, mrn, gender, dob = row[0], row[1], row[2], row[3]
        print(f"  {pid:>6}  {str(mrn):<20}  {str(gender):<8}  {dob}")
    if len(patients) > 30:
        print(f"  ... and {len(patients) - 30} more")

    encounters = sdk.sql_handler.select_patient_encounters()
    print(f"\n{'='*60}")
    print(f"ENCOUNTERS ({len(encounters)} total — showing first 20)")
    print(f"{'='*60}")
    print(f"  {'enc_id':>7}  {'patient_id':>10}  {'visit_number':<16}  "
          f"{'unit_name':<12}  start_time_ns")
    for row in encounters[:20]:
        enc_id, pid, visit, bed_id, unit_id, unit_name, start_ns, end_ns = row
        print(f"  {enc_id:>7}  {pid:>10}  {str(visit):<16}  "
              f"{str(unit_name):<12}  {start_ns}")

    if encounters:
        start_times = [r[6] for r in encounters if r[6] is not None]
        if start_times:
            print(f"\n  Encounter start_time range:")
            print(f"    min = {min(start_times)}")
            print(f"    max = {max(start_times)}")

    print(f"\n{'='*60}")
    print("HINT: copy MRNs from the patient list above into")
    print("test_mrn_cohort_real_data, and set admit_start_ns / admit_end_ns")
    print("to bracket the encounter start_time range shown above.")
    print(f"{'='*60}\n")

    assert len(patients) > 0, "Dataset has no patients — check the mount path."


# ---------------------------------------------------------------------------
# Step 2 — MRN cohort (Priority 1A)
#
# Admission window: 2020-01-01 00:00:00 UTC → 2021-01-01 00:00:00 UTC.
#
# MRN_COHORTS: list of (cohort_id, mrn_list) pairs — one cohort per entry.
# EXPECTED_MRN_COHORTS: maps cohort_id → set of MRNs expected to resolve.
#   - MRNs in the input but NOT in the expected set are expected to be
#     excluded (don't exist or have no in-window encounter).
#   - Leave the dict empty while still exploring; the test still runs and
#     prints resolved MRNs without asserting specific values.
# ---------------------------------------------------------------------------

MRN_ADMIT_START_NS: int = 1577836800000000000  # 2020-01-01 00:00:00 UTC
MRN_ADMIT_END_NS:   int = 1609459200000000000  # 2021-01-01 00:00:00 UTC

MRN_COHORTS: list[tuple[str, list[str]]] = [
    ("cohort_a", ["MRN3E7DB077", "MRN3279EDCF", "MRN824A6D3E", "MRNFD5F56B4"]),
    ("cohort_b", ["MRN885332B7", "MRN9D06E5D3"]),
]

# Maps cohort_id → expected set of MRNs.
EXPECTED_MRN_COHORTS: dict[str, set[str]] = {
    "cohort_a": {"MRN3E7DB077", "MRN3279EDCF", "MRN824A6D3E"},  # MRNFD5F56B4 excluded (non-existent)
    "cohort_b": {"MRN885332B7", "MRN9D06E5D3"},
}

# Maps cohort_id → {mrn: admissions}.
# Fill in once you know the admissions values from the printed output below.
# Only cohorts with entries here are validated on admission time.
EXPECTED_MRN_ADMISSIONS: dict[str, dict[str, list[int]]] = {
    "cohort_a": {
        "MRN3E7DB077": [1584420294000000000],
        "MRN3279EDCF": [1602609105000000000],
        "MRN824A6D3E": [1608553652000000000],
    },
    "cohort_b": {
        "MRN885332B7": [1597153068000000000],
        "MRN9D06E5D3": [1605016227000000000],
    },
}


@pytest.mark.skipif(not MRN_COHORTS, reason="MRN_COHORTS is empty — fill it in to run this test")
def test_mrn_cohort_real_data(sdk):
    """1A: MRN cohort — input MRNs are validated against AtriumDB and filtered
    by admission window. Resolved sets are printed and validated against
    EXPECTED_MRN_COHORTS and EXPECTED_MRN_ADMISSIONS where entries are filled in.
    """
    date_range = AdmissionDateRange(start=MRN_ADMIT_START_NS, end=MRN_ADMIT_END_NS)
    request = CohortDefinitionRequest(
        type="mrn",
        admission_date_range=date_range,
        cohorts=[MrnCohort(id=cid, mrn_list=mrns) for cid, mrns in MRN_COHORTS],
    )
    result = sdk.dashboard_resolve_cohort(request, request_id="real-1a")
    # by_id: cohort_id → list[PatientAdmission]
    by_id: dict[str, list[PatientAdmission]] = {
        c.id: c.patients for c in result.cohorts
    }

    print(f"\n{'='*60}")
    for cohort_id, patients in by_id.items():
        print(f"  {cohort_id} ({len(patients)} resolved):")
        for p in sorted(patients, key=lambda p: p.mrn):
            print(f"    mrn={p.mrn}  admissions={p.admissions}")
    print(f"{'='*60}")

    # Resolved MRNs must be a subset of the input.
    for cohort_id, patients in by_id.items():
        resolved_mrns = {p.mrn for p in patients}
        input_mrns = {mrn for cid, mrns in MRN_COHORTS if cid == cohort_id for mrn in mrns}
        assert resolved_mrns.issubset(input_mrns), (
            f"Cohort '{cohort_id}': resolved MRNs {resolved_mrns} contain values not in input {input_mrns}"
        )

    # Validate MRN sets where filled in.
    for cohort_id, expected_mrns in EXPECTED_MRN_COHORTS.items():
        if expected_mrns:
            resolved_mrns = {p.mrn for p in by_id[cohort_id]}
            assert resolved_mrns == expected_mrns, (
                f"Cohort '{cohort_id}': expected MRNs {expected_mrns}, got {resolved_mrns}"
            )

    # Validate admissions per MRN where filled in.
    for cohort_id, expected_admissions in EXPECTED_MRN_ADMISSIONS.items():
        if expected_admissions:
            resolved = {
                p.mrn: [a.admission_ns for a in p.admissions] for p in by_id[cohort_id]
            }
            assert resolved == expected_admissions, (
                f"Cohort '{cohort_id}': expected admissions {expected_admissions}, got {resolved}"
            )


# ---------------------------------------------------------------------------
# Step 3 — demographic cohort (Priority 1B)
#
# Admission window: 2020-01-01 00:00:00 UTC → 2021-01-01 00:00:00 UTC.
# Confirmed to contain a subset of encounters in the real dataset.
#
# Fill in the cohort definitions below as you explore the real data.
# Each cohort exercises a different filter combination; add or remove cohorts
# as needed. Expected MRN sets go in EXPECTED_DEMOGRAPHIC_COHORTS.
# ---------------------------------------------------------------------------

DEMO_ADMIT_START_NS: int = 1577836800000000000  # 2020-01-01 00:00:00 UTC
DEMO_ADMIT_END_NS:   int = 1609459200000000000  # 2021-01-01 00:00:00 UTC

ONE_YEAR_NS: int = 365 * 24 * 3600 * 1_000_000_000
ONE_MONTH_NS: int = 30 * 24 * 3600 * 1_000_000_000

# Maps cohort_id → expected set of MRNs.
EXPECTED_DEMOGRAPHIC_COHORTS: dict[str, set[str]] = {
    "male_age_10_0_15_0": {"MRN824A6D3E", "MRND62AADF3", "MRN79BFCA31"},
    "female_age_10_3_15_10": {"MRN63163B57", "MRN44A9FE95"},
    "female_age_2_5_7_11": {"MRN3E7DB077", "MRNAA5C025D","MRN376EFBF4"}
}

# Maps cohort_id → {mrn: [Admission, ...]}.
#
# Each admission is checked in full: admission_ns, discharge_ns AND location must
# all match. A patient who transferred between units has one Admission per unit,
# so expect several entries for them, ordered by admission_ns.
#
# The admission_ns values below carry over from the previous run; discharge_ns and
# location are new and still need filling in. Run the test and copy the paste-ready
# block it prints (see "EXPECTED_DEMOGRAPHIC_ADMISSIONS" in the output) — then
# eyeball the values against the dataset before trusting them.
#
# Only cohorts with entries here are validated; leave a cohort out to skip it.
EXPECTED_DEMOGRAPHIC_ADMISSIONS: dict[str, dict[str, list[Admission]]] = {
    "male_age_10_0_15_0": {
        "MRN824A6D3E": [Admission(admission_ns=1608553652000000000, discharge_ns=1610082209000000000, location="ICU")],
        "MRND62AADF3": [Admission(admission_ns=1586531479000000000, discharge_ns=1586610119000000000, location="ICU")],
        "MRN79BFCA31": [Admission(admission_ns=1597582892000000000, discharge_ns=1597729818000000000, location="ICU")],
    },
    "female_age_10_3_15_10": {
        "MRN63163B57": [Admission(admission_ns=1591514059000000000, discharge_ns=1824889020735786141, location="ICU")],
        "MRN44A9FE95": [Admission(admission_ns=1586164554000000000, discharge_ns=1586242193000000000, location="ICU")],
    },
    "female_age_2_5_7_11": {
        "MRN3E7DB077": [Admission(admission_ns=1584420294000000000, discharge_ns=1586155101000000000, location="ICU")],
        "MRNAA5C025D": [Admission(admission_ns=1587313853000000000, discharge_ns=1587453694000000000, location="ICU")],
        "MRN376EFBF4": [Admission(admission_ns=1587319600000000000, discharge_ns=1589456668000000000, location="ICU")],
    },
}

DEMOGRAPHIC_COHORTS: list[DemographicCohort] = [
    DemographicCohort(id="no_filters"),
    DemographicCohort(id="male",   sex=["M"]),
    DemographicCohort(id="female", sex=["F"]),
    DemographicCohort(
        id="male_age_10_0_15_0",
        sex=["M"],
        age=[AgeBand(start_ns=10 * ONE_YEAR_NS + 0 * ONE_MONTH_NS, end_ns=15 * ONE_YEAR_NS + 0 * ONE_MONTH_NS)],
        location=["ICU"]
    ),
    DemographicCohort(
        id="female_age_10_3_15_10",
        sex=["F"],
        age=[AgeBand(start_ns=10 * ONE_YEAR_NS + 3 * ONE_MONTH_NS, end_ns=15 * ONE_YEAR_NS + 10 * ONE_MONTH_NS)],
        location=["ICU"]
    ),
    DemographicCohort(
        id="female_age_2_5_7_11",
        sex=["F"],
        age=[AgeBand(start_ns=2 * ONE_YEAR_NS + 5 * ONE_MONTH_NS, end_ns=7 * ONE_YEAR_NS + 11 * ONE_MONTH_NS)],
        location=["ICU"]
    ),
]


def test_demographic_cohort_real_data(sdk):
    """1B: demographic cohort — sex, age, location filters against real data.

    Cohort definitions and expected results are configured via
    DEMOGRAPHIC_COHORTS and EXPECTED_DEMOGRAPHIC_COHORTS at the top of this
    section. Add cohorts and fill in expected sets as you explore the dataset.
    """
    date_range = AdmissionDateRange(start=DEMO_ADMIT_START_NS, end=DEMO_ADMIT_END_NS)
    request = CohortDefinitionRequest(
        type="demographic",
        admission_date_range=date_range,
        cohorts=DEMOGRAPHIC_COHORTS,
    )
    result = sdk.dashboard_resolve_cohort(request, request_id="real-1b")
    # by_id: cohort_id → list[PatientAdmission]
    by_id: dict[str, list[PatientAdmission]] = {
        c.id: c.patients for c in result.cohorts
    }

    print(f"\n{'='*60}")
    for cohort_id, patients in by_id.items():
        print(f"  {cohort_id} ({len(patients)} patients):")
        for p in sorted(patients, key=lambda p: p.mrn)[:10]:
            print(f"    mrn={p.mrn}  admissions={p.admissions}")
    print(f"{'='*60}")

    # Paste-ready literal for EXPECTED_DEMOGRAPHIC_ADMISSIONS: copy the cohorts you
    # want validated into the constant above rather than transcribing by hand.
    print("\nEXPECTED_DEMOGRAPHIC_ADMISSIONS = {")
    for cohort_id in EXPECTED_DEMOGRAPHIC_ADMISSIONS:
        print(f'    "{cohort_id}": {{')
        for p in sorted(by_id.get(cohort_id, []), key=lambda p: p.mrn):
            entries = ", ".join(
                f"Admission(admission_ns={a.admission_ns}, "
                f"discharge_ns={a.discharge_ns}, location={a.location!r})"
                for a in p.admissions
            )
            print(f'        "{p.mrn}": [{entries}],')
        print("    },")
    print("}")

    no_filter_mrns = {p.mrn for p in by_id["no_filters"]}
    assert len(no_filter_mrns) > 0, "Expected at least one patient in the 2020 window."

    # Male and female cohorts must not overlap.
    male_mrns   = {p.mrn for p in by_id.get("male",   [])}
    female_mrns = {p.mrn for p in by_id.get("female", [])}
    overlap = male_mrns & female_mrns
    assert not overlap, f"Patients appear in both M and F cohorts: {overlap}"

    # Validate MRN sets where filled in.
    for cohort_id, expected_mrns in EXPECTED_DEMOGRAPHIC_COHORTS.items():
        if expected_mrns:
            resolved_mrns = {p.mrn for p in by_id[cohort_id]}
            assert resolved_mrns == expected_mrns, (
                f"Cohort '{cohort_id}': expected MRNs {expected_mrns}, got {resolved_mrns}"
            )

    # Validate admissions per MRN where filled in — admission_ns, discharge_ns and
    # location must all match (Admission is a pydantic model, so == compares fields).
    for cohort_id, expected_admissions in EXPECTED_DEMOGRAPHIC_ADMISSIONS.items():
        if expected_admissions:
            resolved = {p.mrn: p.admissions for p in by_id[cohort_id]}
            assert resolved == expected_admissions, (
                f"Cohort '{cohort_id}': expected admissions {expected_admissions}, got {resolved}"
            )
